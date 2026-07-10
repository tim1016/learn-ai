"""PRD #619-D1 — durable ``mutation_attempt`` record + pure state machine.

The 619-C5 work surfaces ``OUTCOME_UNKNOWN`` synchronously on the
mutation response.  This module is the *durable* half: every
``start`` / ``stop`` / ``flatten`` / ``resume`` / ``pause`` mutation
the data plane attempts is written to a per-attempt JSON artifact
**before** the HTTP POST leaves the process.  When the response
arrives (or fails to), the attempt transitions to either
``RESPONSE_CONFIRMED`` or ``OUTCOME_UNKNOWN``.  D3's Reconcile action
later joins evidence and transitions to one of the terminal effect
states.

The repository is intentionally small.  It owns durable storage,
nothing else.  Three operations are public:

- ``MutationAttemptRepo.write(attempt)`` — atomic ``tmp + fsync +
  replace`` per the same pattern used by ``engine_runtime.py``.
- ``MutationAttemptRepo.read(attempt_id)`` — direct path lookup;
  ``None`` on missing / malformed / forward-incompatible.
- ``MutationAttemptRepo.latest_for(instance_id)`` — most-recent
  attempt for the instance by ``requested_at_ms``; ``None`` when no
  attempts exist.

The PRD notes ``mutation_attempt_id`` is **audit-only** in 619-D —
the daemon does not yet enforce it as an idempotency key.  Persisting
it now means the C5 surfacing pass can be promoted from synchronous-
only to durable in D2 without a storage migration.

The state machine is a separate concern.  ``transition_attempt`` is
**pure**: it returns a new ``MutationAttempt`` and never touches disk.
The router writes before each transition.  Illegal transitions raise
``InvalidMutationTransitionError`` rather than silently coerce — the
422 path is documented and tested.

``reconcile_mutation_effect`` (619-D3) is the **pure** evidence
classifier: it takes a ``MutationAttempt`` and a typed
``ReconciliationEvidence`` snapshot and returns one of the four
``ReconciliationOutcome`` literals.  The router assembles the
evidence (daemon process state, child engine_runtime, broker
positions, durable desired_state) and then folds the outcome into
the attempt via ``transition_attempt``.  Reconcile is **read-only**
— it never replays the mutation.

All timestamps are ``int64`` ms UTC per ``.claude/rules/numerical-
rigor.md``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact

ActionName = Literal["start", "stop", "flatten", "resume", "pause"]

_CREATION_ORDER_LOCK = threading.Lock()
_CREATION_ORDER_NEXT: dict[tuple[Path, str], int] = {}

DispatchState = Literal[
    "PREPARED",
    "DISPATCHING",
    "RESPONSE_CONFIRMED",
    "OUTCOME_UNKNOWN",
    "EFFECT_CONFIRMED",
    "EFFECT_NOT_OBSERVED",
    "NOT_PROVABLE",
    "EVIDENCE_CONFLICT",
]


# State-machine legality table.  Keys are source states; values are the
# set of legal successor states.  Terminal states map to an empty set so
# any transition out of them is illegal.
_LEGAL_TRANSITIONS: dict[DispatchState, frozenset[DispatchState]] = {
    "PREPARED": frozenset({"DISPATCHING"}),
    "DISPATCHING": frozenset({"RESPONSE_CONFIRMED", "OUTCOME_UNKNOWN"}),
    "RESPONSE_CONFIRMED": frozenset({"EFFECT_CONFIRMED", "EFFECT_NOT_OBSERVED", "NOT_PROVABLE", "EVIDENCE_CONFLICT"}),
    "OUTCOME_UNKNOWN": frozenset({"EFFECT_CONFIRMED", "EFFECT_NOT_OBSERVED", "NOT_PROVABLE", "EVIDENCE_CONFLICT"}),
    "EFFECT_CONFIRMED": frozenset(),
    "EFFECT_NOT_OBSERVED": frozenset(),
    "NOT_PROVABLE": frozenset(),
    "EVIDENCE_CONFLICT": frozenset(),
}


TERMINAL_STATES: frozenset[DispatchState] = frozenset(
    state for state, successors in _LEGAL_TRANSITIONS.items() if not successors
)


class InvalidMutationTransitionError(ValueError):
    """Raised when ``transition_attempt`` is called with an illegal pair.

    The router translates this to HTTP 422 / a structured operator
    surface.  The exception's ``current_state`` / ``requested_state``
    fields let the surfacing pass include them in the response body
    without re-parsing the message text.
    """

    def __init__(self, *, current_state: DispatchState, requested_state: DispatchState) -> None:
        super().__init__(f"illegal mutation_attempt transition: {current_state} -> {requested_state}")
        self.current_state = current_state
        self.requested_state = requested_state


class MutationAttempt(BaseModel):
    """Durable per-mutation record.

    ``mutation_attempt_id`` is the caller-supplied stable identity for
    one mutation.  ``schema_version`` participates in the artifact-IO
    forward-compatibility contract (a reader that sees a higher version
    surfaces the file as ``None`` per ``read_pydantic_artifact``).

    ``outcome`` and ``evidence`` are intentionally typed as ``dict`` so
    the contract is open to D3's Reconcile shapes (which want
    ``daemon_boot_id``, ``engine_runtime_seq``, ``broker_position_set``,
    etc.) without a schema bump.  Both default to ``None``: ``outcome``
    is populated when the HTTP response arrives; ``evidence`` when
    Reconcile joins facts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)

    mutation_attempt_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    run_id: str | None = None
    action: ActionName

    requested_at_ms: int = Field(ge=0)
    creation_order: int = Field(default=0, ge=0)
    last_transition_at_ms: int = Field(ge=0)

    dispatch_state: DispatchState

    outcome: dict | None = None
    evidence: dict | None = None


def transition_attempt(
    attempt: MutationAttempt,
    new_state: DispatchState,
    *,
    transitioned_at_ms: int,
    outcome: dict | None = None,
    evidence: dict | None = None,
) -> MutationAttempt:
    """Return a new ``MutationAttempt`` advanced to ``new_state``.

    Pure: never touches disk.  The caller is responsible for writing
    the returned record through ``MutationAttemptRepo.write`` before
    the side-effect (HTTP POST, Reconcile evidence emit) the transition
    represents takes hold.

    Raises ``InvalidMutationTransitionError`` if ``new_state`` is not in
    the legal successor set of ``attempt.dispatch_state``.  Same-state
    transitions are illegal even when the source is non-terminal — a
    re-write that doesn't change the state should not increment
    ``last_transition_at_ms``.

    ``outcome`` and ``evidence``, when provided, replace whatever was
    on the source attempt for that slot.  Leaving them ``None`` keeps
    the source value (so a ``DISPATCHING → RESPONSE_CONFIRMED`` write
    that doesn't yet know the outcome can pass ``None`` and the existing
    ``outcome`` field — likely ``None`` on a brand-new attempt —
    remains).
    """
    legal = _LEGAL_TRANSITIONS[attempt.dispatch_state]
    if new_state not in legal:
        raise InvalidMutationTransitionError(current_state=attempt.dispatch_state, requested_state=new_state)
    return attempt.model_copy(
        update={
            "dispatch_state": new_state,
            "last_transition_at_ms": transitioned_at_ms,
            "outcome": outcome if outcome is not None else attempt.outcome,
            "evidence": evidence if evidence is not None else attempt.evidence,
        }
    )


class MutationAttemptRepo:
    """Atomic durable storage for ``MutationAttempt`` records.

    Each attempt is one file at ``<root>/<attempt_id>.json``.  Flat
    layout intentionally — ``latest_for(instance_id)`` scans and
    filters; the operator's mutation rate is tens-per-day per instance,
    not thousands.  When that ceases to hold, the scan becomes the
    obvious bottleneck and earns a per-instance index.

    The writer uses the same ``tmp + fsync + replace`` pattern as
    ``write_engine_runtime_snapshot`` — partial reads cannot observe a
    torn file.  The repo is filesystem-coupled but does not hold any
    in-memory state: two repo instances pointing at the same root are
    operationally equivalent, useful for the FastAPI request-scoped
    singleton pattern.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, attempt_id: str) -> Path:
        return self._root / f"{attempt_id}.json"

    def write(self, attempt: MutationAttempt) -> None:
        """Atomically persist ``attempt`` keyed by ``mutation_attempt_id``.

        Overwrites a prior record at the same path — the state machine
        relies on this to advance an attempt's ``dispatch_state`` in
        place rather than appending.  Callers that need an audit log
        should mirror writes to a separate JSONL.
        """
        atomic_write_pydantic_artifact(self._path_for(attempt.mutation_attempt_id), attempt)

    def read(self, attempt_id: str) -> MutationAttempt | None:
        """Return the attempt at ``<root>/<attempt_id>.json`` or ``None``.

        Delegates the four fail-closed guards (missing, OSError,
        malformed, forward-incompatible) to ``read_pydantic_artifact``.
        """
        return read_pydantic_artifact(self._path_for(attempt_id), MutationAttempt)

    def latest_for(self, instance_id: str) -> MutationAttempt | None:
        """Return the most recent attempt for ``instance_id``.

        Most-recent is ordered by requested time then the durable per-instance
        creation order (never completion time or file mtime). Returns ``None``
        when no attempts exist for the instance or the storage root is absent.
        """
        if not self._root.exists():
            return None
        best: MutationAttempt | None = None
        for entry in self._root.iterdir():
            if entry.suffix != ".json":
                continue
            attempt = read_pydantic_artifact(entry, MutationAttempt)
            if attempt is None or attempt.instance_id != instance_id:
                continue
            if best is None or (
                attempt.requested_at_ms,
                attempt.creation_order,
            ) > (
                best.requested_at_ms,
                best.creation_order,
            ):
                best = attempt
        return best

    def next_creation_order(self, instance_id: str) -> int:
        """Allocate a process-serialized durable order for one instance."""

        with _CREATION_ORDER_LOCK:
            key = (self._root.resolve(strict=False), instance_id)
            cached = _CREATION_ORDER_NEXT.get(key)
            if cached is not None:
                _CREATION_ORDER_NEXT[key] = cached + 1
                return cached
            highest = 0
            if self._root.exists():
                for entry in self._root.iterdir():
                    if entry.suffix != ".json":
                        continue
                    attempt = read_pydantic_artifact(entry, MutationAttempt)
                    if attempt is not None and attempt.instance_id == instance_id:
                        highest = max(highest, attempt.creation_order)
            allocated = highest + 1
            _CREATION_ORDER_NEXT[key] = allocated + 1
            return allocated

    def recover_inflight(self, *, transitioned_at_ms: int) -> list[MutationAttempt]:
        """Mark attempts stranded by a prior process exit as outcome unknown."""

        recovered: list[MutationAttempt] = []
        if not self._root.exists():
            return recovered
        for entry in sorted(self._root.iterdir()):
            if entry.suffix != ".json":
                continue
            attempt = read_pydantic_artifact(entry, MutationAttempt)
            if attempt is None or attempt.dispatch_state not in {"PREPARED", "DISPATCHING"}:
                continue
            if attempt.dispatch_state == "PREPARED":
                attempt = transition_attempt(
                    attempt,
                    "DISPATCHING",
                    transitioned_at_ms=max(transitioned_at_ms, attempt.last_transition_at_ms),
                )
            recovered_attempt = transition_attempt(
                attempt,
                "OUTCOME_UNKNOWN",
                transitioned_at_ms=max(transitioned_at_ms, attempt.last_transition_at_ms),
                outcome={"stage": "data_plane_restart_recovery"},
            )
            self.write(recovered_attempt)
            recovered.append(recovered_attempt)
        return recovered


def begin_mutation_attempt(
    repo: MutationAttemptRepo,
    *,
    instance_id: str,
    action: ActionName,
    requested_at_ms: int,
    run_id: str | None = None,
) -> MutationAttempt:
    """Persist PREPARED then DISPATCHING before the mutation side effect."""

    prepared = MutationAttempt(
        mutation_attempt_id=f"mutation-{uuid4().hex}",
        instance_id=instance_id,
        run_id=run_id,
        action=action,
        requested_at_ms=requested_at_ms,
        creation_order=repo.next_creation_order(instance_id),
        last_transition_at_ms=requested_at_ms,
        dispatch_state="PREPARED",
    )
    repo.write(prepared)
    dispatching = transition_attempt(
        prepared,
        "DISPATCHING",
        transitioned_at_ms=requested_at_ms,
    )
    repo.write(dispatching)
    return dispatching


def persist_attempt_transition(
    repo: MutationAttemptRepo,
    attempt: MutationAttempt,
    new_state: DispatchState,
    *,
    transitioned_at_ms: int,
    outcome: dict | None = None,
    evidence: dict | None = None,
) -> MutationAttempt:
    """Advance and persist one legal mutation-attempt transition."""

    advanced = transition_attempt(
        attempt,
        new_state,
        transitioned_at_ms=transitioned_at_ms,
        outcome=outcome,
        evidence=evidence,
    )
    repo.write(advanced)
    return advanced


class MutationAttemptScope:
    """Make every exceptional exit after begin durably outcome-unknown."""

    def __init__(
        self,
        repo: MutationAttemptRepo,
        attempt: MutationAttempt,
        *,
        now_ms: Callable[[], int],
    ) -> None:
        self.repo = repo
        self.attempt = attempt
        self.stage = "mutation_dispatch"
        self._now_ms = now_ms

    @classmethod
    def begin(
        cls,
        repo: MutationAttemptRepo,
        *,
        instance_id: str,
        action: ActionName,
        requested_at_ms: int,
        run_id: str | None,
        now_ms: Callable[[], int],
    ) -> MutationAttemptScope:
        return cls(
            repo,
            begin_mutation_attempt(
                repo,
                instance_id=instance_id,
                action=action,
                requested_at_ms=requested_at_ms,
                run_id=run_id,
            ),
            now_ms=now_ms,
        )

    def __enter__(self) -> MutationAttemptScope:
        return self

    def __exit__(self, _exc_type, exc, _traceback) -> bool:
        if exc is not None and self.attempt.dispatch_state == "DISPATCHING":
            self.unknown(error=exc)
        return False

    def confirm(self, *, outcome: dict) -> MutationAttempt:
        self.attempt = persist_attempt_transition(
            self.repo,
            self.attempt,
            "RESPONSE_CONFIRMED",
            transitioned_at_ms=max(self.attempt.last_transition_at_ms, self._now_ms()),
            outcome=outcome,
        )
        return self.attempt

    def reject_not_observed(self, *, outcome: dict) -> MutationAttempt:
        self.confirm(outcome=outcome)
        self.attempt = persist_attempt_transition(
            self.repo,
            self.attempt,
            "EFFECT_NOT_OBSERVED",
            transitioned_at_ms=max(self.attempt.last_transition_at_ms, self._now_ms()),
        )
        return self.attempt

    def unknown(self, *, error: BaseException) -> MutationAttempt:
        self.attempt = persist_attempt_transition(
            self.repo,
            self.attempt,
            "OUTCOME_UNKNOWN",
            transitioned_at_ms=max(self.attempt.last_transition_at_ms, self._now_ms()),
            outcome={"stage": self.stage, "error_type": type(error).__name__},
        )
        return self.attempt


# ---------------------------------------------------------------------------
# PRD #619-D3 — Reconcile action.
#
# The Reconcile action joins evidence the data plane can already see
# (daemon process state, child engine_runtime, broker positions, the
# instance's durable desired_state) and classifies whether the
# mutation's *intended effect* has landed.  It is **read-only**: it
# never replays the mutation.  ``EFFECT_NOT_OBSERVED`` is not
# automatic permission to retry — the operator must still decide.
# ---------------------------------------------------------------------------


ProcessStateLiteral = Literal["running", "stopping", "exited", "idle", "unreachable"]
DesiredStateLiteral = Literal["RUNNING", "PAUSED", "STOPPED"]
EngineRuntimeStateLiteral = Literal["IDLE", "RUNNING", "PAUSED", "DRAINING", "FAILED"]


class ReconciliationEvidence(BaseModel):
    """Typed snapshot the router hands to ``reconcile_mutation_effect``.

    Every field is independently optional because each evidence source
    can be unavailable for its own reason (daemon down, child not
    bound, broker disconnected) — Reconcile must classify partial
    snapshots conservatively rather than refuse them.

    ``daemon_reachable`` is the gating signal: when ``False``, every
    action classifies as ``NOT_PROVABLE`` regardless of the rest,
    because the daemon owns process identity and a daemon outage
    means the read is fundamentally stale.

    The fields cover the evidence sources PRD #619 §3 names: daemon
    process registry (``process_state``, ``bound_run_id``), child
    ``engine_runtime.json`` (``engine_runtime_state``), durable
    desired-state sidecar (``desired_state``), broker view
    (``broker_owned_positions_empty``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    daemon_reachable: bool
    process_state: ProcessStateLiteral | None = None
    bound_run_id: str | None = None
    desired_state: DesiredStateLiteral | None = None
    engine_runtime_state: EngineRuntimeStateLiteral | None = None
    broker_owned_positions_empty: bool | None = None
    observed_at_ms: int = Field(ge=0)


ReconciliationOutcome = Literal[
    "EFFECT_CONFIRMED",
    "EFFECT_NOT_OBSERVED",
    "EVIDENCE_CONFLICT",
    "NOT_PROVABLE",
]


def reconcile_mutation_effect(attempt: MutationAttempt, evidence: ReconciliationEvidence) -> ReconciliationOutcome:
    """Classify whether ``attempt``'s intended effect is observable.

    Pure: never touches disk.  The router writes the resulting
    ``MutationAttempt`` (with the outcome folded in via
    ``transition_attempt``) after this returns.

    Classification rules dispatch on ``attempt.action``.  Each rule
    set treats absent evidence as ``NOT_PROVABLE`` rather than
    inferring a missing fact — the only positive evidence that
    confirms an effect is direct observation of the intended state.
    ``EVIDENCE_CONFLICT`` is reserved for genuine contradictions
    (e.g. the durable desired state moved further than the mutation
    asked for); a brief timing mismatch reads as
    ``EFFECT_NOT_OBSERVED`` instead.
    """
    if not evidence.daemon_reachable:
        return "NOT_PROVABLE"
    if attempt.action == "stop":
        return _reconcile_stop(evidence)
    if attempt.action == "start":
        return _reconcile_start(evidence)
    if attempt.action == "resume":
        return _reconcile_resume(evidence)
    if attempt.action == "pause":
        return _reconcile_pause(evidence)
    if attempt.action == "flatten":
        return _reconcile_flatten(evidence)
    # The action set is closed by ``ActionName``; an unknown value
    # only reaches here through a forward-incompatible record.
    return "NOT_PROVABLE"


def _reconcile_stop(evidence: ReconciliationEvidence) -> ReconciliationOutcome:
    state = evidence.process_state
    if state in {"exited", "idle"}:
        return "EFFECT_CONFIRMED"
    if state == "running":
        return "EFFECT_NOT_OBSERVED"
    if state == "stopping":
        # In flight — not yet observable as effect; operator should
        # check back rather than mark confirmed prematurely.
        return "EFFECT_NOT_OBSERVED"
    # ``unreachable`` or ``None`` — daemon answered but cannot
    # describe the process; insufficient evidence.
    return "NOT_PROVABLE"


def _reconcile_start(evidence: ReconciliationEvidence) -> ReconciliationOutcome:
    state = evidence.process_state
    if state == "running":
        if evidence.bound_run_id is None:
            # Process reported running but the daemon has no binding —
            # the two facts disagree about whether a run is actually
            # owned by this instance.
            return "EVIDENCE_CONFLICT"
        return "EFFECT_CONFIRMED"
    if state in {"exited", "idle", "stopping"}:
        return "EFFECT_NOT_OBSERVED"
    return "NOT_PROVABLE"


def _reconcile_resume(evidence: ReconciliationEvidence) -> ReconciliationOutcome:
    if evidence.desired_state is None:
        return "NOT_PROVABLE"
    if evidence.desired_state == "STOPPED":
        # Resume's durable target is RUNNING; a STOPPED desired state
        # means a later mutation has superseded this one with a
        # stronger intent.  The reconcile result is "the effect we
        # asked for did not land because something else replaced it."
        return "EVIDENCE_CONFLICT"
    if evidence.desired_state != "RUNNING":
        return "EFFECT_NOT_OBSERVED"
    # desired_state == RUNNING from here on.
    if evidence.process_state != "running":
        return "EFFECT_NOT_OBSERVED"
    if evidence.engine_runtime_state is None:
        # Durable + process say resumed but the child has not yet
        # published a runtime snapshot — operator should refresh.
        return "EFFECT_NOT_OBSERVED"
    if evidence.engine_runtime_state == "RUNNING":
        return "EFFECT_CONFIRMED"
    return "EFFECT_NOT_OBSERVED"


def _reconcile_pause(evidence: ReconciliationEvidence) -> ReconciliationOutcome:
    if evidence.desired_state is None:
        return "NOT_PROVABLE"
    if evidence.desired_state == "PAUSED":
        return "EFFECT_CONFIRMED"
    if evidence.desired_state == "STOPPED":
        # Stop is stronger than pause; another mutation moved the
        # state past pause.
        return "EVIDENCE_CONFLICT"
    return "EFFECT_NOT_OBSERVED"


def _reconcile_flatten(
    evidence: ReconciliationEvidence,
) -> ReconciliationOutcome:
    empty = evidence.broker_owned_positions_empty
    if empty is True:
        return "EFFECT_CONFIRMED"
    if empty is False:
        return "EFFECT_NOT_OBSERVED"
    return "NOT_PROVABLE"
