"""Exact replay proof over real lake ZIP formats and archived original bytes."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import respx

from app.options.contract import OptionContract
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import RunResult, run_sessions
from app.options_bots import golden_proof
from app.options_bots.golden_proof import GoldenProofError, build_proof, input_manifest, reprove, result_sha256
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    minute_bar,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._market import leg, setup


async def _run(fake_catalog: FakeCatalog, tmp_lake: Path) -> RunResult:
    contract = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=600)
    await store_listing([vendor_listing_row(contract)])
    await store_underlying_day(
        fake_catalog, tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + minute * MINUTE_MS, "600", "600", "600", "600") for minute in range(65)],
    )
    await store_option_day(contract, [minute_bar(minute, 2, 2, 2, 2) for minute in range(65)])
    return await asyncio.to_thread(run_sessions, setup(leg()), [FRIDAY], LakeRunData("SPY"))


@pytest.fixture(autouse=True)
def fixed_build(monkeypatch: pytest.MonkeyPatch) -> None:
    # Other implementation agents may edit shared rules while these tests
    # run. Byte/rule identity detection is exercised separately below.
    monkeypatch.setattr(golden_proof, "current_rules_digest", lambda: "a" * 64)


@respx.mock
async def test_archived_original_inputs_reproduce_a_selected_run_after_the_lake_is_gone(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "proof")

    proof = await asyncio.to_thread(build_proof, source, blobs=blobs)
    shutil.rmtree(raw_root(tmp_lake))
    reproduced = await asyncio.to_thread(reprove, source, proof, blobs=blobs)

    assert proof["result_sha256"] == reproduced["result_sha256"] == result_sha256(source)
    assert proof["manifest"] == input_manifest(source)
    assert len(proof["manifest"]) == 3
    assert source.summary.trades == 1


@respx.mock
async def test_a_changed_receipted_input_does_not_approve_the_old_run(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    relative = next(path for path in input_manifest(source) if path.startswith("equity/"))
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), relative, b"changed file", admit=True)

    with pytest.raises(GoldenProofError, match="changed since"):
        await asyncio.to_thread(build_proof, source, blobs=BlobStore(tmp_path / "proof"))


@respx.mock
async def test_current_replay_must_match_recorded_outcomes_not_merely_their_input_hashes(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    changed = replace(source, summary=replace(source.summary, trades=source.summary.trades + 1))

    with pytest.raises(GoldenProofError, match="recorded outcomes"):
        await asyncio.to_thread(build_proof, changed, blobs=BlobStore(tmp_path / "proof"))


@respx.mock
async def test_a_missing_archived_blob_cannot_be_reproved(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "proof")
    proof = await asyncio.to_thread(build_proof, source, blobs=blobs)
    digest = next(iter(proof["manifest"].values()))
    (blobs.root / digest[:2] / digest).unlink()

    with pytest.raises(ProofMismatchError, match="missing"):
        await asyncio.to_thread(reprove, source, proof, blobs=blobs)


def test_loaded_rule_identity_refuses_a_changed_source_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(golden_proof, "_source_digest", lambda: "changed")

    with pytest.raises(GoldenProofError, match="Restart"):
        golden_proof.current_rules_digest()


@pytest.mark.parametrize("source", ["broker/alpaca/option_fees.py", "broker/alpaca/regulatory_fees.py", "broker/alpaca/clerk/money.py"])
def test_canonical_fee_calculation_changes_invalidate_the_rules_identity(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    before = golden_proof._source_digest()
    original = Path.read_bytes
    changed = golden_proof._APP_ROOT / source

    def read_with_changed_calculation(path: Path) -> bytes:
        content = original(path)
        return content + b"\n# Changed canonical calculation bytes\n" if path == changed else content

    monkeypatch.setattr(Path, "read_bytes", read_with_changed_calculation)
    assert golden_proof._source_digest() != before


async def _retained_reader(source: RunResult, blobs: BlobStore, tmp_lake: Path):
    from app.data_lake.admission import read_committed_bytes
    from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData

    manifest = input_manifest(source)
    size = 0
    for relative, expected in manifest.items():
        payload = await asyncio.to_thread(read_committed_bytes, raw_root(tmp_lake) / relative)
        assert blobs.put(payload) == expected
        size += len(payload)
    snapshot = Snapshot(manifest=manifest, absent=(), retained_bytes=size)
    return SnapshotRunData(source.setup.underlying, snapshot, blobs)


@respx.mock
async def test_retained_study_proof_survives_lake_repair_without_reading_mutable_inputs(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "study-proof")
    retained = await _retained_reader(source, blobs, tmp_lake)
    relative = next(path for path in input_manifest(source) if path.startswith("equity/"))
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), relative, b"repaired lake inputs", admit=True)

    def mutable_lake_is_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("A retained study proof must not consult the current lake")

    monkeypatch.setattr(golden_proof, "option_read_root", mutable_lake_is_forbidden)
    progress = []
    proof = await asyncio.to_thread(build_proof, source, blobs=blobs, source_data=retained,
                                    on_session=lambda done, total, day: progress.append((done, total, day)))
    assert proof["result_sha256"] == result_sha256(source)
    assert proof["manifest"] == input_manifest(source)
    assert progress == [(1, 1, FRIDAY), (1, 1, FRIDAY)]


@pytest.mark.parametrize("stop_after_replay", [1, 2])
@respx.mock
async def test_retained_proof_cancellation_is_checked_during_replays_and_before_return(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, stop_after_replay: int,
) -> None:
    from app.options_backtest.run import RunStopped

    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "cancel-proof")
    retained = await _retained_reader(source, blobs, tmp_lake)
    completions = []
    with pytest.raises(RunStopped):
        await asyncio.to_thread(build_proof, source, blobs=blobs, source_data=retained,
            on_session=lambda done, total, day: completions.append(day),
            should_stop=lambda: len(completions) >= stop_after_replay)
    assert completions == [FRIDAY] * stop_after_replay


@respx.mock
async def test_retained_proof_requires_existing_verified_blob_even_when_lake_still_holds_it(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "missing-proof")
    retained = await _retained_reader(source, blobs, tmp_lake)
    digest = next(iter(input_manifest(source).values()))
    (blobs.root / digest[:2] / digest).unlink()
    with pytest.raises(ProofMismatchError, match="missing"):
        await asyncio.to_thread(build_proof, source, blobs=blobs, source_data=retained)


def test_minute_observation_calculation_changes_invalidate_the_options_rules_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    before = golden_proof._source_digest()
    original = Path.read_bytes
    changed = golden_proof._APP_ROOT / "options_backtest/observation.py"

    def read_with_changed_observation(path: Path) -> bytes:
        content = original(path)
        return content + b"\n# Changed minute-equity rule bytes\n" if path == changed else content

    monkeypatch.setattr(Path, "read_bytes", read_with_changed_observation)
    assert golden_proof._source_digest() != before
