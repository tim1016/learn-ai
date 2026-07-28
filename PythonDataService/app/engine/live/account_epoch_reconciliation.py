"""Typed broker-fact proof construction for the Account Epoch write fence.

This module intentionally makes no broker writes. It reads one complete fact
set, compares it with the canonical durable journal exposure, and returns an
immutable proof for :mod:`account_epoch` to validate and persist.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from app.broker.ibkr.models import IbkrOpenOrder, IbkrOrderEvent, IbkrPosition, IbkrPositionsSnapshot
from app.engine.live.account_clerk_journal import is_economic_terminal_broker_event
from app.engine.live.account_clerk_journal_models import AccountClerkJournalEntry
from app.engine.live.account_epoch import (
    AccountEpochOutageChanges,
    AccountEpochOutageDiff,
    AccountEpochReconciliationProof,
    AccountEpochState,
    EpochReconciliationDepth,
)
from app.engine.live.journal_exposure import project_journal_account_exposure

_BROKER_READ_TIMEOUT_S = 30.0


@runtime_checkable
class AccountEpochBrokerEvidence(Protocol):
    """Complete read-only account truth required to reopen an epoch gate."""

    async def fetch_positions(self) -> IbkrPositionsSnapshot: ...

    async def fetch_execution_evidence(self) -> list[IbkrOrderEvent]: ...

    async def list_open_orders(self) -> list[IbkrOpenOrder]: ...

    async def list_completed_orders(self) -> list[IbkrOpenOrder]: ...


async def collect_epoch_reconciliation_proof(
    *,
    broker: object | None,
    state: AccountEpochState,
    entries: list[AccountClerkJournalEntry],
) -> AccountEpochReconciliationProof:
    """Return a complete proof, or an explicit unavailable-evidence proof.

    The four broker reads are independent. Running them together bounds the
    recovery interval to one broker timeout and lets Clerk callback intake
    continue; the caller rejects the proof if that journal changes meanwhile.
    """

    try:
        evidence = _require_broker_evidence(broker)
        positions_raw, executions_raw, open_orders_raw, completed_orders_raw = await asyncio.gather(
            _read(evidence.fetch_positions),
            _read(evidence.fetch_execution_evidence),
            _read(evidence.list_open_orders),
            _read(evidence.list_completed_orders),
        )
        positions = IbkrPositionsSnapshot.model_validate(positions_raw)
        executions = tuple(IbkrOrderEvent.model_validate(item) for item in executions_raw)
        open_orders = tuple(IbkrOpenOrder.model_validate(item) for item in open_orders_raw)
        completed_orders = tuple(IbkrOpenOrder.model_validate(item) for item in completed_orders_raw)
        if positions.account_id != state.account_id or not positions.is_paper:
            raise RuntimeError("ACCOUNT_EPOCH_POSITION_SNAPSHOT_ACCOUNT_OR_PAPER_MISMATCH")
    except Exception as exc:
        return _unavailable_proof(state=state, entries=entries, failure=exc)

    return _complete_proof(
        state=state,
        entries=entries,
        positions=positions,
        executions=executions,
        open_orders=open_orders,
        completed_orders=completed_orders,
    )


def _require_broker_evidence(broker: object | None) -> AccountEpochBrokerEvidence:
    if broker is None or not isinstance(broker, AccountEpochBrokerEvidence):
        raise RuntimeError("ACCOUNT_EPOCH_BROKER_EVIDENCE_UNAVAILABLE")
    return broker


async def _read(method: Callable[[], Awaitable[object]]) -> object:
    return await asyncio.wait_for(method(), timeout=_BROKER_READ_TIMEOUT_S)


def _complete_proof(
    *,
    state: AccountEpochState,
    entries: list[AccountClerkJournalEntry],
    positions: IbkrPositionsSnapshot,
    executions: tuple[IbkrOrderEvent, ...],
    open_orders: tuple[IbkrOpenOrder, ...],
    completed_orders: tuple[IbkrOpenOrder, ...],
) -> AccountEpochReconciliationProof:
    known_order_refs = {
        entry.intent.order_ref
        for entry in entries
        if entry.entry_kind == "recorded" and entry.intent is not None and entry.intent.order_ref is not None
    }
    terminal_intent_ids = {
        entry.intent.intent_id
        for entry in entries
        if entry.intent is not None
        and (
            entry.entry_kind in {"broker_acked", "cancel_confirmed"}
            or (
                entry.entry_kind == "broker_event"
                and is_economic_terminal_broker_event(entry.broker_event)
            )
            or (
                entry.entry_kind == "reconciliation"
                and entry.reconciliation_verdict in {"RECOVER_ADOPT", "HALT"}
            )
        )
    }
    unresolved_intents = [
        {"intent_id": entry.intent.intent_id, "order_ref": entry.intent.order_ref}
        for entry in entries
        if entry.entry_kind == "recorded"
        and entry.intent is not None
        and entry.intent.intent_id not in terminal_intent_ids
    ]
    unknown_orders = [
        _order_fact(order)
        for order in (*open_orders, *completed_orders)
        if order.order_ref not in known_order_refs
    ]
    unknown_executions = [
        _execution_fact(execution)
        for execution in executions
        if execution.order_ref not in known_order_refs
    ]
    baseline_exposure = project_journal_account_exposure(entries, account_id=state.account_id)
    expected_position_by_symbol = {
        exposure.symbol.upper(): exposure.quantity for exposure in baseline_exposure
    }
    observed_position_by_symbol: dict[str, float] = defaultdict(float)
    observed_position_count_by_symbol: dict[str, int] = defaultdict(int)
    for position in positions.positions:
        if position.quantity != 0:
            observed_position_by_symbol[position.symbol.upper()] += position.quantity
            observed_position_count_by_symbol[position.symbol.upper()] += 1
    # A healthy sibling's journal-proven position is not contamination. The
    # proof remains conservative for any broker position without an exact
    # account-level journal baseline (including an ack-only broker order).
    nonflat_positions = [
        _position_fact(position)
        for position in positions.positions
        if position.quantity != 0
        and not _is_single_position_journal_match(
            position=position,
            expected_position_by_symbol=expected_position_by_symbol,
            observed_position_count_by_symbol=observed_position_count_by_symbol,
        )
    ]
    missing_baseline_positions = [
        {
            "symbol": symbol,
            "expected_signed_quantity": expected_quantity,
            "observed_signed_quantity": observed_position_by_symbol.get(symbol, 0.0),
        }
        for symbol, expected_quantity in expected_position_by_symbol.items()
        if (
            observed_position_by_symbol.get(symbol, 0.0) != expected_quantity
            or observed_position_count_by_symbol.get(symbol, 0) != 1
        )
    ]
    outage_diff = AccountEpochOutageDiff(
        required_reconciliation_depth=_required_reconciliation_depth(state),
        intents=AccountEpochOutageChanges(changed=tuple(unresolved_intents)),
        orders=AccountEpochOutageChanges(discovered=tuple(unknown_orders)),
        executions=AccountEpochOutageChanges(discovered=tuple(unknown_executions)),
        positions=AccountEpochOutageChanges(
            discovered=tuple(nonflat_positions),
            changed=tuple(missing_baseline_positions),
        ),
    )
    return AccountEpochReconciliationProof(
        account_id=state.account_id,
        reconciliation_id=_required_reconciliation_id(state),
        candidate_epoch=state.current_epoch,
        required_reconciliation_depth=_required_reconciliation_depth(state),
        journal_tail_seq=max((entry.seq for entry in entries), default=0),
        evidence_status="COMPLETE",
        outage_diff=outage_diff,
    )


def _is_single_position_journal_match(
    *,
    position: IbkrPosition,
    expected_position_by_symbol: dict[str, float],
    observed_position_count_by_symbol: dict[str, int],
) -> bool:
    """Accept a baseline only when no same-symbol contract can be hidden.

    The canonical Clerk account exposure is symbol-granular.  When the broker
    reports more than one non-flat contract for a symbol, netting their
    quantities could conceal an unmanaged option or a different stock lot.
    Keep such a case contaminated until the journal evolves a contract-level
    identity; the normal one-symbol, one-position sibling path stays usable.
    """

    symbol = position.symbol.upper()
    return (
        observed_position_count_by_symbol.get(symbol, 0) == 1
        and position.quantity == expected_position_by_symbol.get(symbol)
    )


def _unavailable_proof(
    *,
    state: AccountEpochState,
    entries: list[AccountClerkJournalEntry],
    failure: BaseException,
) -> AccountEpochReconciliationProof:
    return AccountEpochReconciliationProof(
        account_id=state.account_id,
        reconciliation_id=_required_reconciliation_id(state),
        candidate_epoch=state.current_epoch,
        required_reconciliation_depth=_required_reconciliation_depth(state),
        journal_tail_seq=max((entry.seq for entry in entries), default=0),
        evidence_status="UNAVAILABLE",
        outage_diff=AccountEpochOutageDiff(
            required_reconciliation_depth=_required_reconciliation_depth(state),
            intents=AccountEpochOutageChanges(
                discovered=({"failure_type": type(failure).__name__},),
            ),
            orders=AccountEpochOutageChanges(),
            executions=AccountEpochOutageChanges(),
            positions=AccountEpochOutageChanges(),
        ),
    )


def _required_reconciliation_id(state: AccountEpochState) -> str:
    if state.reconciliation_id is None:
        raise RuntimeError("ACCOUNT_EPOCH_RECONCILIATION_ID_MISSING")
    return state.reconciliation_id


def _required_reconciliation_depth(state: AccountEpochState) -> EpochReconciliationDepth:
    if state.required_reconciliation_depth is None:
        raise RuntimeError("ACCOUNT_EPOCH_RECONCILIATION_DEPTH_MISSING")
    return state.required_reconciliation_depth


def _order_fact(order: IbkrOpenOrder) -> dict[str, object]:
    return {
        "order_ref": order.order_ref,
        "order_id": order.order_id,
        "perm_id": order.perm_id,
        "con_id": order.con_id,
        "symbol": order.symbol,
        "sec_type": order.sec_type,
        "status": order.status,
        "quantity": order.quantity,
        "remaining": order.remaining,
    }


def _execution_fact(execution: IbkrOrderEvent) -> dict[str, object]:
    return {
        "order_ref": execution.order_ref,
        "order_id": execution.order_id,
        "exec_id": execution.exec_id,
        "con_id": execution.con_id,
        "symbol": execution.symbol,
        "side": execution.side,
        "fill_quantity": execution.fill_quantity,
        "ts_ms": execution.ts_ms,
    }


def _position_fact(position: IbkrPosition) -> dict[str, object]:
    return {
        "con_id": position.con_id,
        "symbol": position.symbol,
        "sec_type": position.sec_type,
        "expiry_ms": position.expiry_ms,
        "strike": position.strike,
        "right": position.right,
        "multiplier": position.multiplier,
        "signed_quantity": position.quantity,
    }


__all__ = [
    "AccountEpochBrokerEvidence",
    "collect_epoch_reconciliation_proof",
]
