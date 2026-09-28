"""Revision-coherent money read on the existing custody transaction.

No balance is stored. FIFO, effective corrections and fee attribution keep
their existing authorities; this composes their facts into clerk.budgets.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Protocol

from app.broker.alpaca.clerk.budgets import AccountBudget, account_budget, deployment_budget
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims
from app.broker.contract.models import OrderSide


class BudgetUnavailable(ValueError):
    """A named unknown prevents authorizing money; it is never a zero."""


class BudgetFees(Protocol):
    """The canonical fee reconciler's read contract, no new fee calculator."""

    @property
    def known(self) -> bool: ...

    @property
    def unresolved(self) -> tuple[str, ...]: ...

    def total_for(self, subject_id: str) -> Decimal: ...

    def unobserved_cash_claim(self, *, cash_seen_before_ms: int, modelled_fees_seen_before_ms: int | None = None, subject_id: str | None = None) -> Decimal: ...


def project_account_budget(
    conn: sqlite3.Connection, *, cash: object, seen_before_ms: int, fees: BudgetFees,
    modelled_fees_seen_before_ms: int | None = None,
) -> AccountBudget:
    """The caller holds one custody read/write fence for this entire read.

    Formula/reference/validation: clerk.budgets; FIFO remains fifo_pnl.py.
    """
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
        budgets = []
        for commitment in commitments:
            sid = commitment["strategy_instance_id"]
            subject_id = f"bot:{sid}"
            fifo = compute_fifo_pnl([fill for fill in records if fill.sid == subject_id])
            if any(lot.side != OrderSide.BUY for lot in fifo.open_lots):
                raise BudgetUnavailable("Unexplained short exposure prevents a long-only deployment budget.")
            position_cost = sum((normalize_money(lot.qty) * normalize_money(lot.cost) for lot in fifo.open_lots), ZERO)
            pending_orders = sum((claim.unfilled_cost + claim.unfilled_fee for claim in claims if claim.strategy_instance_id == sid), ZERO)
            budgets.append(deployment_budget(
                strategy_instance_id=sid, committed_cents=commitment["committed_cents"],
                active=commitment["run_state"] == "ACTIVE" and commitment["released_at_ms"] is None,
                realized_gross=fifo.realized_pnl, fees=fees.total_for(subject_id),
                position_cost=position_cost, pending_orders=pending_orders,
                outstanding_cash=sum((claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee for claim in claims if claim.strategy_instance_id == sid), ZERO)
                + fees.unobserved_cash_claim(cash_seen_before_ms=seen_before_ms, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms, subject_id=subject_id),
            ))
        return account_budget(
            cash=cash, deployments=budgets,
            order_claims=sum((claim.unfilled_cost + claim.unseen_fill_cost + claim.unfilled_fee for claim in claims), ZERO),
            fee_claims=fees.unobserved_cash_claim(cash_seen_before_ms=seen_before_ms, modelled_fees_seen_before_ms=modelled_fees_seen_before_ms),
        )
