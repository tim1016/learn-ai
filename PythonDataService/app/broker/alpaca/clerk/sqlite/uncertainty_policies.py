"""The per-reason policy registry: what each uncertainty cause authorizes.

One declarative table, consulted by both halves of ``uncertainty``: the write
path validates a cause against it before an episode is recorded, and the
capability path reads ``blocks_new_exposure`` / ``allows_reduction`` from it
to decide what an operator or strategy may still do. Splitting it out keeps
the reference data separate from the engine that applies it, and keeps this
module a leaf — it imports the cause types and nothing else from the Clerk.

Adding a reason code is adding a row here. A code with no row cannot be
written at all, which is what makes every stored episode fenceable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.broker.alpaca.clerk.sqlite.facts import FACTS_SCHEMA_VERSION
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    ExecutionCoverageConflictCause,
    ExitNotFlatCause,
    ExitStuckCause,
    PositionDriftCause,
    StreamHealthHoldCause,
    UnexplainedOrderCause,
    broker_snapshot_stale_cause_is_valid,
    reconciliation_incomplete_cause_is_valid,
)


class Capability(StrEnum):
    NEW_EXPOSURE = "NEW_EXPOSURE"
    CANCEL = "CANCEL"
    REDUCE = "REDUCE"
    RECONCILE = "RECONCILE"


@dataclass(frozen=True)
class CauseCleared:
    """The episode ends when, and only when, its cause is proven gone.

    No clock at all. This is the honest declaration for an episode this ADR
    does not add age behaviour to (ADR 0048 Decision 1).
    """


@dataclass(frozen=True)
class VoidAfter:
    """Auto-close the episode once its cause has stood unresolved for
    ``grace_ms``, with a receipt whose ``summary_code`` names the age rule
    that closed it."""

    grace_ms: int
    summary_code: str


@dataclass(frozen=True)
class RedriveThenEscalate:
    """Retry the resolution every ``after_ms``, at most ``max_count`` times,
    then open the ``escalate_to`` successor episode."""

    after_ms: int
    max_count: int
    escalate_to: str


# A closed sum of exactly three shapes (ADR 0048 Decision 1). Deliberately
# not three optional fields on ReasonPolicy: independent fields admit
# combinations with no meaning (e.g. a grace window racing a redrive clock
# for the same episode). The sum makes those combinations unrepresentable.
AgePolicy = CauseCleared | VoidAfter | RedriveThenEscalate


@dataclass(frozen=True)
class ReasonPolicy:
    scope: str
    blocks_new_exposure: bool
    allows_reduction: bool
    cause_is_valid: Callable[[Any], bool]
    age: AgePolicy
    facts_schema_version: int = FACTS_SCHEMA_VERSION


def _position_drift_cause_is_valid(value: Any) -> bool:
    try:
        PositionDriftCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _order_outcome_unknown_cause_is_valid(value: Any) -> bool:
    # An unknown broker outcome is never reduction-authorizing.  Its strict
    # decoder lives with the atomic fold that opens and closes the episode.
    return isinstance(value, dict)


def _exit_not_flat_cause_is_valid(value: Any) -> bool:
    try:
        ExitNotFlatCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _exit_stuck_cause_is_valid(value: Any) -> bool:
    try:
        ExitStuckCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _execution_coverage_conflict_cause_is_valid(value: Any) -> bool:
    try:
        ExecutionCoverageConflictCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _unexplained_order_cause_is_valid(value: Any) -> bool:
    try:
        UnexplainedOrderCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _stream_health_hold_cause_is_valid(value: Any) -> bool:
    try:
        StreamHealthHoldCause.from_mapping(value)
    except ValueError:
        return False
    return True


_REASON_POLICIES: dict[str, ReasonPolicy] = {
    POSITION_DRIFT_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_position_drift_cause_is_valid,
        age=CauseCleared(),
    ),
    BROKER_SNAPSHOT_STALE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=broker_snapshot_stale_cause_is_valid,
        age=CauseCleared(),
    ),
    RECONCILIATION_INCOMPLETE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=reconciliation_incomplete_cause_is_valid,
        age=CauseCleared(),
    ),
    ORDER_OUTCOME_UNKNOWN_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_order_outcome_unknown_cause_is_valid,
        # Byte-identical replacement of the former UNCERTAIN_SUBMIT_GRACE_MS
        # = 30_000 module constant in order_evidence.py. summary_code is the
        # sole definition of the definitive-absence receipt code;
        # order_evidence.SUBMIT_ABSENCE_SUMMARY_CODE derives from it.
        age=VoidAfter(grace_ms=30_000, summary_code="ORDER_SUBMIT_FAILED_ABSENT"),
    ),
    EXIT_NOT_FLAT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_exit_not_flat_cause_is_valid,
        # Byte-identical replacement of the former
        # EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000 / EXIT_NOT_FLAT_MAX_REDRIVES
        # = 3 module constants in exit_watchdog.py.
        age=RedriveThenEscalate(after_ms=120_000, max_count=3, escalate_to=EXIT_STUCK_REASON_CODE),
    ),
    EXIT_STUCK_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_exit_stuck_cause_is_valid,
        # A durable escalation must not carry a clock: only an
        # attributed-flat proof or an operator may end it. VoidAfter here
        # would silently discard the episode the escalation exists to
        # preserve (ADR 0048 Decision 1).
        age=CauseCleared(),
    ),
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_execution_coverage_conflict_cause_is_valid,
        age=CauseCleared(),
    ),
    # The two former ``holds`` causes (ADR 0048 Decision 2). A hold was
    # always an uncertainty whose policy had nowhere to live: account-wide,
    # entry-blocking, reduction-forbidding, and ended only by proof its cause
    # is gone. Registering them here is what retires the separate table.
    #
    # Neither takes a clock. An unreviewed foreign order and an unhealthy
    # channel are both conditions, not deadlines — a grace window would
    # release an account-wide entry fence on a timer, with the cause still
    # standing. Adding age behaviour to either is a separate decision with
    # its own evidence, not a migration detail.
    UNEXPLAINED_ORDER_HOLD_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_unexplained_order_cause_is_valid,
        age=CauseCleared(),
    ),
    STREAM_HEALTH_HOLD_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_stream_health_hold_cause_is_valid,
        age=CauseCleared(),
    ),
}


def reason_age_policy[AgePolicyT: (CauseCleared, VoidAfter, RedriveThenEscalate)](
    reason_code: str, expect: type[AgePolicyT]
) -> AgePolicyT:
    """The declared age policy for one registered reason code, narrowed.

    The single place an episode's life is specified (ADR 0048 Decision 1).

    ``expect`` is required rather than optional because every caller reads a
    shape-specific field (``grace_ms``, ``after_ms``) and so is already
    coupled to one variant. Narrowing here instead of at each call site
    keeps that check in one place and turns a mis-declared reason into a
    named ``TypeError`` rather than an ``AttributeError`` several frames
    later.

    Raises ``KeyError`` for an unregistered code: every caller passes a
    known reason-code constant, so a miss here is a programming error, not
    a runtime condition to absorb.
    """
    policy = _REASON_POLICIES[reason_code].age
    if not isinstance(policy, expect):
        raise TypeError(
            f"reason code {reason_code!r} declares {type(policy).__name__}, "
            f"not the {expect.__name__} this caller requires"
        )
    return policy
