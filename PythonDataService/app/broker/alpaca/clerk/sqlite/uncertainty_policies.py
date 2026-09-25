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
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite.facts import FACTS_SCHEMA_VERSION
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXECUTION_PRICE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
    ExecutionCoverageConflictCause,
    ExecutionPriceConflictCause,
    ExitNotFlatCause,
    ExitStuckCause,
    FailedEnterFilledCause,
    LossHoldCause,
    PositionDriftCause,
    StreamHealthHoldCause,
    UnexplainedOrderCause,
    UnfoldableBrokerOrderCause,
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


#: What an open episode means for the operator's residue discharge (#2381).
#: ``strands``: the episode is what leaves an attributed residue the broker
#: does not hold, and a discharge is offered for it. ``admits``: the episode
#: doubts no fill the strategy is owed, so a discharge may proceed beside it.
#: ``refuses`` (the default, and the answer for an unregistered code): the
#: episode says the Clerk may not yet know a fill that would move the residue,
#: and zeroing it now could leave a phantom position once that fill folds.
type ResidueDischargeRole = Literal["strands", "admits", "refuses"]


@dataclass(frozen=True)
class ReasonPolicy:
    scope: str
    blocks_new_exposure: bool
    allows_reduction: bool
    cause_is_valid: Callable[[Any], bool]
    age: AgePolicy
    #: Whether an open episode keeps a draining lane from answering flat
    #: (#2344, ADR 0063 Decision 2 condition 4): true when the episode says
    #: the Clerk does not know what it holds. Required, with no default, so
    #: every row states it. False for account holds (``UNFOLDABLE_BROKER_ORDER``
    #: included) and for ``EXECUTION_COVERAGE_CONFLICT``: their only resolvers
    #: are refused while draining, so the broker-flat and attributed-flat reads
    #: carry the load there, and blocking on them would wedge a flat lane
    #: forever.
    blocks_lane_quiet: bool
    facts_schema_version: int = FACTS_SCHEMA_VERSION
    # Whether an active episode leaves the safe-flatten recovery available.
    # Distinct from ``allows_reduction``: POSITION_DRIFT and the loss hold
    # admit a proven REDUCE, yet a flatten plan built from attributed
    # quantities must still refuse under them. Set only for causes a flatten
    # exists to clear (a stuck/not-flat EXIT, a fill on a failed ENTER #2348)
    # or that say nothing about any attributed quantity (an unfoldable foreign
    # order, #2363).
    admits_safe_flatten: bool = False
    # Whether an admitted episode is *broker-side* evidence the latest
    # successful account reconciliation must postdate before a safe flatten
    # may rely on it. Set for an unfoldable foreign order (#2363 review): it
    # can be raised by the trade-update stream after the reconciliation, and
    # the flatten must not reuse broker truth that predates it. Not set for
    # EXIT_NOT_FLAT / EXIT_STUCK: those are the Clerk's own EXIT bookkeeping,
    # refreshed by every automatic re-drive, and requiring a newer
    # reconciliation after each would refuse the very flatten that clears
    # them; their attributed quantities are already pinned by the
    # position-evidence freshness gate. Not set for FAILED_ENTER_FILLED
    # (#2348) either: it is derived from a folded fill, so every raise moves
    # the fenced symbol's attributed position with it (pinned by the same
    # freshness gate), and a sweep's re-derive raises from fills that sweep's
    # broker snapshot already contains.
    safe_flatten_requires_later_reconciliation: bool = False
    residue_discharge: ResidueDischargeRole = "refuses"


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


def _failed_enter_filled_cause_is_valid(value: Any) -> bool:
    try:
        FailedEnterFilledCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _execution_coverage_conflict_cause_is_valid(value: Any) -> bool:
    try:
        ExecutionCoverageConflictCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _execution_price_conflict_cause_is_valid(value: Any) -> bool:
    try:
        ExecutionPriceConflictCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _unexplained_order_cause_is_valid(value: Any) -> bool:
    try:
        UnexplainedOrderCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _unfoldable_broker_order_cause_is_valid(value: Any) -> bool:
    try:
        UnfoldableBrokerOrderCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _stream_health_hold_cause_is_valid(value: Any) -> bool:
    try:
        StreamHealthHoldCause.from_mapping(value)
    except ValueError:
        return False
    return True


def _loss_hold_cause_is_valid(value: Any) -> bool:
    try:
        LossHoldCause.from_mapping(value)
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
        blocks_lane_quiet=True,
        residue_discharge="admits",
    ),
    BROKER_SNAPSHOT_STALE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=broker_snapshot_stale_cause_is_valid,
        age=CauseCleared(),
        blocks_lane_quiet=True,
    ),
    RECONCILIATION_INCOMPLETE_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=reconciliation_incomplete_cause_is_valid,
        age=CauseCleared(),
        blocks_lane_quiet=True,
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
        blocks_lane_quiet=True,
    ),
    EXIT_NOT_FLAT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        admits_safe_flatten=True,
        cause_is_valid=_exit_not_flat_cause_is_valid,
        # Byte-identical replacement of the former
        # EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000 / EXIT_NOT_FLAT_MAX_REDRIVES
        # = 3 module constants in exit_watchdog.py.
        age=RedriveThenEscalate(after_ms=120_000, max_count=3, escalate_to=EXIT_STUCK_REASON_CODE),
        blocks_lane_quiet=True,
        residue_discharge="strands",
    ),
    EXIT_STUCK_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        admits_safe_flatten=True,
        cause_is_valid=_exit_stuck_cause_is_valid,
        # A durable escalation must not carry a clock: only an
        # attributed-flat proof or an operator may end it. VoidAfter here
        # would silently discard the episode the escalation exists to
        # preserve (ADR 0048 Decision 1).
        age=CauseCleared(),
        blocks_lane_quiet=True,
        residue_discharge="strands",
    ),
    # #2348: a fill on an ENTER already folded terminal. The Clerk keeps the
    # real position; this fences the instance against new exposure and admits
    # only reduction of the contradicted symbols, so the operator's flatten or
    # a strategy EXIT can close it. Ended only by an attributed-flat proof on a
    # clean broker reconciliation -- never on a timer.
    FAILED_ENTER_FILLED_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        admits_safe_flatten=True,
        cause_is_valid=_failed_enter_filled_cause_is_valid,
        age=CauseCleared(),
        # Blocks lane quiet (#2344): it says the strategy's belief and the
        # Clerk's custody disagree about a position. It cannot wedge a flat
        # draining lane: its only exit is the reconciliation sweep's
        # attributed-flat proof on a clean verdict, which runs on its own and
        # through ``reconcile_now`` -- a quiesce action a draining lane still
        # routes (#2351). A flat broker with no open orders is a clean verdict.
        blocks_lane_quiet=True,
    ),
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_execution_coverage_conflict_cause_is_valid,
        age=CauseCleared(),
        blocks_lane_quiet=False,
    ),
    # #2460: a broker order total that repeats the recorded fill quantity but
    # restates its average price. The recorded fills, positions and FIFO P&L
    # inputs stay exactly as they are -- the episode keeps the broker's
    # reported price, quantity and source time as evidence and reports the
    # bot's economic coverage incomplete, so the panel's needs-attention flag
    # shows it. It doubts the *cost* of executions whose quantity both sides
    # agree on, so unlike EXECUTION_COVERAGE_CONFLICT it never forbids
    # reductions or exits: a position must always be reduceable while its
    # basis is under dispute. Cleared only by a later total that agrees
    # within tolerance (an execution correction that explains the difference
    # shows up as exactly that).
    EXECUTION_PRICE_CONFLICT_REASON_CODE: ReasonPolicy(
        scope="CUSTODY_SUBJECT",
        blocks_new_exposure=True,
        allows_reduction=True,
        admits_safe_flatten=True,
        cause_is_valid=_execution_price_conflict_cause_is_valid,
        age=CauseCleared(),
        # Does not block lane quiet (#2344): it says nothing about any
        # attributed quantity -- the quantity is the one thing both sides
        # agree on -- so it cannot keep a flat draining lane from answering.
        blocks_lane_quiet=False,
        # Doubts no fill quantity the strategy is owed, so a residue
        # discharge may proceed beside it.
        residue_discharge="admits",
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
        blocks_lane_quiet=False,
    ),
    # #2363: a broker order the Clerk cannot state truthfully (e.g. a
    # multi-leg parent with a null side). Like the unexplained-order hold it
    # fences new exposure account-wide until an operator acknowledges each
    # named order (``acknowledge_unfoldable_broker_order``). Unlike that hold
    # it admits reductions: refusing them is the account-wide exit freeze the
    # containment exists to end, and any position effect the order had is
    # still fenced per symbol by the reconciliation sweep's POSITION_DRIFT.
    UNFOLDABLE_BROKER_ORDER_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=True,
        admits_safe_flatten=True,
        safe_flatten_requires_later_reconciliation=True,
        cause_is_valid=_unfoldable_broker_order_cause_is_valid,
        age=CauseCleared(),
        # Does not block lane quiet (#2344), like the unexplained-order hold:
        # its only exit, the operator acknowledgement
        # (``custody_external_order_ack``), is refused while draining, so
        # blocking would wedge a flat lane forever. It doubts no held
        # quantity -- any position effect is fenced per symbol by
        # POSITION_DRIFT, which does block -- and an order still working is
        # caught by the broker open-order read.
        blocks_lane_quiet=False,
        residue_discharge="admits",
    ),
    STREAM_HEALTH_HOLD_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=False,
        cause_is_valid=_stream_health_hold_cause_is_valid,
        age=CauseCleared(),
        blocks_lane_quiet=False,
    ),
    # ADR 0059 D4: the loss hold refuses entries account-wide and lets every
    # program keep managing its own position. It clears only by the guarded
    # operator action, never on a timer and never at session rollover.
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: ReasonPolicy(
        scope="ACCOUNT_CLERK",
        blocks_new_exposure=True,
        allows_reduction=True,
        cause_is_valid=_loss_hold_cause_is_valid,
        age=CauseCleared(),
        blocks_lane_quiet=False,
        residue_discharge="admits",
    ),
}


def reason_policy(reason_code: str) -> ReasonPolicy | None:
    """The registered policy for one reason code, or ``None`` if there is none.

    The public read of the registry. ``None`` is a real answer, not an error:
    an unregistered code is exactly what the admission path must fail closed
    on, so every caller branches on it rather than catching a ``KeyError``.
    """
    return _REASON_POLICIES.get(reason_code)


def residue_discharge_role(reason_code: str) -> ResidueDischargeRole:
    """An episode's residue-discharge role; an unregistered code refuses (#2381)."""
    policy = reason_policy(reason_code)
    return "refuses" if policy is None else policy.residue_discharge


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
