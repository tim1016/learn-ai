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
import redis

from app.jobs import progress
from app.jobs.checkpoints import (
    PHASE_SILENCE_LIMIT_S,
    SIMULATION_CHECKPOINT_TARGET_S,
    CheckpointGate,
    CursorKind,
    ExecutionCheckpoint,
    TaskSilence,
)
from app.jobs.progress import JOB_TTL_SECONDS
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
    kind: CursorKind = "processed_blocks",
    phase: str = "simulation",
    transition: bool = False,
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        generation=generation,
        task_id=task_id,
        sequence=sequence,
        cursor=cursor,
        cursor_kind=kind,
        phase=phase,
        transition=transition,
        occurred_at_ms=1_000_000,
    )


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


@pytest.mark.parametrize(
    ("first", "second", "first_accepted", "second_accepted"),
    [
        pytest.param(
            _checkpoint(1, 100),
            _checkpoint(2, 200),
            True,
            True,
            id="an_accepted_advancing_checkpoint_renews_the_lease",
        ),
        # A dispatch, an acceptance or a durable completion is evidence at one cursor.
        pytest.param(
            _checkpoint(1, 100),
            _checkpoint(2, 100, kind="durable_completions", transition=True),
            True,
            True,
            id="a_real_transition_renews_even_without_cursor_advance",
        ),
        pytest.param(
            _checkpoint(1, 100, generation=GENERATION - 1),
            _checkpoint(2, 100),
            False,
            True,
            id="a_current_generation_checkpoint_after_a_stale_one_is_accepted",
        ),
    ],
)
def test_each_accepted_checkpoint_renews_the_lease_once_and_a_rejected_one_renews_nothing(
    fake: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    first: ExecutionCheckpoint,
    second: ExecutionCheckpoint,
    first_accepted: bool,
    second_accepted: bool,
) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(first) is first_accepted, "the first checkpoint is judged as the row says"
    assert gate.accept(second) is second_accepted, "the second checkpoint is judged as the row says"
    assert fake.expires == [(LEASE, gate._lease_ttl_s)] * [first_accepted, second_accepted].count(True), (
        "each accepted checkpoint renews the lease once and a rejected one renews nothing"
    )


@pytest.mark.parametrize(
    ("first", "second", "third", "first_accepted", "second_accepted", "third_accepted"),
    [
        # Only the first advancing checkpoint renews: a repeated sequence or a repeated cursor is a poll or a tick.
        pytest.param(
            _checkpoint(5, 100),
            _checkpoint(5, 200),
            _checkpoint(6, 100),
            True,
            False,
            False,
            id="repeated_sequence_cursor_polls_and_ticks_renew_nothing",
        ),
        # Cursor values are kind-local: blocks cursor 1 is past the artifacts ground, not behind it.
        pytest.param(
            _checkpoint(1, 12, kind="admitted_artifacts"),
            _checkpoint(2, 1, kind="processed_blocks"),
            _checkpoint(3, 1, kind="processed_blocks"),
            True,
            True,
            False,
            id="cursor_ground_is_per_kind_not_per_task",
        ),
    ],
)
def test_the_gate_judges_a_run_of_three_checkpoints_and_renews_once_per_accepted_one(
    fake: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    first: ExecutionCheckpoint,
    second: ExecutionCheckpoint,
    third: ExecutionCheckpoint,
    first_accepted: bool,
    second_accepted: bool,
    third_accepted: bool,
) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(first) is first_accepted, "the first checkpoint is judged as the row says"
    assert gate.accept(second) is second_accepted, "the second checkpoint is judged as the row says"
    assert gate.accept(third) is third_accepted, "the third checkpoint is judged as the row says"
    assert fake.expires == [(LEASE, gate._lease_ttl_s)] * [first_accepted, second_accepted, third_accepted].count(True), (
        "each accepted checkpoint renews the lease once and a rejected one renews nothing"
    )


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
    assert gate.last_accepted("task-a", "processed_blocks") is None, "a stale event may not seed the task's ordering state"


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


def test_every_phase_class_boundary_is_checked_on_the_injected_clock() -> None:
    watch = TaskSilence()
    for phase, limit in PHASE_SILENCE_LIMIT_S.items():
        watch.record(phase, phase, at_ms=1_000)
        assert watch.expired(phase, now_ms=1_000 + limit * 1000) is False
        assert watch.expired(phase, now_ms=1_000 + limit * 1000 + 1) is True


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


def test_a_store_failure_consumes_nothing(fake, monkeypatch) -> None:
    """A Redis hiccup neither consumes the sequence nor fails the work path; the retry renews."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    class Unreachable:
        def expire(self, *_args):
            raise redis.ConnectionError("connection refused")

    monkeypatch.setattr(progress, "get_redis", lambda: Unreachable())
    assert gate.accept(_checkpoint(1, 100)) is False, "the checkpoint is not accepted while the store cannot hear it"
    assert gate.last_accepted("task-a", "processed_blocks") is None, "the failed renewal recorded no ordering state"

    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    assert gate.accept(_checkpoint(1, 100)) is True, "retrying the same checkpoint after recovery renews it"
    assert fake.expires == [(LEASE, gate._lease_ttl_s)]


def test_a_pooled_emitter_renews_nothing(fake, monkeypatch) -> None:
    """The public SSE event is presentation: a pooled-mode emit must not slide the lease."""
    from app.jobs.progress import ProgressEmitter

    fake.job("pooled-1", "running")
    fake.lease("pooled-1")
    emitter = ProgressEmitter("pooled-1", renews_lease=False)
    emitter.log("retry")

    assert fake.expires == [("job:pooled-1:events", JOB_TTL_SECONDS)], (
        "only the stream TTL slides; the lease does not"
    )


def test_a_first_non_transition_cursor_at_the_ground_renews_nothing(fake, monkeypatch) -> None:
    """Cursor zero is the kind's ground: no work advanced, so the checkpoint is not evidence (#3041 review)."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(_checkpoint(1, 0)) is False
    assert gate.accept(_checkpoint(1, -5)) is False
    assert fake.expires == [] and gate.last_accepted("task-a", "processed_blocks") is None

    assert gate.accept(_checkpoint(2, 1)) is True, "the first real unit of work past the ground is evidence"


def test_a_transition_never_lowers_the_cursor_watermark(fake, monkeypatch) -> None:
    """A transition may land at any cursor, but a later ordinary checkpoint reads against the high-water mark (#3041 review)."""
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    gate = CheckpointGate("pooled-1", GENERATION)

    assert gate.accept(_checkpoint(1, 100)) is True
    assert gate.accept(_checkpoint(2, 3, transition=True)) is True, "the transition is evidence at its own cursor"

    below = gate.accept(_checkpoint(3, 50))
    at_watermark = gate.accept(_checkpoint(4, 100))

    assert below is False and at_watermark is False, "cursors below the preserved watermark 100 advance nothing"
    assert gate.accept(_checkpoint(5, 101)) is True


def test_a_forgotten_task_stops_policing() -> None:
    """A durably completed task leaves the silence watch; it must not read hung beside its running sibling (#3041 review)."""
    watch = TaskSilence()
    watch.record("done-1", "simulation", at_ms=0)
    watch.record("running-1", "simulation", at_ms=0)

    watch.forget("done-1")
    assert watch.seconds_silent("done-1", now_ms=60_000) is None
    assert watch.expired_tasks(now_ms=60_001) == ["running-1"], "the finished task is not returned as hung"
