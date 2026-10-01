"""Unit tests for the durable desired-state sidecar.

Covers round-trip, atomic-write hygiene, default-when-absent, version
bump, and corrupt-file refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRecord,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.schemas.bot_end import BotEnd, RecordedEnd


def test_stable_path_layout(tmp_path: Path) -> None:
    path = stable_desired_state_path(tmp_path, "spy_ema_crossover")
    assert path == tmp_path / "live_state" / "spy_ema_crossover" / "desired_state.json"


@pytest.mark.parametrize("bad_sid", ["../escape", "nested/id", "", " spy", ".", ".."])
def test_stable_desired_state_path_rejects_unsafe_strategy_instance_id(
    tmp_path: Path, bad_sid: str
) -> None:
    with pytest.raises(ValueError):
        stable_desired_state_path(tmp_path, bad_sid)


def test_stable_desired_state_path_rejects_symlink_escape(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    live_state_root = artifacts_root / "live_state"
    live_state_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (live_state_root / "x").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        stable_desired_state_path(artifacts_root, "x")


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


def test_a_legacy_record_reads_as_no_end(tmp_path: Path) -> None:
    """#2607 migration: a bot running before ends existed has no end and keeps running.

    Its file still carries the retired ``end_day_requested`` marker; the
    record reads, with no end, and no field of it is lost.
    """
    path = stable_desired_state_path(tmp_path, "x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"desired_state": "RUNNING", "updated_at_ms": 5, "updated_by": "bot_runner", '
        '"reason": "deploy", "end_day_requested": false, "version": 3}',
        encoding="utf-8",
    )

    record = DesiredStateRepo(path).read()

    assert record is not None
    assert record.desired_state is DesiredState.RUNNING
    assert record.version == 3
    assert record.end is None
    assert record.pending_end() is None


def test_the_end_is_stored_as_one_nested_record(tmp_path: Path) -> None:
    """One ``end`` object -- the time, sell or keep, and when it was carried out (#2607 review)."""
    path = stable_desired_state_path(tmp_path, "x")
    repo = DesiredStateRepo(path)

    repo.set(
        DesiredState.RUNNING, updated_by="bot_runner", now_ms=1,
        end=BotEnd(end_at_ms=1_790_000_000_000, end_action="KEEP"),
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["end"] == {"end_at_ms": 1_790_000_000_000, "end_action": "KEEP", "carried_out_at_ms": None}
    assert not {"end_at_ms", "end_action", "end_carried_out_at_ms"} & stored.keys()


def test_set_records_an_end_and_every_later_state_change_keeps_it(tmp_path: Path) -> None:
    """The end is owner intent that a crash, a restart or a Stop's own write must not lose."""
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    end = BotEnd(end_at_ms=1_790_000_000_000, end_action="KEEP")

    repo.set(DesiredState.RUNNING, updated_by="bot_runner", now_ms=1, reason="deploy", end=end)
    stopped = repo.set(DesiredState.STOPPED, updated_by="bot_runner", now_ms=2, reason="terminal_outcome:CRASHED")

    assert stopped.pending_end() == end
    assert stopped.end == RecordedEnd(end_at_ms=1_790_000_000_000, end_action="KEEP")


def test_set_with_no_end_clears_it(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    repo.set(DesiredState.RUNNING, updated_by="bot_runner", now_ms=1, end=BotEnd(end_at_ms=1_790_000_000_000))

    stopped = repo.set(DesiredState.STOPPED, updated_by="operator", now_ms=2, end=None)

    assert stopped.end is None
    assert stopped.pending_end() is None


def test_set_end_changes_the_end_and_keeps_the_desired_state(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    repo.set(DesiredState.RUNNING, updated_by="bot_runner", now_ms=1, end=BotEnd(end_at_ms=1_790_000_000_000))

    edited = repo.set_end(BotEnd(end_at_ms=1_790_000_600_000, end_action="KEEP"), updated_by="operator", now_ms=2)

    assert edited.desired_state is DesiredState.RUNNING
    assert edited.pending_end() == BotEnd(end_at_ms=1_790_000_600_000, end_action="KEEP")
    assert edited.version == 2


def test_mark_end_carried_out_ends_the_pending_end_once(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    end = BotEnd(end_at_ms=1_790_000_000_000)
    repo.set(DesiredState.RUNNING, updated_by="bot_runner", now_ms=1, end=end)

    first = repo.mark_end_carried_out(end, updated_by="account_clerk", now_ms=1_790_000_005_000)
    again = repo.mark_end_carried_out(end, updated_by="account_clerk", now_ms=1_790_000_020_000)

    assert first.pending_end() is None
    # The carried-out end stays readable.
    assert first.end == RecordedEnd(end_at_ms=end.end_at_ms, carried_out_at_ms=1_790_000_005_000)
    assert first.desired_state is DesiredState.RUNNING  # stopping the bot is the runner's write
    assert again == first


def test_mark_end_carried_out_leaves_an_end_the_owner_changed_meanwhile(tmp_path: Path) -> None:
    """The Clerk records the end it carried out; an edit that landed first is not erased."""
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "x"))
    carried = BotEnd(end_at_ms=1_790_000_000_000)
    repo.set(DesiredState.RUNNING, updated_by="bot_runner", now_ms=1, end=carried)
    edited = repo.set_end(BotEnd(end_at_ms=1_790_000_600_000), updated_by="operator", now_ms=2)

    after = repo.mark_end_carried_out(carried, updated_by="account_clerk", now_ms=3)

    assert after == edited
