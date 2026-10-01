"""An order's executions read from Alpaca's account activity, recorded as exact slices (#2686, #2787).

Two recoveries credit an order the executions Alpaca's ``FILL`` activity
names for it: a manual leg's replacement chain, from the sweep
(:mod:`manual_order_executions`), and a bot order whose fill frame the
stream lost, from the fee-evidence producer (:mod:`bot_order_executions`).
Both record through :func:`record_activity_executions`, which names its log
actions after the :class:`ActivityRecoverySubject` it records for.

Each activity row's id embeds the execution id the stream carries
(:func:`execution_id_from_activity_id`), and the one exact append flow dedups
on it, so an execution is credited once whichever source records it first.
An execution the order already accounts for is not presented again: one it
recorded as a fill, or one an order-total proof (#2346) left quarantined
behind the cumulative that already counts it. Once the activity names one
the order does not account for, every execution it names for the order goes
through the append flow together, which dedups what the order holds and
fails closed on one the activity contradicts.

An activity can also contradict an execution the order accounts for: the
same execution id with another quantity or price. That batch goes through the
append flow too, past the caller's total gate, so the flow raises the
coverage conflict once and the operator sees it (#2791). A contradiction the
flow would not raise again -- one an uncertainty already names, or on an
order an open coverage conflict fences -- is not one here.

That id bridge has no captured Alpaca receipt yet, and a wrong one would
credit one execution twice under two ids. So an order's executions are
recorded together or not at all, and only while its exact total stays within
the quantity the caller names -- no order executes more. A batch past it
records nothing and raises ``EXECUTION_COVERAGE_CONFLICT`` in the subject's
words; an order already fenced by an open coverage conflict keeps that one.

Recovery reads the same immutable evidence again on every pass, so what it
cannot act on is reported once per process: a row that cannot be read as an
exact execution of the order (left out, never folded), and a refusal past
the order's quantity for the same evidence.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from app.broker.alpaca.adapter import execution_id_from_activity_id
from app.broker.alpaca.clerk.sqlite.exact_execution_evidence import (
    ACTIVITY_EXACT_CONFLICT_COPY,
    ExactExecutionConflictCopy,
    append_exact_execution_slice,
    exact_execution_coverage_conflict,
    exact_execution_facts,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    FILL_QTY_EPSILON,
    active_execution_coverage_conflicts,
)
from app.broker.alpaca.clerk.sqlite.execution_coverage_evidence import order_total_retained_exact_provenance
from app.broker.alpaca.clerk.sqlite.models import EffectOperationResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrderEvent, BrokerOrderLeg, OrderSide

logger = logging.getLogger(__name__)

_EXECUTION_ACTIVITY_TYPES = frozenset({"FILL", "PARTIAL_FILL"})

_EVIDENCE_SOURCE = "activity_recovery"

#: An activity's side, as the leg spells it. A sale from a flat or short
#: position is reported ``sell_short``; it is still the leg's sell.
_LEG_SIDE_OF_ACTIVITY = {"buy": OrderSide.BUY, "sell": OrderSide.SELL, "sell_short": OrderSide.SELL}


def _over_order_quantity_copy(order: str) -> ExactExecutionConflictCopy:
    """The recovery's own refusal: no execution is held aside, so the copy sends the operator to Alpaca."""
    return ExactExecutionConflictCopy(
        headline="Alpaca's fill history reports more shares than the order asked for",
        explanation=(
            f"Alpaca's fill history reports more shares for this {order} than it "
            "asked for, so nothing was credited."
        ),
        operator_impact="New exposure is blocked while this conflict stands.",
        next_step="Check the order at Alpaca.",
    )


ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY = _over_order_quantity_copy("manual order")


@dataclass(frozen=True)
class ActivityRecoverySubject:
    """Whose order a recording credits.

    ``name`` prefixes its log actions -- ``<name>_execution_recovered``,
    ``<name>_execution_unreadable``, ``<name>_execution_contradicted`` and
    ``<name>_recovered_executions_exceed_order`` -- and
    ``over_quantity_copy`` words its refusal.
    """

    name: str
    over_quantity_copy: ExactExecutionConflictCopy


MANUAL_ORDER = ActivityRecoverySubject(name="manual_order", over_quantity_copy=ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY)
BOT_ORDER = ActivityRecoverySubject(name="bot_order", over_quantity_copy=_over_order_quantity_copy("bot order"))


@dataclass(frozen=True)
class RecordedActivityExecutions:
    """What one recording did: whether the quantity refused the batch, and whether custody grew."""

    over_quantity: bool = False
    grew: bool = False


#: What each account's recovery already reported, by the report's own key.
#: In memory only: a restart reports each of them once more.
_REPORTED: WeakKeyDictionary[ClerkSqliteRepository, set[tuple[object, ...]]] = WeakKeyDictionary()
_REPORTED_GUARD = threading.Lock()


def first_report(repo: ClerkSqliteRepository, key: tuple[object, ...]) -> bool:
    """Whether this process reports ``key`` for ``repo``'s account for the first time; it is then marked reported.

    Recovery reads the same immutable evidence on every pass, so a report
    about that evidence is made once, not once a pass.
    """
    with _REPORTED_GUARD:
        reported = _REPORTED.setdefault(repo, set())
        if key in reported:
            return False
        reported.add(key)
        return True


def record_activity_executions(
    repo: ClerkSqliteRepository,
    *,
    subject: ActivityRecoverySubject,
    order_ref: str,
    owner: EffectOperationResource,
    leg: BrokerOrderLeg,
    broker_order_id: str,
    member_ids: Collection[str],
    quantity_cap: float,
    require_total: bool,
    fills: Iterable[BrokerActivity],
) -> RecordedActivityExecutions:
    """Record the executions in ``fills`` the order does not account for yet, all or none.

    An activity is the order's when it executed on one of ``member_ids``,
    under the broker order it names. ``broker_order_id`` is the order whose
    quantity ``quantity_cap`` is, named on the refusal's log. With
    ``require_total``, a batch is recorded only once the order's exact total
    would equal the shares its effective fills hold -- unless it contradicts
    an execution the order accounts for, which the append flow must raise.

    Checked and appended under the repository's write lock, so no stream
    slice lands between the check and the appends.
    """
    with repo._write_lock:
        batch = _executions_of_order(
            repo, subject=subject, order_ref=order_ref, leg=leg, member_ids=member_ids, fills=fills
        )
        accounted_ids, exact_quantity = _accounted_exacts(repo, order_ref=order_ref)
        unrecorded = [(fill, event) for fill, event in batch if event.execution_id not in accounted_ids]
        recovered_quantity = math.fsum(
            {event.execution_id: event.quantity or 0.0 for _, event in unrecorded}.values()
        )
        if exact_quantity + recovered_quantity - quantity_cap >= FILL_QTY_EPSILON:
            return RecordedActivityExecutions(
                over_quantity=True,
                grew=_refuse_over_quantity(
                    repo,
                    subject=subject,
                    order_ref=order_ref,
                    owner=owner,
                    broker_order_id=broker_order_id,
                    quantity_cap=quantity_cap,
                    executions=unrecorded,
                    exact_quantity=exact_quantity,
                    recovered_quantity=recovered_quantity,
                ),
            )
        contradicted = _contradicted_executions(
            repo, order_ref=order_ref, leg=leg, batch=batch, accounted_ids=accounted_ids
        )
        if not unrecorded and not contradicted:
            return RecordedActivityExecutions()
        if require_total and not contradicted:
            filled_quantity, _ = repo.effective_fill_totals_for_order(order_ref)
            if abs(exact_quantity + recovered_quantity - filled_quantity) >= FILL_QTY_EPSILON:
                return RecordedActivityExecutions()
        if contradicted:
            logger.warning(
                "Alpaca's account activity contradicts an execution the order records",
                extra={
                    "action": f"{subject.name}_execution_contradicted",
                    "order_ref": order_ref,
                    "execution_ids": contradicted,
                },
            )
        grew = False
        for fill, event in batch:
            executed_on = (fill.native_order_id or "").strip()
            outcome = append_exact_execution_slice(
                repo,
                event=event,
                symbol=leg.symbol,
                side=leg.side.value,
                broker_order_id=executed_on,
                order_ref=order_ref,
                owner=owner,
                evidence_source=_EVIDENCE_SOURCE,
                conflict_copy=ACTIVITY_EXACT_CONFLICT_COPY,
                proof_reference=fill.activity_id,
                extra_conflict_evidence_refs=[fill.activity_id],
            )
            if outcome == "duplicate":
                continue
            grew = grew or outcome != "coverage_conflict_already_raised"
            logger.info(
                "An order's execution was recovered from Alpaca's account activity",
                extra={
                    "action": f"{subject.name}_execution_recovered",
                    "order_ref": order_ref,
                    "execution_id": event.execution_id,
                    "broker_order_id": executed_on,
                    "outcome": outcome,
                },
            )
        return RecordedActivityExecutions(grew=grew)


def _executions_of_order(
    repo: ClerkSqliteRepository,
    *,
    subject: ActivityRecoverySubject,
    order_ref: str,
    leg: BrokerOrderLeg,
    member_ids: Collection[str],
    fills: Iterable[BrokerActivity],
) -> list[tuple[BrokerActivity, BrokerOrderEvent]]:
    """Each execution in ``fills`` of the order, oldest first, read as an exact execution of ``leg``."""
    executions: list[tuple[BrokerActivity, BrokerOrderEvent]] = []
    for fill in sorted(fills, key=lambda item: (item.occurred_at_ms or 0, item.activity_id)):
        executed_on = (fill.native_order_id or "").strip()
        if fill.activity_type.strip().upper() not in _EXECUTION_ACTIVITY_TYPES or executed_on not in member_ids:
            continue
        event = exact_execution_of_activity(fill, leg=leg)
        if event is None:
            if first_report(repo, (f"{subject.name}_execution_unreadable", order_ref, fill.activity_id)):
                logger.warning(
                    "An Alpaca execution could not be read as an exact execution of its order",
                    extra={
                        "action": f"{subject.name}_execution_unreadable",
                        "order_ref": order_ref,
                        "activity_id": fill.activity_id,
                        "broker_order_id": executed_on,
                    },
                )
            continue
        executions.append((fill, event))
    return executions


def _contradicted_executions(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    leg: BrokerOrderLeg,
    batch: list[tuple[BrokerActivity, BrokerOrderEvent]],
    accounted_ids: frozenset[str],
) -> list[str]:
    """The executions the order accounts for whose activity the append flow would raise as a conflict (#2791)."""
    return [
        event.execution_id
        for _, event in batch
        if event.execution_id in accounted_ids
        and repo.exact_execution_contradicts_record(
            order_ref=order_ref,
            facts=exact_execution_facts(
                event, symbol=leg.symbol, side=leg.side.value, evidence_source=_EVIDENCE_SOURCE
            ),
        )
    ]


def _accounted_exacts(repo: ClerkSqliteRepository, *, order_ref: str) -> tuple[frozenset[str], float]:
    """The execution ids the order accounts for, and its exact total in shares.

    Every fill it recorded, effective or superseded by a correction, and
    every exact an order-total proof (#2346) left quarantined: the order's
    final total counts those, though no fill does. The total is the
    effective exacts' shares plus those quarantined exacts'. An exact an
    open coverage conflict holds aside is not accounted for: the operator
    settles it, and the append flow already answers it without appending.
    """
    recorded = {fill["execution_id"] for fill in repo.fills_for_order(order_ref) if fill["execution_id"]}
    retained = [
        item.exact_execution
        for item in order_total_retained_exact_provenance(repo._conn, order_ref=order_ref)
        if item.exact_execution.execution_id not in recorded
    ]
    effective_exact_quantity, _ = repo.effective_exact_fill_totals_for_order(order_ref)
    return (
        frozenset(recorded | {exact.execution_id for exact in retained}),
        math.fsum([effective_exact_quantity, *(exact.slice_qty for exact in retained)]),
    )


def _refuse_over_quantity(
    repo: ClerkSqliteRepository,
    *,
    subject: ActivityRecoverySubject,
    order_ref: str,
    owner: EffectOperationResource,
    broker_order_id: str,
    quantity_cap: float,
    executions: list[tuple[BrokerActivity, BrokerOrderEvent]],
    exact_quantity: float,
    recovered_quantity: float,
) -> bool:
    """Fence a batch that would credit the order more shares than ``quantity_cap``; whether custody grew.

    The order's coverage conflict is raised once, naming the batch's first
    execution and every activity in it; an order already fenced by an open
    coverage conflict keeps that one. The refusal is logged once per
    process for the same evidence, so an order whose accounted executions
    already exceed its quantity is named once, not on every pass.
    """
    activity_ids = [fill.activity_id for fill, _ in executions]
    action = f"{subject.name}_recovered_executions_exceed_order"
    if first_report(repo, (action, order_ref, quantity_cap, exact_quantity, *activity_ids)):
        logger.warning(
            "Executions recovered for an order would exceed the shares it asked for",
            extra={
                "action": action,
                "order_ref": order_ref,
                "broker_order_id": broker_order_id,
                "order_quantity": quantity_cap,
                "exact_quantity": exact_quantity,
                "recovered_quantity": recovered_quantity,
                "activity_ids": activity_ids,
            },
        )
    if not executions or active_execution_coverage_conflicts(repo._conn, order_ref=order_ref):
        return False
    fill, event = executions[0]
    repo.append_transition(
        exact_execution_coverage_conflict(
            repo,
            event=event,
            broker_order_id=(fill.native_order_id or "").strip(),
            order_ref=order_ref,
            owner=owner,
            conflict_copy=subject.over_quantity_copy,
            proof_reference=fill.activity_id,
            extra_evidence_refs=[item.activity_id for item, _ in executions],
        )
    )
    return True


def exact_execution_of_activity(fill: BrokerActivity, *, leg: BrokerOrderLeg) -> BrokerOrderEvent | None:
    """The activity as one exact execution of the accepted leg, or ``None`` when it cannot be one."""
    if (fill.symbol or "").strip().upper() != leg.symbol.strip().upper():
        return None
    if _LEG_SIDE_OF_ACTIVITY.get((fill.side or "").strip().lower()) != leg.side:
        return None
    if fill.quantity is None or fill.price is None or fill.occurred_at_ms is None:
        return None
    if not (math.isfinite(fill.quantity) and fill.quantity > 0 and math.isfinite(fill.price) and fill.price > 0):
        return None
    return BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=fill.occurred_at_ms,
        price=fill.price,
        quantity=fill.quantity,
        execution_id=execution_id_from_activity_id(fill.activity_id),
    )


__all__ = [
    "ACTIVITY_OVER_ORDER_QUANTITY_CONFLICT_COPY",
    "BOT_ORDER",
    "MANUAL_ORDER",
    "ActivityRecoverySubject",
    "RecordedActivityExecutions",
    "exact_execution_of_activity",
    "first_report",
    "record_activity_executions",
]
