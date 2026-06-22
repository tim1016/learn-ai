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

``reconcile_mutation_effect`` lands alongside the Reconcile endpoint
in 619-D3.

All timestamps are ``int64`` ms UTC per ``.claude/rules/numerical-
rigor.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifact_io import atomic_write_pydantic_artifact, read_pydantic_artifact

ActionName = Literal["start", "stop", "flatten", "resume", "pause"]

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
    "RESPONSE_CONFIRMED": frozenset(
        {"EFFECT_CONFIRMED", "EFFECT_NOT_OBSERVED", "NOT_PROVABLE", "EVIDENCE_CONFLICT"}
    ),
    "OUTCOME_UNKNOWN": frozenset(
        {"EFFECT_CONFIRMED", "EFFECT_NOT_OBSERVED", "NOT_PROVABLE", "EVIDENCE_CONFLICT"}
    ),
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
        super().__init__(
            f"illegal mutation_attempt transition: {current_state} -> {requested_state}"
        )
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
        raise InvalidMutationTransitionError(
            current_state=attempt.dispatch_state, requested_state=new_state
        )
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

        Most-recent is defined by ``requested_at_ms`` (not file mtime —
        the artifact's stamped time is authoritative; mtime drifts under
        clock adjustments).  Returns ``None`` when no attempts exist
        for the instance or the storage root is absent.
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
            if best is None or attempt.requested_at_ms > best.requested_at_ms:
                best = attempt
        return best


