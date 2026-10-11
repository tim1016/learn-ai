"""Execution checkpoints: the only evidence a pooled research worker's lease renews on.

ADR 0065 decision 1 renews the serial job modes' lease on every emit and
every gated cancellation check — the calls those workers already make when
they make progress. The pooled research execution mode (PRD #3034) cannot
ride that rule: a worker's cancellation polls, timer ticks, status reads and
public SSE emissions prove neither that the task holding the lease is
executing nor that any work advanced, and a hung task must read as dead even
while its siblings report progress. This module is that mode's contract.

The lease slides forward only on an **accepted checkpoint**: a real
dispatch, acceptance or durable-completion transition, or a work cursor
that advanced — admitted artifacts, processed bar/work blocks, durable
completions — for the current job/attempt/task generation, with an
increasing sequence. Rejected events (a stale generation, a repeated
sequence or cursor, a timer tick, a pool-fullness inspection that queued
nothing) renew nothing and mutate no progress.

Renewal is ``EXPIRE`` on the lease key and nothing else: an expired lease
cannot be recreated by a late checkpoint, so liveness never flips back on
mid-flight (ADR 0065 decision 1). The accepted checkpoint is handed to the
adapter callback the job layer supplies — coarse progress for the existing
SSE vocabulary — never one stream event per bar.

The silence deadlines are plain data plus an injected clock, so the bounds
are testable without waiting: startup/queue/preparation/persistence allow
60 s of silence, simulation/aggregation 30 s, catalog calls 35 s (the
admission path's own timeout), and a simulation checkpoint is targeted to
land within one second of the work step. Each active task owns its own
deadline: task A's checkpoint never resets hung task B's.

Canonical implementation: this file.
Validated against: tests/jobs/test_checkpoints.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.jobs import progress
from app.jobs.progress import JOB_LEASE_TTL_SECONDS

CursorKind = Literal["admitted_artifacts", "processed_bars", "processed_blocks", "durable_completions"]

# How long each phase class may stay silent before the worker reads as hung.
# The runtime (not this module) owns hard termination; these bounds are what
# it polices. ``catalog`` mirrors the admission path's own 35 s call bound:
# a catalog call that outlives it is a hang, not a slow query.
PHASE_SILENCE_LIMIT_S: dict[str, float] = {
    "startup": 60.0,
    "queue": 60.0,
    "preparation": 60.0,
    "persistence": 60.0,
    "simulation": 30.0,
    "aggregation": 30.0,
    "catalog": 35.0,
}

# A simulation checkpoint should land within one second of the work step it
# reports; the runtime's dispatch path is sized against this target.
SIMULATION_CHECKPOINT_TARGET_S = 1.0


@dataclass(frozen=True)
class ExecutionCheckpoint:
    """One real step of pooled execution, as the worker reports it.

    ``transition`` marks a dispatch/acceptance/durable-completion boundary —
    an event that is evidence even though no work cursor moved. Every other
    checkpoint must advance ``cursor`` to be accepted. ``occurred_at_ms`` is
    when the work step happened, not when the report was made.
    """

    generation: int
    task_id: str
    sequence: int
    cursor: int
    cursor_kind: CursorKind
    phase: str
    transition: bool = False
    occurred_at_ms: int = 0


class CheckpointGate:
    """Admits one job's checkpoints and renews its lease on the accepted ones.

    The gate holds the current generation (the job's attempt/task generation
    under the fence). A checkpoint whose generation differs is stale: it is
    rejected without renewing or recording, so a superseded worker cannot
    keep a lease it no longer holds alive.
    """

    def __init__(
        self,
        job_id: str,
        generation: int,
        *,
        lease_ttl_s: int = JOB_LEASE_TTL_SECONDS,
        on_accepted: Callable[[ExecutionCheckpoint], None] = lambda checkpoint: None,
    ) -> None:
        self.job_id = job_id
        self.generation = generation
        self._lease_ttl_s = lease_ttl_s
        self._on_accepted = on_accepted
        # Per task: the highest accepted (sequence, cursor) pair.
        self._accepted: dict[str, tuple[int, int]] = {}

    def accept(self, checkpoint: ExecutionCheckpoint) -> bool:
        """Admit ``checkpoint`` if it is current, ordered and advancing; renew once when it is.

        Rejected without renewal: a stale generation, a sequence that does not
        advance past the task's last accepted one, and a non-transition
        checkpoint whose cursor did not advance. Acceptance renews the lease
        exactly once and hands the checkpoint to the adapter callback.
        """
        if checkpoint.generation != self.generation:
            return False
        prior = self._accepted.get(checkpoint.task_id)
        if prior is not None and checkpoint.sequence <= prior[0]:
            return False
        if prior is not None and not checkpoint.transition and checkpoint.cursor <= prior[1]:
            return False
        self._accepted[checkpoint.task_id] = (checkpoint.sequence, checkpoint.cursor)
        # EXPIRE only: a lease that already expired must not be recreated.
        progress.get_redis().expire(_lease_key(self.job_id), self._lease_ttl_s)
        self._on_accepted(checkpoint)
        return True

    def last_accepted(self, task_id: str) -> tuple[int, int] | None:
        """The task's highest accepted ``(sequence, cursor)``, or ``None`` before its first."""
        return self._accepted.get(task_id)


def _lease_key(job_id: str) -> str:
    return f"job:{job_id}:lease"


class TaskSilence:
    """Per-task silence deadlines by phase class, against an injected clock.

    ``record`` stamps the named task's deadline from a real event; the
    queries answer from ``now_ms`` the caller supplies. Deadlines are per
    task: progress from task A never resets task B's clock, so one hung task
    expires while its siblings report.
    """

    def __init__(self, *, limits_s: dict[str, float] | None = None) -> None:
        self._limits_s = limits_s if limits_s is not None else PHASE_SILENCE_LIMIT_S
        # task_id -> (phase, deadline_ms)
        self._deadlines: dict[str, tuple[str, float]] = {}

    def record(self, task_id: str, phase: str, *, at_ms: int) -> float:
        """Stamp ``task_id``'s deadline for ``phase`` from a real event; returns the new deadline in ms."""
        limit_s = self._limits_s.get(phase)
        if limit_s is None:
            raise ValueError(f"no silence limit for phase {phase!r}")
        deadline_ms = at_ms + limit_s * 1000
        self._deadlines[task_id] = (phase, deadline_ms)
        return deadline_ms

    def seconds_silent(self, task_id: str, *, now_ms: int) -> float | None:
        """How far past its deadline the task is, in seconds; 0.0 while within it; ``None`` if never stamped."""
        stamped = self._deadlines.get(task_id)
        if stamped is None:
            return None
        _, deadline_ms = stamped
        return max(0.0, (now_ms - deadline_ms) / 1000)

    def expired(self, task_id: str, *, now_ms: int) -> bool:
        """Whether the task's phase silence deadline has passed."""
        silence = self.seconds_silent(task_id, now_ms=now_ms)
        return silence is not None and silence > 0.0

    def expired_tasks(self, *, now_ms: int) -> list[str]:
        """Every stamped task past its deadline, in stamp order — the no-progress expiry the runtime acts on."""
        return [task_id for task_id in self._deadlines if self.expired(task_id, now_ms=now_ms)]
