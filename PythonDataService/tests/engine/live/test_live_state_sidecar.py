"""Tests for LiveStateSidecar — order-idempotency state across restarts.

Grown vertically via TDD: each cycle adds one behavior and one minimal
slice of schema or mechanics. See plan §16.4 Resolution 3 for the
12-field target schema this module grows toward.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)


def _min_envelope(**overrides: object) -> LiveStateEnvelope:
    """Construct an envelope with the minimum required fields filled in.

    Tests override only the fields they exercise; identity-tuple defaults
    are stable so equality assertions stay readable as the schema grows.
    """
    base: dict[str, object] = {
        "strategy_instance_id": "spy_ema_crossover",
        "run_id": "run-fixture",
        "bot_order_namespace": "learn-ai/spy_ema_crossover/v1",
        "ib_client_id": 17,
        "last_processed_bar_ms": 1_748_000_000_000,
        "last_artifact_flush_ms": 1_748_000_000_500,
    }
    base.update(overrides)
    return LiveStateEnvelope(**base)  # type: ignore[arg-type]


def test_write_then_read_round_trips_strategy_instance_id(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded == env


def test_read_missing_returns_none(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "absent.json")
    assert repo.read() is None


def test_write_if_missing_creates_sidecar(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(run_id="first")

    assert repo.write_if_missing(env) is True
    assert repo.read() == env


def test_write_if_missing_preserves_existing_sidecar(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    first = _min_envelope(run_id="first")
    second = _min_envelope(run_id="second")

    repo.write(first)

    assert repo.write_if_missing(second) is False
    assert repo.read() == first


def test_round_trip_persists_identity_tuple(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(
        strategy_instance_id="spy_ema_crossover",
        run_id="run-2026-05-28-001",
        bot_order_namespace="learn-ai/spy_ema_crossover/v1",
        ib_client_id=17,
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.strategy_instance_id == "spy_ema_crossover"
    assert loaded.run_id == "run-2026-05-28-001"
    assert loaded.bot_order_namespace == "learn-ai/spy_ema_crossover/v1"
    assert loaded.ib_client_id == 17


def test_round_trip_persists_order_tracking(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(
        pending_intents=[
            {"client_order_id": "learn-ai/spy_ema_crossover/v1/3", "side": "BUY", "qty": 100},
        ],
        submitted_orders={
            "learn-ai/spy_ema_crossover/v1/2": {"perm_id": 9876543210, "status": "Submitted"},
        },
        known_perm_ids=[9876543209, 9876543210],
        known_exec_ids=["0000e0d5.6452f4c2.01.01"],
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.known_perm_ids == [9876543209, 9876543210]
    assert loaded.submitted_orders["learn-ai/spy_ema_crossover/v1/2"]["perm_id"] == 9876543210


def test_order_tracking_defaults_to_empty(tmp_path: Path) -> None:
    """Fresh cold start: no submitted orders, no intents, no known ids."""
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.pending_intents == []
    assert loaded.submitted_orders == {}
    assert loaded.known_perm_ids == []
    assert loaded.known_exec_ids == []


def test_round_trip_persists_position_and_bar_cursors(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(
        expected_position_by_symbol={"SPY": 100},
        last_processed_bar_ms=1_748_000_000_000,
        last_artifact_flush_ms=1_748_000_001_500,
    )
    repo.write(env)
    loaded = repo.read()
    assert loaded == env
    assert loaded is not None
    assert loaded.expected_position_by_symbol == {"SPY": 100}
    assert loaded.last_processed_bar_ms == 1_748_000_000_000
    assert loaded.last_artifact_flush_ms == 1_748_000_001_500


def test_poisoned_reason_defaults_to_none(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope()
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.poisoned_reason is None


def test_round_trip_persists_poisoned_reason(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    env = _min_envelope(poisoned_reason="unexpected_order_at_broker")
    repo.write(env)
    loaded = repo.read()
    assert loaded is not None
    assert loaded.poisoned_reason == "unexpected_order_at_broker"


def test_failed_rename_preserves_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic-write invariant: a crash between tempfile write and rename
    must leave the live path unchanged. The previous envelope is what
    a subsequent read() returns.
    """
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    repo.write(_min_envelope(run_id="alpha"))

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        repo.write(_min_envelope(run_id="beta"))
    monkeypatch.undo()

    loaded = repo.read()
    assert loaded is not None
    assert loaded.run_id == "alpha"
    # No orphan .tmp left lying around after the failed write either.
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"orphan tmp files after failed rename: {tmp_files}"


@pytest.mark.skipif(sys.platform == "win32", reason="parent-dir fsync is POSIX-only")
def test_write_fsyncs_parent_directory_for_rename_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync of the tempfile flushes its contents to disk, but on POSIX
    the rename's directory entry can still be lost on power loss
    unless the parent directory is also fsynced. Otherwise the old
    sidecar can reappear after crash, rolling cursors/known ids
    backward and opening a double-submit window.
    """
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    repo.write(_min_envelope())

    # At minimum: tempfile fd + parent-dir fd. fd numbers may be reused
    # by the kernel after close so we don't assert distinct values, just
    # the count.
    assert len(fsync_calls) >= 2, (
        f"expected at least 2 fsync calls (tempfile + parent dir), got {len(fsync_calls)}"
    )


def test_successful_write_leaves_no_tmp_artifact(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    repo.write(_min_envelope())
    assert list(tmp_path.glob("*.tmp")) == []


def test_unparseable_json_raises_corrupt_error(tmp_path: Path) -> None:
    path = tmp_path / "live_state.json"
    path.write_text("{ not json", encoding="utf-8")
    repo = LiveStateSidecarRepo(path)
    with pytest.raises(LiveStateSidecarCorruptError):
        repo.read()


def test_schema_violation_raises_corrupt_error(tmp_path: Path) -> None:
    path = tmp_path / "live_state.json"
    path.write_text('{"strategy_instance_id": "x"}', encoding="utf-8")  # missing required fields
    repo = LiveStateSidecarRepo(path)
    with pytest.raises(LiveStateSidecarCorruptError):
        repo.read()


def test_corrupt_error_carries_path(tmp_path: Path) -> None:
    path = tmp_path / "live_state.json"
    path.write_text("garbage", encoding="utf-8")
    repo = LiveStateSidecarRepo(path)
    try:
        repo.read()
    except LiveStateSidecarCorruptError as exc:
        assert exc.path == path
    else:
        pytest.fail("expected LiveStateSidecarCorruptError")


def test_stable_live_state_path_layout(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    resolved = stable_live_state_path(artifacts_root, "spy_ema_crossover")
    assert resolved == artifacts_root / "live_state" / "spy_ema_crossover" / "live_state.json"


def test_stable_path_keys_directory_on_strategy_instance_id(tmp_path: Path) -> None:
    """Two strategy instances must not collide on disk."""
    artifacts_root = tmp_path / "artifacts"
    ema = stable_live_state_path(artifacts_root, "spy_ema_crossover")
    vwap = stable_live_state_path(artifacts_root, "spy_vwap_reversion_1min")
    assert ema.parent != vwap.parent


@pytest.mark.parametrize("bad_sid", ["../escape", "nested/id", "", " spy", ".", ".."])
def test_stable_live_state_path_rejects_unsafe_strategy_instance_id(
    tmp_path: Path, bad_sid: str
) -> None:
    with pytest.raises(ValueError):
        stable_live_state_path(tmp_path / "artifacts", bad_sid)


def test_update_after_flush_holds_lock_across_full_rmw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_after_flush's read-modify-write must hold the lock across
    the full sequence.

    Bug being closed: read() happens before _file_lock is acquired;
    a concurrent write() that writes v1 between this method's read
    and write causes the cursor-advanced v0 to clobber v1, losing
    the writer's submitted_orders / pending_intents update.

    Test injects a 100ms delay after read() to force the race window
    deterministically. The writer thread bypasses the slow read so its
    timing is controlled. Without the fix, the writer's pending_intent
    is silently lost; with the fix, it is preserved (even though the
    updater's cursor advance is then lost — that is the unavoidable
    cost of blind-write semantics outside the lock).
    """
    import threading
    import time

    from app.engine.live import live_state_sidecar as lss

    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    repo.write(_min_envelope(pending_intents=[]))

    real_read = lss.LiveStateSidecarRepo.read

    def slow_read(self: lss.LiveStateSidecarRepo) -> lss.LiveStateEnvelope | None:
        result = real_read(self)
        time.sleep(0.1)
        return result

    monkeypatch.setattr(lss.LiveStateSidecarRepo, "read", slow_read)

    def updater() -> None:
        repo.update_after_flush(
            last_processed_bar_ms=2_000_000_000_000,
            last_artifact_flush_ms=2_000_000_000_500,
        )

    def writer() -> None:
        time.sleep(0.03)  # let updater's read+sleep start first
        env = real_read(repo)  # bypass the slow read so writer's timing is tight
        assert env is not None
        repo.write(env.model_copy(update={"pending_intents": [{"id": "DURING_RMW"}]}))

    t_updater = threading.Thread(target=updater)
    t_writer = threading.Thread(target=writer)
    t_updater.start()
    t_writer.start()
    t_updater.join()
    t_writer.join()

    monkeypatch.undo()
    final = repo.read()
    assert final is not None
    intent_ids = [i.get("id") for i in final.pending_intents]
    assert "DURING_RMW" in intent_ids, (
        f"writer's pending_intent was clobbered by stale update_after_flush RMW; "
        f"got pending_intents={final.pending_intents}"
    )


def test_update_after_flush_advances_cursors_and_preserves_rest(tmp_path: Path) -> None:
    repo = LiveStateSidecarRepo(tmp_path / "live_state.json")
    repo.write(
        _min_envelope(
            run_id="r1",
            submitted_orders={"learn-ai/spy_ema_crossover/v1/2": {"perm_id": 42}},
            known_perm_ids=[42],
            expected_position_by_symbol={"SPY": 100},
            last_processed_bar_ms=1_748_000_000_000,
            last_artifact_flush_ms=1_748_000_000_500,
        )
    )
    repo.update_after_flush(
        last_processed_bar_ms=1_748_000_900_000,
        last_artifact_flush_ms=1_748_000_901_500,
    )
    loaded = repo.read()
    assert loaded is not None
    assert loaded.last_processed_bar_ms == 1_748_000_900_000
    assert loaded.last_artifact_flush_ms == 1_748_000_901_500
    # Every other field preserved verbatim.
    assert loaded.run_id == "r1"
    assert loaded.submitted_orders == {"learn-ai/spy_ema_crossover/v1/2": {"perm_id": 42}}
    assert loaded.known_perm_ids == [42]
    assert loaded.expected_position_by_symbol == {"SPY": 100}


def test_concurrent_writers_serialize_without_error(tmp_path: Path) -> None:
    """Two threads writing the same sidecar must not race on the shared
    tempfile name. Without serialisation the loser's os.replace finds
    the tempfile already renamed by the winner and raises
    FileNotFoundError. With the advisory lock, writes take turns and
    the final on-disk envelope is one of the two writers' content.
    """
    import threading

    path = tmp_path / "live_state.json"
    repo = LiveStateSidecarRepo(path)
    repo.write(_min_envelope(run_id="seed"))

    errors: list[BaseException] = []

    def writer(label: str) -> None:
        try:
            for _ in range(50):
                repo.write(_min_envelope(run_id=label))
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("alpha",))
    t2 = threading.Thread(target=writer, args=("beta",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"concurrent writers raised: {errors!r}"
    loaded = repo.read()
    assert loaded is not None
    assert loaded.run_id in {"alpha", "beta"}


def test_concurrent_write_if_missing_keeps_first_seed(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "live_state.json"
    repo = LiveStateSidecarRepo(path)
    barrier = threading.Barrier(3)
    successes: list[str] = []
    errors: list[BaseException] = []

    def seed(label: str) -> None:
        try:
            barrier.wait()
            if repo.write_if_missing(_min_envelope(run_id=label)):
                successes.append(label)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=seed, args=("alpha",))
    t2 = threading.Thread(target=seed, args=("beta",))
    t1.start()
    t2.start()
    barrier.wait()
    t1.join()
    t2.join()

    assert errors == []
    assert len(successes) == 1
    loaded = repo.read()
    assert loaded is not None
    assert loaded.run_id == successes[0]
