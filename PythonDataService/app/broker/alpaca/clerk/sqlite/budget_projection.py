"""Revision-coherent money read on the existing custody transaction.

No balance is stored. FIFO, effective corrections and fee attribution keep
their existing authorities; this composes their facts into clerk.budgets.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from app.broker.alpaca.clerk.account_money import AccountMoney, Holding, account_money
from app.broker.alpaca.clerk.budgets import AccountBudget, account_budget, deployment_budget
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.custody_subjects import BOT_SUBJECT_PREFIX, bot_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.envelope_reservations import EntryCashClaim, entry_cash_claims
from app.broker.alpaca.clerk.sqlite.order_projection import ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
from app.broker.alpaca.clerk.sqlite.reads import external_orders
from app.broker.contract.models import OrderSide

if TYPE_CHECKING:
    from app.services.alpaca_fee_attribution import FeeFill


class BudgetUnavailable(ValueError):
    """A named unknown prevents authorizing money; it is never a zero."""


class BudgetFees(Protocol):
    """The canonical fee reconciler's read contract, no new fee calculator."""

    @property
    def known(self) -> bool: ...

    @property
    def unresolved(self) -> tuple[str, ...]: ...

    @property
    def external_fills(self) -> tuple[FeeFill, ...]: ...

    def total_for(self, subject_id: str) -> Decimal: ...

    def unobserved_cash_claim(self, *, cash_seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None, subject_id: str | None = None) -> Decimal: ...


@money_context()
def _external_cash_claim(conn: sqlite3.Connection, fees: BudgetFees, *, seen_before_ms: int) -> Decimal:
    """Price normalized external BUY facts once, retaining unknown obligations.

    Formula: sum(qty * price for external BUYs not yet inside observed cash).
    Reference: PRD #2540 all-disjoint-claims contract; observation #2441/#2442.
    Canonical implementation: this composition over the fee evidence's exact
      activity population; no external balance or second execution ledger.
    Validated against: sqlite/test_budget_claims.py external cases (exact).
    """
    quantities: dict[str, Decimal] = {}
    unseen = ZERO
    for fill in fees.external_fills:
        key = fill.native_order_id
        if key is None:
            raise BudgetUnavailable(
                "An external fill has no order identity. "
                "Reconcile account activities before assigning or spending a budget."
            )
        quantities[key] = quantities.get(key, ZERO) + fill.quantity
        if fill.side is OrderSide.BUY and fill.observed_at_ms >= seen_before_ms:
            unseen += fill.quantity * fill.price
    for order in external_orders(conn):
        if order.broker_state not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES or order.filled_quantity is None:
            raise BudgetUnavailable(
                "An external order still has a working or unknown cash obligation. "
                "Resolve it at Alpaca, then choose Reconcile now before assigning or spending a budget."
            )
        if quantities.get(order.broker_order_id, ZERO) != normalize_money(order.filled_quantity):
            raise BudgetUnavailable(
                "The external order's complete execution population is unavailable. "
                "Reconcile account activities before assigning or spending a budget."
            )
    return unseen


@dataclass(frozen=True)
class _Projected:
    """One fenced read's budget plus the facts behind its order claim."""

    budget: AccountBudget
    records: tuple[FillRecord, ...]
    claims: tuple[EntryCashClaim, ...]
    # The part of ``budget.order_claims`` that is recorded fills (and their
    # reported fees) the cash observation has not seen yet; the rest is
    # pending entry orders.
    unseen_fills: Decimal


def project_account_budget(
    conn: sqlite3.Connection, *, cash: object, seen_before_ms: int, fees: BudgetFees,
    modelled_fees_seen_before_ms: int | None = None,
) -> AccountBudget:
    """The caller holds one custody read/write fence for this entire read.

    Formula/reference/validation: clerk.budgets; FIFO remains fifo_pnl.py.
    """
    return _project(conn, cash=cash, seen_before_ms=seen_before_ms, fees=fees,
                    modelled_fees_seen_before_ms=modelled_fees_seen_before_ms).budget


def project_account_money(
    conn: sqlite3.Connection, *, cash: object, seen_before_ms: int, fees: BudgetFees,
    modelled_fees_seen_before_ms: int | None = None,
) -> AccountMoney:
    """Where the account's money is, from the same fenced read as its budget.

    The subjects the budget does not cover -- stopped pre-budget bots and
    manual (operator) positions -- are valued by the same canonical FIFO over
    the same effective fills. Formula/reference/validation: clerk.account_money.
    """
    projected = _project(conn, cash=cash, seen_before_ms=seen_before_ms, fees=fees,
                         modelled_fees_seen_before_ms=modelled_fees_seen_before_ms)
    budgeted = {item.strategy_instance_id for item in projected.budget.deployments}
    with money_context():
        pending: dict[str, Decimal] = {}
        for claim in projected.claims:
            if claim.strategy_instance_id not in budgeted:
                pending[claim.strategy_instance_id] = pending.get(claim.strategy_instance_id, ZERO) + claim.unfilled_cost + claim.unfilled_fee
        subjects = {fill.sid for fill in projected.records} | {bot_subject_id(sid) for sid in pending}
        holdings = []
        for subject_id in sorted(subjects - {bot_subject_id(sid) for sid in budgeted}):
            fifo = compute_fifo_pnl([fill for fill in projected.records if fill.sid == subject_id])
            if any(lot.side != OrderSide.BUY for lot in fifo.open_lots):
                raise BudgetUnavailable("A short position outside the running budgets has no place on the money bar. Flatten it first.")
            sid = subject_id.removeprefix(BOT_SUBJECT_PREFIX) if subject_id.startswith(BOT_SUBJECT_PREFIX) else None
            holdings.append(Holding(
                subject_id=subject_id, strategy_instance_id=sid,
                position_cost=sum((lot.exact_qty * lot.exact_cost for lot in fifo.open_lots), ZERO),
                pending_orders=pending.get(sid, ZERO) if sid is not None else ZERO,
            ))
    return account_money(projected.budget, unseen_fills=projected.unseen_fills, holdings=holdings)


def _project(
    conn: sqlite3.Connection, *, cash: object, seen_before_ms: int, fees: BudgetFees,
    modelled_fees_seen_before_ms: int | None,
) -> _Projected:
    if not fees.known:
        raise BudgetUnavailable("Fee evidence is unresolved: " + "; ".join(fees.unresolved))
    legacy = conn.execute(
        "SELECT r.strategy_instance_id FROM runs r LEFT JOIN deployment_budgets b ON b.run_id=r.run_id "
        "WHERE r.state='ACTIVE' AND b.run_id IS NULL LIMIT 1"
    ).fetchone()
    if legacy is not None:
        raise BudgetUnavailable("Stop the earlier deployment before assigning budgets: " + legacy[0])
    # Manual custody remains independent. Until its order outcome is known,
    # refusing a new budget is safer than inventing its missing cash claim.
    manual = conn.execute(
        "SELECT 1 FROM effect_operations WHERE kind='MANUAL_ORDER' "
        "AND state NOT IN ('succeeded','failed','rejected','cancelled') LIMIT 1"
    ).fetchone()
    if manual:
        raise BudgetUnavailable("Resolve the working manual order before assigning or spending a budget.")
    unpriced_legacy = conn.execute(
        "SELECT 1 FROM orders o LEFT JOIN envelope_reservations r ON r.effect_operation_id=o.effect_operation_id "
        "WHERE o.role='ENTRY' AND r.effect_operation_id IS NULL AND "
        "(o.broker_state IS NULL OR LOWER(o.broker_state) NOT IN ('filled','canceled','expired','rejected','replaced') "
        "OR EXISTS (SELECT 1 FROM fills f WHERE f.order_ref=o.order_ref AND f.recorded_at_ms>=?)) LIMIT 1",
        (seen_before_ms,),
    ).fetchone()
    if unpriced_legacy:
        raise BudgetUnavailable("An earlier order has no retained cash estimate. Resolve its order evidence and wait for fresh cash.")
    account_id = conn.execute("SELECT account_id FROM control_meta WHERE id=1").fetchone()[0]
    commitments = conn.execute(
        "SELECT b.*, r.state AS run_state FROM deployment_budgets b JOIN runs r ON r.run_id=b.run_id"
    ).fetchall()
    claims = entry_cash_claims(conn, seen_before_ms=seen_before_ms)
    records = effective_fill_records(conn, account_id=account_id)
    with money_context():
        external_claim = _external_cash_claim(conn, fees, seen_before_ms=seen_before_ms)
        # A terminal manual effect ends the working-order uncertainty, not
        # the debit's overlap with cash. Effective fill lineage supplies both
        # corrected economics and the original observation boundary.
        manual_refs = {row[0] for row in conn.execute("SELECT order_ref FROM orders WHERE role='MANUAL'")}
        manual_claim = ZERO
        for fill in records:
            if fill.order_ref not in manual_refs or fill.side != OrderSide.BUY:
                continue
            if fill.recorded_at_ms is None:
                raise BudgetUnavailable("A manual fill has no observation boundary. Reconcile account executions.")
            if fill.recorded_at_ms >= seen_before_ms:
                manual_claim += normalize_money(fill.quantity) * normalize_money(fill.fill_price)
                if fill.fee is not None:
                    manual_claim += normalize_money(fill.fee)
        budgets = []
        for commitment in commitments:
            sid = commitment["strategy_instance_id"]
            subject_id = bot_subject_id(sid)
            fifo = compute_fifo_pnl([fill for fill in records if fill.sid == subject_id])
            if any(lot.side != OrderSide.BUY for lot in fifo.open_lots):
                raise BudgetUnavailable("Unexplained short exposure prevents a long-only deployment budget.")
            position_cost = sum((lot.exact_qty * lot.exact_cost for lot in fifo.open_lots), ZERO)
            pending_orders = sum((claim.unfilled_cost + claim.unfilled_fee for claim in claims if claim.strategy_instance_id == sid), ZERO)
            budgets.append(deployment_budget(
                strategy_instance_id=sid, committed_cents=commitment["committed_cents"],
                active=commitment["run_state"] == "ACTIVE" and commitment["released_at_ms"] is None,
                realized_gross=fifo.exact_realized_pnl, fees=fees.total_for(subject_id),
                position_cost=position_cost, pending_orders=pending_orders,
                outstanding_cash=sum((claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee for claim in claims if claim.strategy_instance_id == sid), ZERO)
                + fees.unobserved_cash_claim(cash_seen_before_ms=seen_before_ms, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms, subject_id=subject_id),
            ))
        budget = account_budget(
            cash=cash, deployments=budgets,
            order_claims=external_claim + manual_claim + sum((claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee for claim in claims), ZERO),
            fee_claims=fees.unobserved_cash_claim(cash_seen_before_ms=seen_before_ms, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms),
        )
        return _Projected(
            budget=budget, records=records, claims=claims,
            unseen_fills=external_claim + manual_claim + sum((claim.unseen_fill_cost for claim in claims), ZERO),
        )
