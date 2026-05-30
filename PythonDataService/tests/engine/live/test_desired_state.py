"""Unit tests for the durable desired-state sidecar (PRD-A § 16.4
Resolution 7 / PR-D).

Mirrors test_live_state_sidecar's style: round-trip, atomic-write
hygiene, default-when-absent, version bump, and corrupt-file refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRecord,
    DesiredStateRepo,
    stable_desired_state_path,
)


def test_stable_path_layout(tmp_path: Path) -> None:
    path = stable_desired_state_path(tmp_path, "spy_ema_crossover")
    assert path == tmp_path / "live_state" / "spy_ema_crossover" / "desired_state.json"


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    repo = DesiredStateRepo(tmp_path / "live_state" / "x" / "desired_state.json")
    assert repo.read() is None


def test_read_state_defaults_to_running_when_absent(tmp_path: Path) -> None:
    repo = DesiredStateRepo(tmp_path / "live_state" / "x" / "desired_state.json")
    assert repo.read_state() is DesiredState.RUNNING


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    record = DesiredStateRecord(
        desired_state=DesiredState.PAUSED,
        updated_at_ms=1_700_000_000_000,
        updated_by="operator",
        reason="manual hold",
        version=1,
    )
    repo.write(record)

    loaded = repo.read()
    assert loaded == record
    assert repo.read_state() is DesiredState.PAUSED


def test_set_without_prior_file_starts_at_version_one(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    record = repo.set(
        DesiredState.PAUSED,
        updated_by="operator",
        now_ms=1_700_000_000_000,
        reason="hold",
    )
    assert record.version == 1
    assert record.desired_state is DesiredState.PAUSED
    assert record.updated_by == "operator"
    assert record.reason == "hold"
    assert record.updated_at_ms == 1_700_000_000_000


def test_set_bumps_version_and_overwrites_fields(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    repo.set(DesiredState.PAUSED, updated_by="operator", now_ms=1_000)

    second = repo.set(
        DesiredState.RUNNING,
        updated_by="engine",
        now_ms=2_000,
        reason="command_channel:RESUME",
    )
    assert second.version == 2
    assert second.desired_state is DesiredState.RUNNING
    assert second.updated_by == "engine"
    assert second.reason == "command_channel:RESUME"
    assert second.updated_at_ms == 2_000
    assert repo.read_state() is DesiredState.RUNNING


def test_corrupt_file_raises_typed_error(tmp_path: Path) -> None:
    path = stable_desired_state_path(tmp_path, "x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    repo = DesiredStateRepo(path)
    with pytest.raises(DesiredStateCorruptError) as excinfo:
        repo.read()
    assert excinfo.value.path == path


def test_schema_violation_raises_typed_error(tmp_path: Path) -> None:
    path = stable_desired_state_path(tmp_path, "x")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON, invalid enum value for desired_state.
    path.write_text(
        '{"desired_state": "FROLICKING", "updated_at_ms": 1, "updated_by": "op"}',
        encoding="utf-8",
    )
    repo = DesiredStateRepo(path)
    with pytest.raises(DesiredStateCorruptError):
        repo.read()


def test_write_leaves_no_tmp_artifact(tmp_path: Path) -> None:
    path = stable_desired_state_path(tmp_path, "x")
    repo = DesiredStateRepo(path)
    repo.set(DesiredState.STOPPED, updated_by="operator", now_ms=1)

    leftovers = list(path.parent.glob("*.tmp"))
    assert leftovers == []
    assert path.exists()
