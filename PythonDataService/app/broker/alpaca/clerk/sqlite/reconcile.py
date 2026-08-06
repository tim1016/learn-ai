"""Ordered account reconciliation and effect-operation recovery."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Literal
from weakref import WeakKeyDictionary

from app.broker.alpaca.clerk.exposure import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
    signed_broker_position_quantity,
)
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    AccountHoldResolvedFacts,
    ReconciliationAttemptedFacts,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import (
    EffectOperationResource,
    TransitionInput,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    AdmissionBlockedError,
    raise_uncertainty,
    resolve_reconciliation_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    PositionDriftCause,
    PositionDriftObservation,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    order_ref_namespace_matches,
)

logger = logging.getLogger(__name__)

UNEXPLAINED_ORDER_REASON_CODE = "UNEXPLAINED_ORDER"
MAX_OPEN_ORDER_SNAPSHOT = 500

_RECONCILIATION_LOCKS: WeakKeyDictionary[ClerkSqliteRepository, asyncio.Lock] = WeakKeyDictionary()
_RECONCILIATION_LOCKS_GUARD = threading.Lock()

Trigger = Literal["AUTOMATIC", "OPERATOR_RECONCILE_NOW"]
ReconciliationOutcome = Literal["STILL_UNKNOWN", "RESOLVED_SUCCESS", "RESOLVED_FAILURE"]
AccountVerdict = Literal["clean", "unexplained_order", "position_drift", "stale"]


@dataclass(frozen=True)
class ReconcilePlan:
    verdict: AccountVerdict
    foreign_orders: tuple[BrokerOrder, ...] = field(default_factory=tuple)
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)


def plan_account_reconciliation(
    *,
    namespaces: frozenset[str],
    broker_orders: list[BrokerOrder],
    broker_positions: list[BrokerPosition],
    attributed_positions: dict[str, float],
    known_order_refs: frozenset[str] | None = None,
) -> ReconcilePlan:
    """Derive residual account safety only after local evidence was folded."""
    foreign = tuple(
        order
        for order in broker_orders
        if not order_ref_namespace_matches(order.client_order_id, namespaces)
        or (known_order_refs is not None and order.client_order_id not in known_order_refs)
    )
    in_flight_symbols = {
        order.symbol.upper()
        for order in broker_orders
        if order.status.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
    }
    broker_by_symbol = {
        position.symbol.upper(): signed_broker_position_quantity(position) for position in broker_positions
    }
    symbols = set(broker_by_symbol) | set(attributed_positions)
    drifted = tuple(
        sorted(
            symbol
            for symbol in symbols
            if symbol not in in_flight_symbols
            and abs(broker_by_symbol.get(symbol, 0.0) - attributed_positions.get(symbol, 0.0)) > POSITION_QTY_EPSILON
        )
    )
    verdict: AccountVerdict
    if foreign:
        verdict = "unexplained_order"
    elif drifted:
        verdict = "position_drift"
    else:
        verdict = "clean"
    return ReconcilePlan(
        verdict=verdict,
        foreign_orders=foreign,
        drifted_symbols=drifted,
    )


async def reconcile_uncertain_order(
    repo: ClerkSqliteRepository,
    *,
    order_ref: str,
    trigger: Trigger,
    trade: BrokerTradePort,
) -> ReconciliationOutcome:
    """Compatibility surface that dispatches once at effect-operation scope."""
    order = repo.order(order_ref)
    assert order is not None
    effect = repo.active_exit_for_order(order_ref) or repo.effect_operation(order.effect_operation_id)
    assert effect is not None
    return await _reconcile_effect(repo, effect=effect, trigger=trigger, trade=trade)


async def _reconcile_effect(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    trigger: Trigger,
    trade: BrokerTradePort,
) -> ReconciliationOutcome:
    if effect.state in ("succeeded", "failed", "rejected"):
        return "RESOLVED_SUCCESS" if effect.state == "succeeded" else "RESOLVED_FAILURE"

    if effect.kind == "EXIT":
        await resolve_exit(
            repo,
            effect_operation_id=effect.effect_operation_id,
            trade=trade,
        )
        order = next(
            item for item in repo.orders_for_effect_operation(effect.effect_operation_id) if item.role == "ENTRY"
        )
    else:
        order = repo.order_for_effect_operation(effect.effect_operation_id)
        assert order is not None
        await resolve_enter_submission(repo, order_ref=order.order_ref, trade=trade)

    effect_after = repo.effect_operation(effect.effect_operation_id)
    assert effect_after is not None
    order_after = repo.order(order.order_ref)
    assert order_after is not None
    if effect_after.state == "succeeded" or (effect_after.kind == "ENTER" and order_after.broker_order_id is not None):
        outcome: ReconciliationOutcome = "RESOLVED_SUCCESS"
    elif effect_after.state in ("failed", "rejected"):
        outcome = "RESOLVED_FAILURE"
    else:
        outcome = "STILL_UNKNOWN"
    _record_reconciliation_attempt(
        repo,
        effect=effect_after,
        order_ref=order.order_ref,
        trigger=trigger,
        outcome=outcome,
    )
    return outcome


def _record_reconciliation_attempt(
    repo: ClerkSqliteRepository,
    *,
    effect: EffectOperationResource,
    order_ref: str,
    trigger: Trigger,
    outcome: ReconciliationOutcome,
) -> None:
    facts = ReconciliationAttemptedFacts(
        trigger=trigger,
        outcome=outcome,
        why=f"reconciliation ({trigger}) resolved {effect.effect_operation_id!r} to {outcome}",
    )
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=effect.strategy_instance_id,
            run_id=effect.run_id,
            command_id=effect.command_id,
            effect_operation_id=effect.effect_operation_id,
            order_ref=order_ref,
            transition_kind="RECONCILIATION_ATTEMPTED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state=effect.state,
            clerk_observed_at_ms=repo.clock(),
            summary_code="RECONCILIATION_ATTEMPTED",
            facts_json=facts.to_facts_json(),
        )
    )


def _sync_unexplained_order_hold(repo: ClerkSqliteRepository, foreign_orders: tuple[BrokerOrder, ...]) -> None:
    evidence_refs = sorted(order.order_id for order in foreign_orders)
    if not evidence_refs:
        facts = AccountHoldResolvedFacts(
            reason_code=UNEXPLAINED_ORDER_REASON_CODE,
            evidence_refs=[],
        )
        repo.resolve_account_hold_if_active(
            reason_code=UNEXPLAINED_ORDER_REASON_CODE,
            build_transition=lambda: TransitionInput(
                transition_kind="ACCOUNT_HOLD_RESOLVED",
                custody_owner="ACCOUNT_CLERK",
                execution_authority="ACCOUNT_CLERK",
                operation_state="succeeded",
                clerk_observed_at_ms=repo.clock(),
                summary_code="ACCOUNT_HOLD_RESOLVED_BY_RECONCILIATION",
                facts_json=facts.to_facts_json(),
            ),
        )
        return

    facts = AccountHoldRaisedFacts(
        reason_code=UNEXPLAINED_ORDER_REASON_CODE,
        evidence_refs=evidence_refs,
    )

    def transition(kind: str) -> TransitionInput:
        return TransitionInput(
            transition_kind=kind,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=kind,
            facts_json=facts.to_facts_json(),
        )

    repo.observe_account_hold(
        reason_code=UNEXPLAINED_ORDER_REASON_CODE,
        evidence_refs_json=canonicalize(evidence_refs),
        build_raise=lambda: transition("ACCOUNT_HOLD_RAISED"),
        build_refresh=lambda: transition("ACCOUNT_HOLD_REFRESHED"),
    )


def _sync_position_drift(
    repo: ClerkSqliteRepository,
    *,
    drifted_symbols: tuple[str, ...],
    broker_positions: list[BrokerPosition],
    attributed_positions: dict[str, float],
) -> None:
    if not drifted_symbols:
        resolve_reconciliation_uncertainty(
            repo,
            reason_code=POSITION_DRIFT_REASON_CODE,
            evidence_refs=("fresh_position_snapshot",),
        )
        return
    symbols = ", ".join(drifted_symbols)
    broker_by_symbol = {
        position.symbol.upper(): signed_broker_position_quantity(position) for position in broker_positions
    }
    cause = PositionDriftCause(
        positions=tuple(
            PositionDriftObservation(
                symbol=symbol,
                broker_qty=broker_by_symbol.get(symbol, 0.0),
                attributed_qty=attributed_positions.get(symbol, 0.0),
            )
            for symbol in drifted_symbols
        )
    )
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=POSITION_DRIFT_REASON_CODE,
        headline="Account position doesn't match broker records",
        explanation=(
            f"The broker's reported position for {symbols} differs from the Clerk's "
            "attributed exposure outside the accepted tolerance."
        ),
        operator_impact=("New positions are paused account-wide. Recognized risk reduction remains available."),
        next_step="Reconcile now, then review the drifted symbols before resuming.",
        evidence_refs=("fresh_position_snapshot",),
        cause_facts=cause.to_mapping(),
    )


def _raise_stale_snapshot_uncertainty(repo: ClerkSqliteRepository, why: str) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=None,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        headline="Broker account truth is unavailable",
        explanation=why,
        operator_impact=(
            "New exposure and unproven reduction are paused account-wide until a fresh "
            "snapshot succeeds. Cancellation and reconciliation remain available."
        ),
        next_step="Reconcile now after broker connectivity is restored.",
        evidence_refs=(),
        cause_facts={"snapshot": "open_orders_and_positions"},
        severity="error",
    )


@dataclass(frozen=True)
class AccountReconciliationResult:
    verdict: AccountVerdict
    resolved_count: int = 0
    foreign_order_count: int = 0
    drifted_symbols: tuple[str, ...] = field(default_factory=tuple)


async def _read_account_snapshot(
    repo: ClerkSqliteRepository, read: BrokerReadPort
) -> tuple[list[BrokerOrder], list[BrokerPosition]] | None:
    try:
        broker_orders, broker_positions = await asyncio.gather(
            read.list_orders(status="open", limit=MAX_OPEN_ORDER_SNAPSHOT),
            read.list_positions(),
        )
    except BrokerError as exc:
        _raise_stale_snapshot_uncertainty(repo, str(exc))
        logger.warning(
            "alpaca sqlite reconciliation could not read fresh broker truth",
            extra={"action": "reconcile_account_stale", "account_id": repo.account_id},
        )
        return None
    if len(broker_orders) >= MAX_OPEN_ORDER_SNAPSHOT:
        _raise_stale_snapshot_uncertainty(
            repo,
            "The open-order snapshot reached the 500-row boundary; completeness cannot be proven.",
        )
        return None
    return broker_orders, broker_positions


async def _recover_operations(repo: ClerkSqliteRepository, *, trigger: Trigger, trade: BrokerTradePort) -> int:
    resolved_count = 0
    for effect in repo.reconcilable_effect_operations():
        try:
            outcome = await _reconcile_effect(
                repo,
                effect=effect,
                trigger=trigger,
                trade=trade,
            )
        except (OperationClaimError, AdmissionBlockedError):
            logger.info(
                "alpaca sqlite reconciliation deferred a contended or policy-blocked effect",
                extra={
                    "action": "reconcile_effect_deferred",
                    "account_id": repo.account_id,
                    "effect_operation_id": effect.effect_operation_id,
                },
            )
            continue
        if outcome != "STILL_UNKNOWN":
            resolved_count += 1
    return resolved_count


def _reconciliation_lock(repo: ClerkSqliteRepository) -> asyncio.Lock:
    """One account pass at a time, shared by automatic and operator callers."""
    with _RECONCILIATION_LOCKS_GUARD:
        lock = _RECONCILIATION_LOCKS.get(repo)
        if lock is None:
            lock = asyncio.Lock()
            _RECONCILIATION_LOCKS[repo] = lock
        return lock


async def reconcile_account(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    trigger: Trigger = "AUTOMATIC",
) -> AccountReconciliationResult:
    """Serialize snapshot-to-verdict passes for one live account authority."""
    async with _reconciliation_lock(repo):
        return await _reconcile_account_serialized(
            repo,
            read=read,
            trade=trade,
            trigger=trigger,
        )


async def _reconcile_account_serialized(
    repo: ClerkSqliteRepository,
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    trigger: Trigger,
) -> AccountReconciliationResult:
    """Fold fresh order truth, recover operations, then derive residual safety."""
    snapshot = await _read_account_snapshot(repo, read)
    if snapshot is None:
        return AccountReconciliationResult(verdict="stale")
    broker_orders, broker_positions = snapshot

    resolve_reconciliation_uncertainty(
        repo,
        reason_code=BROKER_SNAPSHOT_STALE_REASON_CODE,
        evidence_refs=("fresh_open_orders", "fresh_positions"),
    )

    known_order_refs: set[str] = set()
    for broker_order in broker_orders:
        if broker_order.client_order_id is None:
            continue
        local_order = repo.order(broker_order.client_order_id)
        if local_order is None:
            continue
        known_order_refs.add(local_order.order_ref)
        owner = repo.active_exit_for_order(local_order.order_ref) or repo.effect_operation(
            local_order.effect_operation_id
        )
        assert owner is not None
        fold_order_evidence(
            repo,
            effect_operation_id=owner.effect_operation_id,
            order=broker_order,
        )

    resolved_count = await _recover_operations(repo, trigger=trigger, trade=trade)

    # Include every locally captured identity, not only orders present in the
    # open snapshot. A namespace-shaped but uncaptured broker order is foreign.
    for effect in repo.reconcilable_effect_operations():
        known_order_refs.update(
            order.order_ref for order in repo.orders_for_effect_operation(effect.effect_operation_id)
        )
    for instance in repo.strategy_instances():
        known_order_refs.update(
            order.order_ref for order in repo.entry_orders_for_strategy(instance["strategy_instance_id"])
        )

    namespaces = frozenset(
        build_bot_order_namespace(instance["strategy_instance_id"]) for instance in repo.strategy_instances()
    )
    attributed_positions = repo.attributed_positions_by_symbol()
    plan = plan_account_reconciliation(
        namespaces=namespaces,
        broker_orders=broker_orders,
        broker_positions=broker_positions,
        attributed_positions=attributed_positions,
        known_order_refs=frozenset(known_order_refs),
    )
    _sync_unexplained_order_hold(repo, plan.foreign_orders)
    _sync_position_drift(
        repo,
        drifted_symbols=plan.drifted_symbols,
        broker_positions=broker_positions,
        attributed_positions=attributed_positions,
    )
    return AccountReconciliationResult(
        verdict=plan.verdict,
        resolved_count=resolved_count,
        foreign_order_count=len(plan.foreign_orders),
        drifted_symbols=plan.drifted_symbols,
    )


__all__ = [
    "AccountReconciliationResult",
    "ReconcilePlan",
    "plan_account_reconciliation",
    "reconcile_account",
    "reconcile_uncertain_order",
]
