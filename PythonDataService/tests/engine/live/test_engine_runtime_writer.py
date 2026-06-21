"""PRD #619-B B1 — atomic writer + schema contract for ``engine_runtime.json``.

Asserts:

- The full schema (four domain blocks + envelope) round-trips through
  ``model_dump`` / ``model_validate_json`` without field drift.
- The atomic writer leaves no ``.tmp`` debris behind on success.
- A subsequent write replaces the file fully (no fields leak from a
  prior schema across writes).
- A higher-than-known ``schema_version`` is read as ``None`` (forward
  incompatibility is fail-closed).
- A missing file, an unreadable file, and a malformed JSON body all
  return ``None`` rather than raising.
- The serialized form pins ``int64 ms UTC`` for every timestamp
  (no ISO strings, no ``datetime`` objects on the wire).

The publisher's monotonic-``snapshot_seq`` + serialized-write contract
is exercised in ``test_engine_runtime_publisher.py`` (619-B B2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.engine_runtime import (
    ENGINE_RUNTIME_FILENAME,
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    read_engine_runtime_snapshot,
    write_engine_runtime_snapshot,
)


def _make_snapshot(
    *,
    seq: int = 1,
    written_at_ms: int = 1_700_000_000_000,
    command_state: str = "RUNNING",
    identity: str = "PAPER_VERIFIED",
    capability: str = "PAPER_ORDERS_ENABLED",
    posture: str = "PAPER_EXECUTION",
    connection_state: str = "connected",
) -> EngineRuntimeSnapshot:
    return EngineRuntimeSnapshot(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-abc",
        pid=4242,
        process_start_identity="child-boot-0001",
        expected_daemon_boot_id="daemon-boot-0001",
        snapshot_seq=seq,
        written_at_ms=written_at_ms,
        command_loop=CommandLoopBlock(
            heartbeat_at_ms=written_at_ms,
            state=command_state,  # type: ignore[arg-type]
        ),
        broker=BrokerBlock(
            identity=identity,  # type: ignore[arg-type]
            submission_capability=capability,  # type: ignore[arg-type]
            effective_posture=posture,  # type: ignore[arg-type]
            connection_state=connection_state,  # type: ignore[arg-type]
            connection_epoch=1,
            connected_account="DU1234567",
            port_class="paper_port",
            observation_at_ms=written_at_ms,
            probe_completed_at_ms=written_at_ms - 100,
            reconnect_attempt=0,
        ),
        bar_loop=BarLoopBlock(
            heartbeat_at_ms=written_at_ms,
            latest_source_bar_ms=written_at_ms - 60_000,
            expected_interval_ms=60_000,
        ),
        control_plane=ControlPlaneBlock(
            lease_observed_at_ms=written_at_ms - 500,
            observed_daemon_boot_id="daemon-boot-0001",
        ),
    )


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


def test_snapshot_round_trips_full_field_set() -> None:
    snapshot = _make_snapshot()
    payload = snapshot.model_dump_json()
    restored = EngineRuntimeSnapshot.model_validate_json(payload)
    assert restored == snapshot


def test_snapshot_rejects_extra_envelope_fields() -> None:
    payload = json.loads(_make_snapshot().model_dump_json())
    payload["unexpected"] = "field"
    with pytest.raises(ValueError):
        EngineRuntimeSnapshot.model_validate(payload)


def test_snapshot_rejects_extra_block_fields() -> None:
    payload = json.loads(_make_snapshot().model_dump_json())
    payload["broker"]["unexpected"] = "field"
    with pytest.raises(ValueError):
        EngineRuntimeSnapshot.model_validate(payload)


def test_snapshot_rejects_negative_timestamps() -> None:
    with pytest.raises(ValueError):
        _make_snapshot(written_at_ms=-1)


def test_snapshot_rejects_unknown_command_state() -> None:
    payload = json.loads(_make_snapshot().model_dump_json())
    payload["command_loop"]["state"] = "WONKY"
    with pytest.raises(ValueError):
        EngineRuntimeSnapshot.model_validate(payload)


# ---------------------------------------------------------------------------
# Wire shape — timestamps are int64 ms UTC at the boundary.
# ---------------------------------------------------------------------------


def test_wire_shape_timestamps_are_integers(tmp_path: Path) -> None:
    snapshot = _make_snapshot()
    write_engine_runtime_snapshot(tmp_path, snapshot)
    raw = json.loads((tmp_path / ENGINE_RUNTIME_FILENAME).read_text(encoding="utf-8"))

    # Every timestamp field at the artifact boundary is a plain int.
    assert isinstance(raw["written_at_ms"], int)
    assert isinstance(raw["command_loop"]["heartbeat_at_ms"], int)
    assert isinstance(raw["broker"]["observation_at_ms"], int)
    assert isinstance(raw["broker"]["probe_completed_at_ms"], int)
    assert isinstance(raw["bar_loop"]["heartbeat_at_ms"], int)
    assert isinstance(raw["bar_loop"]["latest_source_bar_ms"], int)
    assert isinstance(raw["control_plane"]["lease_observed_at_ms"], int)


# ---------------------------------------------------------------------------
# Atomic write + replace
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_tmp_on_success(tmp_path: Path) -> None:
    write_engine_runtime_snapshot(tmp_path, _make_snapshot())

    assert (tmp_path / ENGINE_RUNTIME_FILENAME).exists()
    # The sibling .tmp must not survive past the rename.
    assert not (tmp_path / f"{ENGINE_RUNTIME_FILENAME}.tmp").exists()


def test_write_creates_run_dir_when_missing(tmp_path: Path) -> None:
    nested = tmp_path / "live_runs" / "run-zzz"
    assert not nested.exists()

    write_engine_runtime_snapshot(nested, _make_snapshot())

    assert (nested / ENGINE_RUNTIME_FILENAME).exists()


def test_subsequent_write_replaces_prior_content(tmp_path: Path) -> None:
    first = _make_snapshot(seq=1, command_state="RUNNING")
    second = _make_snapshot(seq=42, command_state="PAUSED")

    write_engine_runtime_snapshot(tmp_path, first)
    write_engine_runtime_snapshot(tmp_path, second)

    raw = json.loads((tmp_path / ENGINE_RUNTIME_FILENAME).read_text(encoding="utf-8"))
    assert raw["snapshot_seq"] == 42
    assert raw["command_loop"]["state"] == "PAUSED"


# ---------------------------------------------------------------------------
# Reader — fail-closed on missing / malformed / forward-incompatible.
# ---------------------------------------------------------------------------


def test_read_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert read_engine_runtime_snapshot(tmp_path / "missing.json") is None


def test_read_returns_none_for_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "engine_runtime.json"
    path.write_text("not-json-at-all\n", encoding="utf-8")

    assert read_engine_runtime_snapshot(path) is None


def test_read_returns_none_for_schema_validation_failure(tmp_path: Path) -> None:
    path = tmp_path / "engine_runtime.json"
    payload = json.loads(_make_snapshot().model_dump_json())
    del payload["broker"]  # required block missing
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_engine_runtime_snapshot(path) is None


def test_read_returns_none_for_forward_incompatible_schema_version(
    tmp_path: Path,
) -> None:
    """PRD #619-B — a higher schema_version is read as ``None`` so the
    backend freshness evaluator surfaces ``UNKNOWN`` rather than parse
    a partial subset of a future contract."""
    path = tmp_path / "engine_runtime.json"
    payload = json.loads(_make_snapshot().model_dump_json())
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_engine_runtime_snapshot(path) is None


def test_read_recovers_a_written_snapshot(tmp_path: Path) -> None:
    snapshot = _make_snapshot(seq=7)

    write_engine_runtime_snapshot(tmp_path, snapshot)
    restored = read_engine_runtime_snapshot(tmp_path / ENGINE_RUNTIME_FILENAME)

    assert restored == snapshot
