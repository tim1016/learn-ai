"""Persistence adapter for in-process ``BacktestEngine`` output captured trade by trade.

Sibling to ``lean_sidecar_persistence``. Where the LEAN sidecar normalizes an
on-disk LEAN workspace into a persist payload, this module converts captured
engine trades (entry/exit ms_utc, prices, quantities, pnl) into the same
canonical payload and hands it to the one repository write
(``app.research.backtest_runs.service``, PRD #1929).

The payload sets ``source="engine"`` and ``lean_run_id=None``. Engine-source
persists have no external idempotency key, so each call inserts a new run
row. The caller (the spec-strategy runner) is responsible for not
double-persisting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.research.backtest_runs.records import utc_date_iso
from app.research.backtest_runs.service import persist_run_payload
from app.research.documentation.analytical_metric_catalog import metric_documentation_context_for_source

__all__ = [
    "EngineAggregateKpis",
    "EngineTrade",
    "build_engine_persist_payload",
    "compute_aggregates",
    "persist_engine_run",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineTrade:
    """One closed round-trip trade as observed in-process from BacktestEngine.

    ``quantity`` is the filled share count at entry (and equal at exit since the
    spec engine doesn't pyramid). ``pnl`` is net of fees if the engine's fill
    model includes them — for the default ``FillModel(commission_per_order=0)``
    used in parity tests, fees are zero.
    """

    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    signal_reason: str = ""
    is_synthetic_exit: bool = False


@dataclass(frozen=True)
class EngineAggregateKpis:
    """Aggregate KPIs computed from a list of EngineTrade."""

    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: Decimal
    final_equity: Decimal
    win_rate: float


def compute_aggregates(
    trades: list[EngineTrade],
    starting_cash: Decimal,
    total_fees: Decimal = Decimal("0"),
) -> EngineAggregateKpis:
    """Compute KPI aggregates from a list of EngineTrade.

    ``final_equity = starting_cash + total_pnl`` (per-trade pnl is already net of
    fees if the engine charges them; do NOT subtract ``total_fees`` again here).
    """
    total_pnl = sum((t.pnl for t in trades), start=Decimal("0"))
    winning = sum(1 for t in trades if t.pnl > 0)
    losing = sum(1 for t in trades if t.pnl < 0)
    final_equity = starting_cash + total_pnl
    win_rate = (winning / len(trades)) if trades else 0.0

    return EngineAggregateKpis(
        total_trades=len(trades),
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=total_pnl,
        final_equity=final_equity,
        win_rate=win_rate,
    )


def build_engine_persist_payload(
    *,
    strategy_name: str,
    symbol: str,
    starting_cash: Decimal,
    start_date_ms: int,
    end_date_ms: int,
    trades: list[EngineTrade],
    total_fees: Decimal = Decimal("0"),
    extra_statistics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the engine-source persist payload (source="engine", lean_run_id=None).

    Timestamps stay ``int64`` ms UTC; the window is carried as the trading
    dates the ms anchors name. Decimals are coerced to floats here because the
    row stores double precision.
    """
    aggregates = compute_aggregates(trades, starting_cash, total_fees=total_fees)

    return {
        "lean_run_id": None,
        "source": "engine",
        "strategy_name": strategy_name,
        "symbol": symbol,
        "starting_cash": float(starting_cash),
        "start_date": utc_date_iso(start_date_ms),
        "end_date": utc_date_iso(end_date_ms),
        "total_trades": aggregates.total_trades,
        "winning_trades": aggregates.winning_trades,
        "losing_trades": aggregates.losing_trades,
        "total_pnl": float(aggregates.total_pnl),
        "total_fees": float(total_fees),
        "final_equity": float(aggregates.final_equity),
        "win_rate": aggregates.win_rate,
        "trades": [
            {
                "trade_number": t.trade_number,
                "entry_ms_utc": t.entry_ms_utc,
                "exit_ms_utc": t.exit_ms_utc,
                "entry_price": float(t.entry_price),
                "exit_price": float(t.exit_price),
                "quantity": float(t.quantity),
                "pnl": float(t.pnl),
                "signal_reason": t.signal_reason,
                "is_synthetic_exit": t.is_synthetic_exit,
            }
            for t in trades
        ],
        "lean_statistics": extra_statistics or {},
        "metric_documentation_json": json.dumps(
            metric_documentation_context_for_source("engine"),
            sort_keys=True,
        ),
    }


async def persist_engine_run(
    *,
    strategy_name: str,
    symbol: str,
    starting_cash: Decimal,
    start_date_ms: int,
    end_date_ms: int,
    trades: list[EngineTrade],
    total_fees: Decimal = Decimal("0"),
    extra_statistics: dict[str, Any] | None = None,
) -> int | None:
    """Build the engine persist payload and write it.

    Returns the assigned run id on success, or ``None`` when persistence
    failed. Persistence failures must not abort the caller; the in-memory
    trade list remains authoritative and can be retried.
    """
    payload = build_engine_persist_payload(
        strategy_name=strategy_name,
        symbol=symbol,
        starting_cash=starting_cash,
        start_date_ms=start_date_ms,
        end_date_ms=end_date_ms,
        trades=trades,
        total_fees=total_fees,
        extra_statistics=extra_statistics,
    )
    logger.info(
        "Persisting engine run: strategy=%s symbol=%s trades=%d",
        strategy_name,
        symbol,
        len(trades),
    )
    return await persist_run_payload(payload)
