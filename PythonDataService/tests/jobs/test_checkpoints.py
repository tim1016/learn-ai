"""The pooled research execution mode's lease renews on accepted checkpoints only (#3034).

ADR 0065 decision 1, as amended: in the pooled mode a worker's cancellation
polls, timer ticks, status reads and public SSE emissions are observation,
not progress — the lease slides forward only on an accepted execution
checkpoint for the current generation with an increasing sequence and an
advancing work cursor (or a real dispatch/accept/durable-completion
transition). Renewal is EXPIRE, so an expired lease cannot be recreated.
Every silence bound is answered from an injected clock, never the wall.
"""

from __future__ import annotations

import pytest

from app.jobs import progress
from app.jobs.checkpoints import (
    PHASE_SILENCE_LIMIT_S,
    SIMULATION_CHECKPOINT_TARGET_S,
    CheckpointGate,
    ExecutionCheckpoint,
    TaskSilence,
)
from app.research.persistence import lifecycle
from tests.jobs.conftest import _FakeRedis

LEASE = "job:pooled-1:lease"
GENERATION = 3


def _checkpoint(
    sequence: int,
    cursor: int,
    *,
    task_id: str = "task-a",
    generation: int = GENERATION,
    kind: str = "processed_blocks",
    phase: str = "simulation",
    transition: bool = False,
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        generation=generation,
        task_id=task_id,
        sequence=sequence,
        cursor=cursor,
        cursor_kind=kind,  # type: ignore[arg-type]
        phase=phase,
        transition=transition,
        occurred_at_ms=1_000_000,
    )


def test_the_serial_defaults_are_untouched() -> None:
    """The shared cancellation check still renews the lease: only the pooled mode opts out."""
    check = progress.CancellationCheck("job-1")
    assert check.renews_lease is True


def test_a_cancellation_poll_does_not_renew_a_pooled_lease(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.job("pooled-1", "running")
    fake.lease("pooled-1")
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)

    check = progress.CancellationCheck("pooled-1", renews_lease=False)
    assert check.should_cancel() is False
    assert check.should_cancel() is False

    assert fake.expires == [], "a poll is observation: it must not slide the lease"


def test_the_read_only_check_still_observes_cancellation(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.job("pooled-1", "running")
    fake.lease("pooled-1")
    fake.hashes["job:pooled-1:state"]["cancel_requested"] = "1"
    monkeypatch.setattr(progress, "get_redis", lambda: fake)

    check = progress.CancellationCheck("pooled-1", renews_lease=False)
    assert check.should_cancel() is True
    assert fake.expires == []


def test_an_accepted_advancing_checkpoint_renews_the_lease_once(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(_checkpoint(1, 100)) is True
    assert gate.accept(_checkpoint(2, 200)) is True

    assert fake.expires == [(LEASE, gate._lease_ttl_s)] * 2


def test_repeated_sequence_cursor_polls_and_ticks_renew_nothing(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)
    assert gate.accept(_checkpoint(5, 100)) is True

    repeated_sequence = gate.accept(_checkpoint(5, 200))
    repeated_cursor = gate.accept(_checkpoint(6, 100))

    assert repeated_sequence is False
    assert repeated_cursor is False
    assert fake.expires == [(LEASE, gate._lease_ttl_s)], "only the first advancing checkpoint renewed"


def test_a_real_transition_renews_even_without_cursor_advance(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dispatch, an acceptance or a durable completion is evidence at one cursor."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)
    assert gate.accept(_checkpoint(1, 100)) is True

    assert gate.accept(_checkpoint(2, 100, kind="durable_completions", transition=True)) is True

    assert len(fake.expires) == 2


def test_an_expired_lease_cannot_be_recreated_by_a_checkpoint(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL passed: only a fresh acquire may hold a lease again, never a late checkpoint."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(_checkpoint(1, 100)) is True
    assert not fake.exists(LEASE), "EXPIRE on a missing key must not recreate the lease"


def test_stale_generation_events_are_rejected_and_renew_nothing(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    seen: list[ExecutionCheckpoint] = []
    gate = CheckpointGate("pooled-1", GENERATION, on_accepted=seen.append)

    stale_attempt = gate.accept(_checkpoint(1, 100, generation=GENERATION - 1))
    stale_task = gate.accept(_checkpoint(1, 100, generation=GENERATION + 1, transition=True))

    assert stale_attempt is False and stale_task is False
    assert seen == [] and fake.expires == []
    assert gate.last_accepted("task-a") is None, "a stale event may not seed the task's ordering state"


def test_a_current_generation_checkpoint_after_a_stale_one_is_accepted(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(_checkpoint(1, 100, generation=GENERATION - 1)) is False
    assert gate.accept(_checkpoint(2, 100)) is True


def test_each_task_owns_its_silence_deadline() -> None:
    watch = TaskSilence()
    watch.record("task-a", "simulation", at_ms=0)
    watch.record("task-b", "simulation", at_ms=0)

    watch.record("task-a", "simulation", at_ms=20_000)

    assert watch.expired("task-b", now_ms=29_999) is False
    assert watch.expired("task-b", now_ms=30_000) is False
    assert watch.expired("task-b", now_ms=30_001) is True, "task A's progress never resets hung task B's deadline"
    assert watch.expired("task-a", now_ms=30_001) is False
    assert watch.expired("task-a", now_ms=50_001) is True


def test_every_phase_bound_is_pinned() -> None:
    """Startup/queue/preparation/persistence 60 s; simulation/aggregation 30 s; catalog 35 s."""
    assert PHASE_SILENCE_LIMIT_S["startup"] == 60.0
    assert PHASE_SILENCE_LIMIT_S["queue"] == 60.0
    assert PHASE_SILENCE_LIMIT_S["preparation"] == 60.0
    assert PHASE_SILENCE_LIMIT_S["persistence"] == 60.0
    assert PHASE_SILENCE_LIMIT_S["simulation"] == 30.0
    assert PHASE_SILENCE_LIMIT_S["aggregation"] == 30.0
    assert PHASE_SILENCE_LIMIT_S["catalog"] == 35.0
    assert SIMULATION_CHECKPOINT_TARGET_S == 1.0


def test_preparation_and_catalog_deadlines_expire_on_the_injected_clock() -> None:
    watch = TaskSilence()
    watch.record("prep", "preparation", at_ms=1_000)
    watch.record("catalog", "catalog", at_ms=1_000)

    assert watch.expired("prep", now_ms=61_000) is False
    assert watch.expired("prep", now_ms=61_001) is True
    assert watch.expired("catalog", now_ms=36_000) is False
    assert watch.expired("catalog", now_ms=36_001) is True


def test_no_progress_expiry_is_visible_by_task(fake: _FakeRedis) -> None:
    """Queued work: repeated inspection of a full pool admits nothing, so the queue deadline runs out visibly."""
    watch = TaskSilence()
    watch.record("queued-1", "queue", at_ms=0)
    watch.record("running-1", "simulation", at_ms=0)

    # Inspection is not a checkpoint: nothing records, so nothing resets.
    assert watch.expired_tasks(now_ms=60_001) == ["queued-1", "running-1"]
    watch.record("running-1", "simulation", at_ms=50_000)
    assert watch.expired_tasks(now_ms=60_001) == ["queued-1"]


def test_an_unknown_phase_is_refused_rather_than_silently_unbounded() -> None:
    watch = TaskSilence()
    with pytest.raises(ValueError, match="no silence limit"):
        watch.record("task-a", "unknown-phase", at_ms=0)
