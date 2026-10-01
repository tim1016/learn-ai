"""Golden Search proof: content-addressed inputs and a two-root decision-trace replay (#2696).

Every replay here runs the registered EMA Signal Program at its validated
point over a seeded, catalog-admitted adjusted lake, so the lake read, the
companion and corporate-action-basis checks, and the restored tree all go
through the production readers.
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
from datetime import date
from pathlib import Path

import pytest

from app.data_lake.path_policy import lake_subpath
from app.engine.engine import EvaluationBoundaryError
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search.proof import (
    BlobStore,
    ProofMismatchError,
    ProofRecord,
    ProofWindow,
    build_proof,
    decision_trace,
    materialize,
    reprove,
    stage_proof_inputs,
)
from app.research.sweep.snapshot import DataSnapshot, capture_data_snapshot
from app.schemas.signal_program_seal import semantic_payload_hash
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.lean_store import seed_store_day

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

PROGRAM = "ema_crossover_signal"
SYMBOL = "SPY"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
VALIDATED = dict(CONTRACT.validated_settings)
# The study's receipt spans two weeks; the proof replays one warmup session
# (Mon 2025-02-03) and scores the three sessions after it.
SNAPSHOT_START, SNAPSHOT_END = date(2025, 2, 3), date(2025, 2, 14)
WINDOW = ProofWindow(
    start_ms=et_midnight_ms(date(2025, 2, 4)),
    end_ms=et_midnight_ms(date(2025, 2, 7)),
    warmup_from_ms=et_midnight_ms(date(2025, 2, 3)),
)
PROOF_SESSIONS = [date(2025, 2, 3), date(2025, 2, 4), date(2025, 2, 5), date(2025, 2, 6)]
ARTIFACT_DIGEST = "a" * 64
WIRING_DIGEST = "b" * 64


def _zip(day: date) -> str:
    return f"equity/usa/minute/spy/{day.strftime('%Y%m%d')}_trade.zip"


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    root = tmp_path / "writer-root" / lake_subpath("polygon_split_adjusted")
    root.mkdir(parents=True)
    for day in expected_sessions(SNAPSHOT_START, SNAPSHOT_END):
        seed_store_day(root, SYMBOL, day)
    return root


@pytest.fixture
def snapshot(lake: Path) -> DataSnapshot:
    return capture_data_snapshot(
        roots=[lake], symbol=SYMBOL, resolution="minute", data_start=SNAPSHOT_START, data_end=SNAPSHOT_END
    )


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def _build(snapshot: DataSnapshot, lake: Path, blobs: BlobStore, **params: float) -> ProofRecord:
    return build_proof(
        strategy_key=PROGRAM,
        symbol=SYMBOL,
        params={**VALIDATED, **params},
        window=WINDOW,
        snapshot=snapshot,
        roots=[lake],
        blob_store=blobs,
        artifact_digest=ARTIFACT_DIGEST,
        wiring_digest=WIRING_DIGEST,
        created_at_ms=1_738_800_000_000,
    )


# ---------------------------------------------------------------------------
# BlobStore
# ---------------------------------------------------------------------------
def test_blob_store_put_round_trips_through_its_content_address(blobs: BlobStore) -> None:
    payload = b"minute bars"

    digest = blobs.put(payload)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert (blobs.root / digest[:2] / digest).read_bytes() == payload
    assert blobs.get(digest) == payload
    assert blobs.put(payload) == digest


def test_blob_store_get_refuses_a_corrupted_blob(blobs: BlobStore) -> None:
    digest = blobs.put(b"minute bars")
    (blobs.root / digest[:2] / digest).write_bytes(b"minute bars, edited")

    with pytest.raises(ProofMismatchError) as excinfo:
        blobs.get(digest)

    assert excinfo.value.code == "BLOB_CORRUPT"


def test_blob_store_get_refuses_a_missing_blob(blobs: BlobStore) -> None:
    with pytest.raises(ProofMismatchError) as excinfo:
        blobs.get(hashlib.sha256(b"never stored").hexdigest())

    assert excinfo.value.code == "BLOB_MISSING"


def test_blob_store_put_rewrites_a_corrupted_blob_from_the_verified_bytes(blobs: BlobStore) -> None:
    digest = blobs.put(b"minute bars")
    (blobs.root / digest[:2] / digest).write_bytes(b"torn")

    assert blobs.put(b"minute bars") == digest
    assert blobs.get(digest) == b"minute bars"


@pytest.mark.parametrize("address", ["../../etc/passwd", "A" * 64, "a" * 63, ""])
def test_blob_store_refuses_an_address_that_is_not_a_sha256(blobs: BlobStore, address: str) -> None:
    with pytest.raises(ValueError, match="not a sha256 blob address"):
        blobs.get(address)


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def test_proof_window_refuses_an_anchor_that_is_not_an_et_midnight() -> None:
    with pytest.raises(ValueError, match="ET-midnight"):
        ProofWindow(start_ms=WINDOW.start_ms + 1, end_ms=WINDOW.end_ms, warmup_from_ms=WINDOW.warmup_from_ms)


def test_proof_window_refuses_a_warmup_after_the_window_start() -> None:
    with pytest.raises(ValueError, match="warmup_from_ms <= start_ms < end_ms"):
        ProofWindow(start_ms=WINDOW.start_ms, end_ms=WINDOW.end_ms, warmup_from_ms=WINDOW.end_ms)


# ---------------------------------------------------------------------------
# Staging and materialization
# ---------------------------------------------------------------------------
def test_stage_proof_inputs_stages_only_the_window_and_its_warmup_sessions(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    inputs = stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)

    zips = [_zip(day) for day in PROOF_SESSIONS]
    expected = {
        **{path: snapshot.artifacts[path] for path in zips},
        **{f"{path}.adjustment.json": snapshot.artifacts[f"{path}.adjustment.json"] for path in zips},
        "adjustment_versions/spy.json": snapshot.corporate_action_versions["SPY"],
    }
    assert inputs.adjusted is True
    assert inputs.symbol == "SPY"
    assert inputs.artifacts == expected
    assert list(inputs.artifacts) == sorted(expected)
    for digest in inputs.artifacts.values():
        assert hashlib.sha256(blobs.get(digest)).hexdigest() == digest


def test_stage_proof_inputs_refuses_bytes_that_moved_since_the_receipt(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    seed_store_day(lake, SYMBOL, date(2025, 2, 5), count=200)

    with pytest.raises(ProofMismatchError) as excinfo:
        stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)

    assert excinfo.value.code == "RECEIPT_MISMATCH"
    assert _zip(date(2025, 2, 5)) in str(excinfo.value)


def test_stage_proof_inputs_refuses_a_session_the_receipt_never_covered(lake: Path, blobs: BlobStore) -> None:
    narrow = capture_data_snapshot(
        roots=[lake], symbol=SYMBOL, resolution="minute", data_start=date(2025, 2, 4), data_end=SNAPSHOT_END
    )

    with pytest.raises(ProofMismatchError) as excinfo:
        stage_proof_inputs(narrow, roots=[lake], window=WINDOW, blob_store=blobs)

    assert excinfo.value.code == "INPUT_NOT_RECEIPTED"
    assert _zip(date(2025, 2, 3)) in str(excinfo.value)


def test_stage_proof_inputs_refuses_a_session_missing_from_the_lake(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    (lake / _zip(date(2025, 2, 4))).unlink()

    with pytest.raises(ProofMismatchError) as excinfo:
        stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)

    assert excinfo.value.code == "INPUT_MISSING"


def test_stage_proof_inputs_refuses_sessions_split_across_raw_and_adjusted_roots(
    lake: Path, blobs: BlobStore, tmp_path: Path
) -> None:
    # One proof replays one data root: a window whose sessions resolve to a
    # raw root and an adjusted one could not be restored as either.
    raw = tmp_path / "raw-writer" / lake_subpath("raw")
    raw.mkdir(parents=True)
    moved = date(2025, 2, 5)
    seed_store_day(raw, SYMBOL, moved)
    (lake / _zip(moved)).unlink()
    split = capture_data_snapshot(
        roots=[lake, raw], symbol=SYMBOL, resolution="minute", data_start=SNAPSHOT_START, data_end=SNAPSHOT_END
    )

    with pytest.raises(ValueError, match="more than one"):
        stage_proof_inputs(split, roots=[lake, raw], window=WINDOW, blob_store=blobs)


def test_materialize_writes_every_input_at_its_relative_path(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore, tmp_path: Path
) -> None:
    inputs = stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)

    data_root = materialize(inputs, blobs, tmp_path / "restore")

    assert data_root.name == "polygon_split_adjusted"
    restored = {
        path.relative_to(data_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in data_root.rglob("*")
        if path.is_file()
    }
    assert restored == inputs.artifacts


def test_restored_replay_verifies_the_corporate_action_basis(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore, tmp_path: Path
) -> None:
    inputs = stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)
    without_basis = dataclasses.replace(
        inputs,
        artifacts={path: digest for path, digest in inputs.artifacts.items() if path != "adjustment_versions/spy.json"},
    )
    data_root = materialize(without_basis, blobs, tmp_path / "restore")

    with pytest.raises(ProofMismatchError) as excinfo:
        decision_trace(PROGRAM, SYMBOL, VALIDATED, WINDOW, roots=[data_root], manifest=without_basis.artifacts)

    assert excinfo.value.code == "RECEIPT_MISMATCH"


def test_materialize_refuses_a_tampered_blob(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore, tmp_path: Path
) -> None:
    inputs = stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)
    digest = inputs.artifacts[_zip(date(2025, 2, 5))]
    (blobs.root / digest[:2] / digest).write_bytes(b"not the receipted zip")

    with pytest.raises(ProofMismatchError) as excinfo:
        materialize(inputs, blobs, tmp_path / "restore")

    assert excinfo.value.code == "BLOB_CORRUPT"


def test_materialize_refuses_a_destination_inside_a_managed_lake(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore, tmp_path: Path
) -> None:
    inputs = stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs)

    with pytest.raises(ValueError, match="managed lake root"):
        materialize(inputs, blobs, tmp_path / "elsewhere" / "lake")


# ---------------------------------------------------------------------------
# Decision trace
# ---------------------------------------------------------------------------
def test_decision_trace_on_a_seeded_lake_is_deterministic(snapshot: DataSnapshot, lake: Path) -> None:
    first = decision_trace(PROGRAM, SYMBOL, VALIDATED, WINDOW, roots=[lake], manifest=snapshot.artifacts)
    second = decision_trace(PROGRAM, SYMBOL, VALIDATED, WINDOW, roots=[lake], manifest=snapshot.artifacts)

    # Four regular sessions of 26 fifteen-minute decision bars each.
    assert first.trace_count == 4 * 26
    assert first == second


def test_decision_trace_root_binds_the_parameters(snapshot: DataSnapshot, lake: Path) -> None:
    validated = decision_trace(PROGRAM, SYMBOL, VALIDATED, WINDOW, roots=[lake], manifest=snapshot.artifacts)
    widened = decision_trace(
        PROGRAM, SYMBOL, {**VALIDATED, "rsi_min": 30.0}, WINDOW, roots=[lake], manifest=snapshot.artifacts
    )

    assert widened.trace_count == validated.trace_count
    assert widened.trace_root != validated.trace_root


def test_decision_trace_refuses_a_cold_start_at_the_window(snapshot: DataSnapshot, lake: Path) -> None:
    # A weekend "warmup" reads no bar before the boundary, so the program is
    # not ready at the first scored bar.
    cold = ProofWindow(
        start_ms=et_midnight_ms(date(2025, 2, 3)),
        end_ms=WINDOW.end_ms,
        warmup_from_ms=et_midnight_ms(date(2025, 2, 1)),
    )

    with pytest.raises(EvaluationBoundaryError, match="not ready at the evaluation start"):
        decision_trace(PROGRAM, SYMBOL, VALIDATED, cold, roots=[lake], manifest=snapshot.artifacts)


def test_decision_trace_refuses_a_window_without_a_decision(lake: Path) -> None:
    # The scored session holds five regular minutes: the replay crosses into
    # evaluation primed, but no fifteen-minute decision bar closes inside it.
    seed_store_day(lake, SYMBOL, date(2025, 2, 4), count=5)
    short = capture_data_snapshot(
        roots=[lake], symbol=SYMBOL, resolution="minute", data_start=SNAPSHOT_START, data_end=SNAPSHOT_END
    )
    window = ProofWindow(
        start_ms=et_midnight_ms(date(2025, 2, 4)),
        end_ms=et_midnight_ms(date(2025, 2, 5)),
        warmup_from_ms=et_midnight_ms(date(2025, 2, 3)),
    )

    with pytest.raises(ProofMismatchError) as excinfo:
        decision_trace(PROGRAM, SYMBOL, VALIDATED, window, roots=[lake], manifest=short.artifacts)

    assert excinfo.value.code == "EMPTY_REPLAY"


def test_decision_trace_refuses_parameters_for_another_symbol(snapshot: DataSnapshot, lake: Path) -> None:
    with pytest.raises(ValueError, match="name symbol"):
        decision_trace(
            PROGRAM, SYMBOL, {**VALIDATED, "symbol": "QQQ"}, WINDOW, roots=[lake], manifest=snapshot.artifacts
        )


# ---------------------------------------------------------------------------
# Proof and re-proof
# ---------------------------------------------------------------------------
def test_build_proof_restored_replay_equals_lake_replay(snapshot: DataSnapshot, lake: Path, blobs: BlobStore) -> None:
    record = _build(snapshot, lake, blobs)

    lake_only = decision_trace(PROGRAM, SYMBOL, VALIDATED, WINDOW, roots=[lake], manifest=record.manifest)
    assert record.lake_trace_root == record.restored_trace_root == lake_only.trace_root
    assert record.trace_count == lake_only.trace_count
    assert record.program_version == CONTRACT.program_version
    # The canonical point is the schema's own dump, symbol included (it omits
    # identity-neutral defaults, #2696), not the caller's mapping.
    canonical = _STRATEGY_REGISTRY[PROGRAM].param_schema.model_validate({**VALIDATED, "symbol": "SPY"})
    assert record.params == canonical.model_dump(mode="json")
    assert record.params["symbol"] == "SPY"
    assert record.manifest == stage_proof_inputs(snapshot, roots=[lake], window=WINDOW, blob_store=blobs).artifacts
    assert (record.artifact_digest, record.wiring_digest) == (ARTIFACT_DIGEST, WIRING_DIGEST)


def test_proof_record_round_trips_and_hashes_its_canonical_json(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    record = _build(snapshot, lake, blobs)
    payload = record.as_dict()

    assert ProofRecord.from_dict(payload) == record
    assert record.sha256() == semantic_payload_hash(payload)
    assert payload["window"] == {"start_ms": WINDOW.start_ms, "end_ms": WINDOW.end_ms}
    assert payload["warmup_from_ms"] == WINDOW.warmup_from_ms
    assert payload["schema_version"] == 1


def test_proof_record_from_dict_refuses_a_malformed_payload(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    payload = _build(snapshot, lake, blobs).as_dict()

    with pytest.raises(ValueError, match="malformed proof record"):
        ProofRecord.from_dict({**payload, "adjusted": "false"})
    with pytest.raises(ValueError, match="malformed proof record"):
        ProofRecord.from_dict({key: value for key, value in payload.items() if key != "manifest"})
    with pytest.raises(ValueError, match="unsupported proof schema version"):
        ProofRecord.from_dict({**payload, "schema_version": 2})


def test_build_proof_refuses_a_lake_that_moved_after_the_receipt(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    seed_store_day(lake, SYMBOL, date(2025, 2, 6), count=300)

    with pytest.raises(ProofMismatchError) as excinfo:
        _build(snapshot, lake, blobs)

    assert excinfo.value.code == "RECEIPT_MISMATCH"


def test_reprove_reproduces_the_recorded_root_from_blobs_alone(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    record = _build(snapshot, lake, blobs)
    shutil.rmtree(lake)

    reproved = reprove(record, blobs, artifact_digest="c" * 64, wiring_digest="d" * 64, created_at_ms=1_738_900_000_000)

    assert reproved.restored_trace_root == record.restored_trace_root
    assert reproved.lake_trace_root == record.lake_trace_root
    assert (reproved.artifact_digest, reproved.wiring_digest) == ("c" * 64, "d" * 64)
    assert reproved.created_at_ms == 1_738_900_000_000
    assert reproved.manifest == record.manifest


def test_build_proof_over_a_raw_lake_restores_and_reproves_without_adjustment_receipts(
    blobs: BlobStore, tmp_path: Path
) -> None:
    raw = tmp_path / "raw-writer" / lake_subpath("raw")
    raw.mkdir(parents=True)
    for day in PROOF_SESSIONS:
        seed_store_day(raw, SYMBOL, day)
    raw_snapshot = capture_data_snapshot(
        roots=[raw], symbol=SYMBOL, resolution="minute", data_start=PROOF_SESSIONS[0], data_end=PROOF_SESSIONS[-1]
    )

    record = _build(raw_snapshot, raw, blobs)
    restored_root = materialize(record.inputs, blobs, tmp_path / "restore")
    shutil.rmtree(raw)
    reproved = reprove(record, blobs, artifact_digest=ARTIFACT_DIGEST, wiring_digest=WIRING_DIGEST)

    assert record.adjusted is False
    assert restored_root.name == "raw"
    assert sorted(record.manifest) == [_zip(day) for day in PROOF_SESSIONS]
    assert record.lake_trace_root == record.restored_trace_root
    assert reproved.restored_trace_root == record.restored_trace_root


def test_reprove_refuses_a_tampered_blob(snapshot: DataSnapshot, lake: Path, blobs: BlobStore) -> None:
    record = _build(snapshot, lake, blobs)
    digest = record.manifest[_zip(date(2025, 2, 4))]
    (blobs.root / digest[:2] / digest).write_bytes(b"tampered")

    with pytest.raises(ProofMismatchError) as excinfo:
        reprove(record, blobs, artifact_digest=ARTIFACT_DIGEST, wiring_digest=WIRING_DIGEST)

    assert excinfo.value.code == "BLOB_CORRUPT"


def test_reprove_refuses_a_replay_that_no_longer_reproduces_the_root(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    record = _build(snapshot, lake, blobs)
    drifted = dataclasses.replace(record, restored_trace_root="0" * 64)

    with pytest.raises(ProofMismatchError) as excinfo:
        reprove(drifted, blobs, artifact_digest=ARTIFACT_DIGEST, wiring_digest=WIRING_DIGEST)

    assert excinfo.value.code == "TRACE_MISMATCH"


def test_reprove_refuses_a_changed_program_version(snapshot: DataSnapshot, lake: Path, blobs: BlobStore) -> None:
    record = dataclasses.replace(_build(snapshot, lake, blobs), program_version="ema-crossover-signal/v0")

    with pytest.raises(ProofMismatchError) as excinfo:
        reprove(record, blobs, artifact_digest=ARTIFACT_DIGEST, wiring_digest=WIRING_DIGEST)

    assert excinfo.value.code == "PROGRAM_CHANGED"


def test_reprove_refuses_parameters_the_schema_no_longer_accepts(
    snapshot: DataSnapshot, lake: Path, blobs: BlobStore
) -> None:
    record = _build(snapshot, lake, blobs)
    retired = dataclasses.replace(record, params={**record.params, "retired_knob": 1})

    with pytest.raises(ProofMismatchError) as excinfo:
        reprove(retired, blobs, artifact_digest=ARTIFACT_DIGEST, wiring_digest=WIRING_DIGEST)

    assert excinfo.value.code == "PARAMETERS_CHANGED"
