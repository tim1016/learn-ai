"""Revision-coherent money read on the existing custody transaction.

No balance is stored. FIFO, effective corrections and fee attribution keep
their existing authorities; this composes their facts into clerk.budgets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.broker.alpaca.clerk.account_money import AccountMoney, Holding, account_money
from app.broker.alpaca.clerk.budgets import AccountBudget, account_budget, deployment_budget
from app.broker.alpaca.clerk.fifo_pnl import OpenLot, compute_fifo_pnl
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import ZERO, money_context, normalize_money
from app.broker.alpaca.clerk.sqlite.custody_subjects import BOT_SUBJECT_PREFIX, bot_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.envelope_reservations import EntryCashClaim, entry_cash_claims
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
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
                "An external fill has no order identity. Reconcile account activities."
            )
        quantities[key] = quantities.get(key, ZERO) + fill.quantity
        if fill.side is OrderSide.BUY and fill.observed_at_ms >= seen_before_ms:
            unseen += fill.quantity * fill.price
    for order in external_orders(conn):
        if order.broker_state not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES or order.filled_quantity is None:
            raise BudgetUnavailable(
                "An external order still has a working or unknown cash obligation. "
                "Resolve it at Alpaca, then choose Reconcile now."
            )
        if quantities.get(order.broker_order_id, ZERO) != normalize_money(order.filled_quantity):
            raise BudgetUnavailable(
                "The external order's complete execution population is unavailable. "
                "Reconcile account activities."
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

    The subjects the budget does not cover -- stopped pre-budget bots, manual
    (operator) positions and external (non-Clerk) fills -- are valued by the
    same canonical FIFO: the Clerk's own effective fills, and the external
    population ``_external_cash_claim`` already requires to be complete. Sale
    proceeds the cash observation has not seen are counted by the same
    ``seen_before_ms`` boundary as unseen purchases. A short position has no
    place on a long-only bar and is named, never dropped.
    Formula/reference/validation: clerk.account_money.
    """
    projected = _project(conn, cash=cash, seen_before_ms=seen_before_ms, fees=fees,
                         modelled_fees_seen_before_ms=modelled_fees_seen_before_ms)
    budgeted = {item.strategy_instance_id for item in projected.budget.deployments}
    order = tuple(row[0] for row in conn.execute(
        "SELECT strategy_instance_id FROM strategy_instances ORDER BY created_at_ms, strategy_instance_id"
    ))
    unvalued: list[str] = []
    with money_context():
        pending: dict[str, Decimal] = {}
        for claim in projected.claims:
            if claim.strategy_instance_id not in budgeted:
                pending[claim.strategy_instance_id] = pending.get(claim.strategy_instance_id, ZERO) + claim.unfilled_cost + claim.unfilled_fee
        subjects = {fill.sid for fill in projected.records} | {bot_subject_id(sid) for sid in pending}
        holdings = []
        for subject_id in sorted(subjects - {bot_subject_id(sid) for sid in budgeted}):
            sid = subject_id.removeprefix(BOT_SUBJECT_PREFIX) if subject_id.startswith(BOT_SUBJECT_PREFIX) else None
            fifo = compute_fifo_pnl([fill for fill in projected.records if fill.sid == subject_id])
            unvalued += _shorts(fifo.open_lots, holder="manual trades" if sid is None else f"stopped bot {sid}")
            holdings.append(Holding(
                subject_id=subject_id, strategy_instance_id=sid,
                position_cost=_long_cost(fifo.open_lots),
                pending_orders=pending.get(sid, ZERO) if sid is not None else ZERO,
            ))
        external, unpriced = _external_holdings(conn, fees)
        holdings += external
        unvalued += unpriced
        # Net of a reported fee, exactly as an unseen purchase is its cost
        # plus its reported fee; an unreported fee stays an account charge.
        unseen_sales = sum((
            normalize_money(fill.quantity) * normalize_money(fill.fill_price) - (ZERO if fill.fee is None else normalize_money(fill.fee))
            for fill in projected.records
            if fill.side is OrderSide.SELL and fill.recorded_at_ms is not None and fill.recorded_at_ms >= seen_before_ms
        ), ZERO) + sum((
            fill.quantity * fill.price for fill in fees.external_fills
            if fill.side is OrderSide.SELL and fill.observed_at_ms >= seen_before_ms
        ), ZERO)
    return account_money(
        projected.budget, unseen_fills=projected.unseen_fills, unseen_sales=unseen_sales,
        holdings=holdings, registration_order=order, unvalued=unvalued,
    )


def _long_cost(lots: Iterable[OpenLot]) -> Decimal:
    return sum((lot.exact_qty * lot.exact_cost for lot in lots if lot.side is OrderSide.BUY), ZERO)


def _shorts(lots: Iterable[OpenLot], *, holder: str) -> list[str]:
    return [f"{lot.exact_qty:f} {lot.symbol} sold short by {holder}" for lot in lots if lot.side is not OrderSide.BUY]


def _external_holdings(conn: sqlite3.Connection, fees: BudgetFees) -> tuple[list[Holding], list[str]]:
    """Shares bought outside every bot, at FIFO cost, one holding per symbol.

    The execution population is the one ``_external_cash_claim`` already
    proved complete; each fill's symbol is its external order's. A fill whose
    order or execution time is unknown cannot be lotted, so it is named.
    """
    symbols = {order.broker_order_id: order.symbol for order in external_orders(conn)}
    records: list[FillRecord] = []
    unpriced: list[str] = []
    for fill in fees.external_fills:
        symbol = symbols.get(fill.native_order_id) if fill.native_order_id is not None else None
        if symbol is None or fill.occurred_at_ms is None:
            unpriced.append(f"{fill.quantity:f} shares from outside order {fill.native_order_id or fill.fill_id}")
            continue
        records.append(FillRecord(
            account_id="", sid=f"external:{symbol}", intent_id=fill.fill_id, order_ref=fill.native_order_id or fill.fill_id,
            event_key=fill.fill_id, symbol=symbol, side=fill.side, quantity=float(fill.quantity),
            fill_price=float(fill.price), filled_at_ms=fill.occurred_at_ms, fee=None,
            native_order_id=fill.native_order_id, recorded_at_ms=fill.observed_at_ms,
        ))
    fifo = compute_fifo_pnl(sorted(records, key=lambda record: (record.filled_at_ms, record.event_key)))
    held: dict[str, Decimal] = {}
    for lot in fifo.open_lots:
        if lot.side is OrderSide.BUY:
            held[lot.symbol] = held.get(lot.symbol, ZERO) + lot.exact_qty * lot.exact_cost
    unpriced += _shorts(fifo.open_lots, holder="outside orders")
    return [Holding(f"external:{symbol}", None, cost, ZERO) for symbol, cost in sorted(held.items())], unpriced


def bots_holding_money(conn: sqlite3.Connection) -> frozenset[str]:
    """Bots with position cost or still-claimed money above zero (PRD #2560 D7).

    The fact-level twin of ``account_money``'s stopped-slice rule
    (``position_cost + still_claimed > 0``), read without a cash observation
    so Home can group its bots while the money bar cannot be drawn. Still
    claimed is exactly what the bar prices it as: ``entry_cash_claims``'
    unfilled remainder (nothing for a dead order, else its quantity less its
    effective fills) and its fee -- the one definition, never a second copy
    in SQL. Position cost is a nonzero attributed position. A stopped bot in
    this set is holding; one outside it is finished.
    """
    with money_context():
        claimed = {
            claim.strategy_instance_id
            for claim in entry_cash_claims(conn, seen_before_ms=0)
            if claim.unfilled_cost + claim.unfilled_fee > ZERO
        }
    positioned = {
        str(row[0])
        for row in conn.execute(
            "SELECT strategy_instance_id, attributed_qty FROM positions WHERE strategy_instance_id IS NOT NULL"
        )
        if position_quantity_is_nonzero(float(row[1]))
    }
    return frozenset(claimed | positioned)


@dataclass(frozen=True)
class BotResult:
    """A bot's whole life in money: what its balance gained, and how it traded."""

    result: Decimal
    trade_count: int


def project_bot_results(
    conn: sqlite3.Connection, *, fees: BudgetFees, strategy_instance_ids: Sequence[str],
) -> dict[str, BotResult]:
    """Each bot's whole-life result: what its balance gained over its budget.

    Formula: result = canonical FIFO gross realized P&L - the bot's attributed
      fees (``budgets.deployment_budget``'s balance - commitment); trade_count
      = the bot's effective executions.
    Reference: https://github.com/tim1016/learn-ai/issues/2560 (Finished rows);
      money semantics are PRD #2540's, unchanged.
    Canonical implementation: this composition; FIFO stays ``fifo_pnl.py`` and
      fees the canonical fee reconciler.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_bot_results.py.
    """
    if not fees.known:
        raise BudgetUnavailable("Fee evidence is unresolved: " + "; ".join(fees.unresolved))
    account_id = conn.execute("SELECT account_id FROM control_meta WHERE id=1").fetchone()[0]
    records = effective_fill_records(conn, account_id=account_id, strategy_instance_ids=strategy_instance_ids)
    results: dict[str, BotResult] = {}
    with money_context():
        for sid in strategy_instance_ids:
            subject_id = bot_subject_id(sid)
            fills = [fill for fill in records if fill.sid == subject_id]
            realized = normalize_money(compute_fifo_pnl(fills).exact_realized_pnl)
            results[sid] = BotResult(result=realized - fees.total_for(subject_id), trade_count=len(fills))
    return results


class BotResultsMemo:
    """The last whole-life results read, keyed by the custody revision it read.

    A Finished bot's result moves only with a custody transition -- a fill, a
    correction, or new fee evidence, which the Clerk records as a transition
    too -- and every transition advances ``control_revision``. So a roster
    poll at an unchanged revision reuses the last answer instead of running
    the lifetime fee projection again. An unresolved read is never kept.
    """

    def __init__(self) -> None:
        self._last: tuple[tuple[int, tuple[str, ...]], dict[str, BotResult]] | None = None

    def get(self, key: tuple[int, tuple[str, ...]]) -> dict[str, BotResult] | None:
        last = self._last
        return dict(last[1]) if last is not None and last[0] == key else None

    def put(self, key: tuple[int, tuple[str, ...]], results: dict[str, BotResult]) -> None:
        self._last = (key, dict(results))


def read_bot_results(
    db_path: Path, *, now_ms: int, fee_evidence_checked_at_ms: int | None, strategy_instance_ids: Sequence[str],
    memo: BotResultsMemo | None = None,
) -> dict[str, BotResult]:
    """``project_bot_results`` on its own query-only snapshot of the custody file.

    A roster poll reads whole-life results without ever holding the Clerk's
    writer: its lifetime fee projection is the costly part of the read, so it
    is skipped when ``memo`` already holds this snapshot's revision. Blocking
    work: callers on the event loop run it in a worker thread.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution

    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        revision = int(conn.execute("SELECT control_revision FROM control_meta WHERE id = 1").fetchone()[0])
        key = (revision, tuple(strategy_instance_ids))
        cached = None if memo is None else memo.get(key)
        if cached is not None:
            return cached
        fees = custody_fee_attribution(conn, now_ms=now_ms, evidence_checked_at_ms=fee_evidence_checked_at_ms)
        results = project_bot_results(conn, fees=fees, strategy_instance_ids=strategy_instance_ids)
        if memo is not None:
            memo.put(key, results)
        return results
    finally:
        conn.close()


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
        raise BudgetUnavailable("An earlier deployment without a budget is still running. Stop it first: " + legacy[0])
    # Manual custody remains independent. Until its order outcome is known,
    # refusing a new budget is safer than inventing its missing cash claim.
    manual = conn.execute(
        "SELECT 1 FROM effect_operations WHERE kind='MANUAL_ORDER' "
        "AND state NOT IN ('succeeded','failed','rejected','cancelled') LIMIT 1"
    ).fetchone()
    if manual:
        raise BudgetUnavailable("A manual order is still working, so its cash is not yet known. Resolve it first.")
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
