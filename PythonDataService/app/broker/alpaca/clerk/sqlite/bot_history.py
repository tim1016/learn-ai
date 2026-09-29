"""Every bot one custody database holds, run by run (Bot history, #2574).

One read over one query-only snapshot of a custody file: each registered
bot's identity, its runs (start, stop), its effective fills and its orders
counted per run, its budget, and its whole-life result and fees. It is a
projection of facts the Clerk already keeps; it adds no FIFO, no fee rule
and no second store:

* **Transactions** are the canonical effective fills after broker
  corrections (``effective_fill_records``) -- the same population, and so
  the same count, as ``project_bot_results``' ``trade_count``. A fill
  belongs to the run its order's effect operation names
  (``orders -> effect_operations.run_id``).
* **Orders** are the bot's own order rows, by their immutable provenance
  (``orders.effect_operation_id``), counted by the broker's last reported
  state: *sent* once the broker has reported the order at all, then
  *filled*, *cancelled* (cancelled or expired) or *rejected*. An order the
  broker never acknowledged was never sent.
* **Result and fees** are per bot, never per run: ``project_bot_results``
  and the canonical fee reconciler's per-subject total. An old bot with
  several runs (the retired Resume) has one result across all of them.

A fill or order whose effect operation names no run (work done for a bot
after its run ended, such as a later flatten) counts toward the bot, never
toward a run, so a run's counts are exact and the bot's totals stay equal to
its ``trade_count``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.money import money_context
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.budget_projection import (
    BotResult,
    BudgetFees,
    BudgetUnavailable,
    bots_holding_money,
    project_bot_results,
)
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.models import BotConfigResource

#: The broker's order states each order count reads. Lower-cased: Alpaca's
#: own spelling ("canceled") is the stored one.
_FILLED_STATES = ("filled",)
_CANCELLED_STATES = ("canceled", "expired")
_REJECTED_STATES = ("rejected",)


@dataclass(frozen=True)
class OrderCounts:
    """One bot's (or one run's) orders by the broker's last reported state."""

    sent: int = 0
    filled: int = 0
    cancelled: int = 0
    rejected: int = 0

    def plus(self, broker_state: str | None) -> OrderCounts:
        """These counts with one more order, in its reported state."""
        if broker_state is None:
            return self
        state = broker_state.lower()
        return OrderCounts(
            sent=self.sent + 1,
            filled=self.filled + int(state in _FILLED_STATES),
            cancelled=self.cancelled + int(state in _CANCELLED_STATES),
            rejected=self.rejected + int(state in _REJECTED_STATES),
        )


@dataclass(frozen=True)
class RunFacts:
    """One run of one bot: when it ran, and what it traded."""

    run_id: str
    lifecycle_run_id: str
    active: bool
    started_at_ms: int
    stopped_at_ms: int | None
    transactions: int
    orders: OrderCounts
    #: The run was stopped with a flatten (a STOP_AND_FLATTEN operation).
    flattened: bool


@dataclass(frozen=True)
class BotFacts:
    """One registered bot's whole history in this custody database."""

    strategy_instance_id: str
    symbol: str
    created_at_ms: int
    retired_at_ms: int | None
    config: BotConfigResource | None
    #: Newest first.
    runs: tuple[RunFacts, ...]
    transactions: int
    orders: OrderCounts
    holds_money: bool
    live_custody: bool
    committed_cents: int | None
    result: Decimal | None
    fees: Decimal | None


@dataclass(frozen=True)
class CustodyHistory:
    """Every bot one custody database holds, from one snapshot.

    ``money_unavailable`` names why results and fees are unknown (the fee
    evidence cannot vouch for them); every bot then carries ``None`` for
    both, never zero.
    """

    account_id: str
    bots: tuple[BotFacts, ...]
    money_unavailable: str | None


def project_custody_history(
    conn: sqlite3.Connection,
    *,
    fees: BudgetFees | None,
    fees_unavailable: str | None = None,
    strategy_instance_ids: Sequence[str] | None = None,
) -> CustodyHistory:
    """Project every bot (or the named ones) on the caller's snapshot.

    ``fees`` is the canonical fee reconciler's answer for this snapshot, or
    ``None`` with ``fees_unavailable`` naming why it could not be read.

    Formula: transactions = |effective fills| (per run: those whose order's
      effect operation names the run); orders = order rows by provenance,
      bucketed by broker state; result = ``project_bot_results``; fees = the
      fee reconciler's per-subject total.
    Reference: https://github.com/tim1016/learn-ai/issues/2574; money
      semantics are PRD #2540's, unchanged.
    Canonical implementation: this composition; the counts reuse
      ``effective_fill_records`` and the money ``project_bot_results``.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_bot_history.py.
    """
    account_id = str(conn.execute("SELECT account_id FROM control_meta WHERE id = 1").fetchone()[0])
    registrations = [
        row for row in reads.strategy_instances(conn)
        if strategy_instance_ids is None or row["strategy_instance_id"] in strategy_instance_ids
    ]
    sids = [str(row["strategy_instance_id"]) for row in registrations]
    if not sids:
        return CustodyHistory(account_id=account_id, bots=(), money_unavailable=fees_unavailable)

    order_runs: dict[str, tuple[str, str | None]] = {}
    orders_by_bot: dict[str, OrderCounts] = {}
    orders_by_run: dict[str, OrderCounts] = {}
    for row in conn.execute(
        "SELECT o.order_ref, o.broker_state, e.strategy_instance_id, e.run_id "
        "FROM orders o JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
        "WHERE e.strategy_instance_id IS NOT NULL"
    ):
        sid, run_id, state = str(row["strategy_instance_id"]), row["run_id"], row["broker_state"]
        order_runs[str(row["order_ref"])] = (sid, run_id)
        orders_by_bot[sid] = orders_by_bot.get(sid, OrderCounts()).plus(state)
        if run_id is not None:
            orders_by_run[run_id] = orders_by_run.get(run_id, OrderCounts()).plus(state)

    fills_by_bot: dict[str, int] = {}
    fills_by_run: dict[str, int] = {}
    for fill in effective_fill_records(conn, account_id=account_id, strategy_instance_ids=sids):
        sid, run_id = order_runs[fill.order_ref]
        if fill.sid != bot_subject_id(sid):
            continue
        fills_by_bot[sid] = fills_by_bot.get(sid, 0) + 1
        if run_id is not None:
            fills_by_run[run_id] = fills_by_run.get(run_id, 0) + 1

    flattened_runs = {
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT run_id FROM effect_operations "
            "WHERE kind = 'STOP_AND_FLATTEN' AND run_id IS NOT NULL"
        )
    }
    runs_by_bot: dict[str, list[RunFacts]] = {}
    for row in conn.execute(
        "SELECT run_id, strategy_instance_id, lifecycle_run_id, state, started_at_ms, stopped_at_ms "
        "FROM runs ORDER BY started_at_ms DESC, run_id DESC"
    ):
        run_id = str(row["run_id"])
        runs_by_bot.setdefault(str(row["strategy_instance_id"]), []).append(RunFacts(
            run_id=run_id,
            lifecycle_run_id=str(row["lifecycle_run_id"]),
            active=row["state"] == "ACTIVE",
            started_at_ms=int(row["started_at_ms"]),
            stopped_at_ms=None if row["stopped_at_ms"] is None else int(row["stopped_at_ms"]),
            transactions=fills_by_run.get(run_id, 0),
            orders=orders_by_run.get(run_id, OrderCounts()),
            flattened=run_id in flattened_runs,
        ))

    budgets = {
        str(row["strategy_instance_id"]): int(row["committed_cents"])
        for row in conn.execute("SELECT strategy_instance_id, committed_cents FROM deployment_budgets")
    }
    holding = bots_holding_money(conn)
    live_custody = reads.strategy_instances_with_live_custody(conn)
    results: dict[str, BotResult] = {}
    money_unavailable = fees_unavailable
    if fees is not None and money_unavailable is None:
        try:
            results = project_bot_results(conn, fees=fees, strategy_instance_ids=sids)
        except BudgetUnavailable as exc:
            money_unavailable = str(exc)

    known = fees if money_unavailable is None else None
    bots: list[BotFacts] = []
    with money_context():
        for registration in registrations:
            sid = str(registration["strategy_instance_id"])
            bots.append(BotFacts(
                strategy_instance_id=sid,
                symbol=str(registration["symbol"]),
                created_at_ms=int(registration["created_at_ms"]),
                retired_at_ms=(
                    None if registration["retired_at_ms"] is None else int(registration["retired_at_ms"])
                ),
                config=reads.bot_config(conn, sid),
                runs=tuple(runs_by_bot.get(sid, ())),
                transactions=fills_by_bot.get(sid, 0),
                orders=orders_by_bot.get(sid, OrderCounts()),
                holds_money=sid in holding,
                live_custody=sid in live_custody,
                committed_cents=budgets.get(sid),
                result=None if known is None else results[sid].result,
                fees=None if known is None else known.total_for(bot_subject_id(sid)),
            ))
    return CustodyHistory(account_id=account_id, bots=tuple(bots), money_unavailable=money_unavailable)


def read_custody_history(
    db_path: Path,
    *,
    now_ms: int,
    fee_evidence_checked_at_ms: int | None,
    strategy_instance_ids: Sequence[str] | None = None,
) -> CustodyHistory:
    """``project_custody_history`` on its own query-only snapshot of a custody file.

    Never under the Clerk's writer, like ``read_bot_results``: a history read
    must not hold up trading. ``fee_evidence_checked_at_ms`` is the owning
    process's fee-evidence freshness (``None`` for a file no running Clerk
    owns: a real account's fees are then unknown, while a simulated one's
    need no broker evidence). Blocking work: callers on the event loop run
    it in a worker thread.
    """
    from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution

    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        fees = custody_fee_attribution(conn, now_ms=now_ms, evidence_checked_at_ms=fee_evidence_checked_at_ms)
        if fees.known:
            return project_custody_history(conn, fees=fees, strategy_instance_ids=strategy_instance_ids)
        return project_custody_history(
            conn, fees=None, strategy_instance_ids=strategy_instance_ids,
            fees_unavailable="Fee evidence is unresolved: " + "; ".join(fees.unresolved),
        )
    finally:
        conn.close()
