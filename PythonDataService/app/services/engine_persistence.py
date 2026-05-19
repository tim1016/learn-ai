"""Persistence layer: send in-process BacktestEngine output to the .NET backend.

Sibling to ``lean_sidecar_persistence``. Where the LEAN sidecar normalizes an
on-disk LEAN workspace into a persist payload, this module converts captured
engine trades (entry/exit ms_utc, prices, quantities, pnl) into the same
``PersistLeanRunPayload`` shape that the .NET endpoint already accepts.

The payload sets ``source="engine"`` and ``lean_run_id=None`` — the .NET service
accepts both as of PR 4. Engine-source persists have no external idempotency
key, so each call inserts a new ``StrategyExecution`` row. The caller (typically
a spec-strategy runner) is responsible for not double-persisting.

The HTTP transport is shared with the LEAN path via
``lean_sidecar_persistence.persist_via_dotnet`` — both routes use the same
``POST /api/backtest-runs/persist-lean`` endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.lean_sidecar_persistence import persist_via_dotnet

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

    All timestamps in the returned payload are ``int64`` ms UTC. Decimals are
    serialized as numbers by the consuming JSON encoder (Pydantic v2 / Python's
    default json.dumps will require ``str(Decimal)`` or ``float`` coercion at
    the wire boundary — see ``persist_via_dotnet`` for the conversion).
    """
    aggregates = compute_aggregates(trades, starting_cash, total_fees=total_fees)

    return {
        "lean_run_id": None,
        "source": "engine",
        "strategy_name": strategy_name,
        "symbol": symbol,
        "starting_cash": float(starting_cash),
        "start_date_ms": start_date_ms,
        "end_date_ms": end_date_ms,
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
    }


async def persist_engine_run(
    *,
    base_url: str,
    strategy_name: str,
    symbol: str,
    starting_cash: Decimal,
    start_date_ms: int,
    end_date_ms: int,
    trades: list[EngineTrade],
    total_fees: Decimal = Decimal("0"),
    extra_statistics: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> int | None:
    """Build the engine persist payload and POST it to the .NET backend.

    Returns the assigned ``StrategyExecution.Id`` on success, or ``None`` on
    HTTP/network failure. Mirrors the failure semantics of
    ``lean_sidecar_persistence.persist_via_dotnet``: persistence failures must
    not abort the caller; the in-memory trade list remains authoritative and
    can be retried.
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
    return await persist_via_dotnet(payload, base_url=base_url, timeout_seconds=timeout_seconds)
