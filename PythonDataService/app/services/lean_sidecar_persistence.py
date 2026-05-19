"""Persistence layer: normalize LEAN sidecar output into StrategyExecution rows.

Consumed by lean_sidecar_service.run_trusted_sample() at the tail of a successful
run. Reads the normalized result.json, pairs filled order events into round-trip
trades, synthesizes a mark-to-market exit for any half-open position, computes
aggregate KPIs, and writes one StrategyExecution row + N BacktestTrade rows.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OpenLot:
    """A buy fill that has not yet been matched with a sell."""

    entry_ms_utc: int
    entry_price: float
    quantity: float
    fees: list[float] = field(default_factory=list)


@dataclass
class PairedTrade:
    """Round-trip trade reconstructed from a buy/sell event pair."""

    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    signal_reason: str
    is_synthetic_exit: bool


def pair_order_events(
    events: Sequence[dict[str, Any]],
    signal_reason: str = "EMA crossover exit (5-bar time stop)",
) -> tuple[list[PairedTrade], OpenLot | None]:
    """Pair buy/sell filled events into round-trip trades.

    Returns (trades, leftover_open_lot). If the events end on an unmatched buy
    the caller is responsible for synthesizing an MTM exit.

    Raises NotImplementedError if a second buy arrives without an intervening
    sell (pyramiding). EMA crossover and buy-and-hold both have pyramiding=1
    so this branch is defensive.
    """
    fills = [e for e in events if e.get("status") == "filled"]
    open_lot: OpenLot | None = None
    trade_number = 0
    trades: list[PairedTrade] = []

    for fill in fills:
        direction = fill["direction"]
        ms_utc = int(fill["ms_utc"])
        price = float(fill["fill_price"])
        qty = float(fill["fill_quantity"])
        fee = float(fill.get("order_fee_amount") or 0.0)

        if direction == "buy":
            if open_lot is None:
                open_lot = OpenLot(
                    entry_ms_utc=ms_utc,
                    entry_price=price,
                    quantity=qty,
                    fees=[fee],
                )
            else:
                raise NotImplementedError("Pyramiding not supported in Phase 1; expected at most one open lot")
        elif direction == "sell":
            if open_lot is None:
                # Defensive: short selling not expected for current templates.
                continue
            trade_number += 1
            entry_fees = sum(open_lot.fees)
            pnl = (price - open_lot.entry_price) * open_lot.quantity - entry_fees - fee
            trades.append(
                PairedTrade(
                    trade_number=trade_number,
                    entry_ms_utc=open_lot.entry_ms_utc,
                    exit_ms_utc=ms_utc,
                    entry_price=open_lot.entry_price,
                    exit_price=price,
                    quantity=open_lot.quantity,
                    pnl=pnl,
                    signal_reason=signal_reason,
                    is_synthetic_exit=False,
                )
            )
            open_lot = None

    return trades, open_lot


def finalize_open_lot_as_synthetic(
    open_lot: OpenLot,
    equity_curve: Sequence[dict[str, Any]],
    starting_cash: float,
    trade_number: int,
) -> PairedTrade:
    """Synthesize an MTM exit at the last equity-curve point.

    Reconstructs exit price by reversing the portfolio-value identity:
        equity_value = cash_remaining + qty * exit_price
        cash_remaining = starting_cash - qty * entry_price - sum(fees)
    Solving:
        exit_price = (equity_value - starting_cash + qty * entry_price + sum(fees)) / qty
    """
    if not equity_curve:
        raise ValueError("equity_curve is empty — cannot synthesize MTM exit")

    last_point = equity_curve[-1]
    last_ms = int(last_point["ms_utc"])
    last_value = float(last_point["value"])
    entry_fees = sum(open_lot.fees)

    exit_price = (
        last_value - starting_cash + open_lot.entry_price * open_lot.quantity + entry_fees
    ) / open_lot.quantity

    pnl = (exit_price - open_lot.entry_price) * open_lot.quantity - entry_fees

    return PairedTrade(
        trade_number=trade_number,
        entry_ms_utc=open_lot.entry_ms_utc,
        exit_ms_utc=last_ms,
        entry_price=open_lot.entry_price,
        exit_price=exit_price,
        quantity=open_lot.quantity,
        pnl=pnl,
        signal_reason="EndOfAlgorithm:MTM (synthetic exit)",
        is_synthetic_exit=True,
    )


@dataclass
class AggregateKpis:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    final_equity: float
    win_rate: float


def compute_aggregates(
    trades: Sequence[PairedTrade],
    starting_cash: float,
    total_fees: float,
) -> AggregateKpis:
    """Compute aggregate KPIs from a list of round-trip trades."""
    total_pnl = sum(t.pnl for t in trades)
    winning = sum(1 for t in trades if t.pnl > 0)
    losing = sum(1 for t in trades if t.pnl < 0)
    win_rate = winning / len(trades) if trades else 0.0
    return AggregateKpis(
        total_trades=len(trades),
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=total_pnl,
        final_equity=starting_cash + total_pnl - total_fees,
        win_rate=win_rate,
    )


@dataclass
class NormalizedResult:
    """Schema-versioned view of normalized/result.json."""

    parser_version: str
    order_events: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    statistics: dict[str, Any]
    runtime_statistics: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> NormalizedResult:
        data = json.loads(path.read_text())
        return cls(
            parser_version=data.get("parser_version", "unknown"),
            order_events=data.get("order_events") or [],
            equity_curve=data.get("equity_curve") or [],
            statistics=data.get("statistics") or {},
            runtime_statistics=data.get("runtime_statistics") or {},
        )


def build_persist_payload(
    workspace_path: Path,
    run_id: str,
    starting_cash: float,
    symbol: str,
    algorithm_name: str,
    start_date_ms: int,
    end_date_ms: int,
) -> dict[str, Any]:
    """Build a JSON-serializable payload to POST to the .NET persist endpoint.

    This function is pure: it reads normalized/result.json from disk and computes
    aggregates + paired trades using pair_order_events, finalize_open_lot_as_synthetic,
    and compute_aggregates. It performs no DB writes and no HTTP calls.

    The .NET endpoint at POST /api/backtest-runs/persist-lean is responsible for
    persisting the payload into the StrategyExecution + BacktestTrade tables.

    If the workspace has no normalized/result.json (LEAN crashed before output),
    returns a "failed run" payload with TotalTrades=0 and the error noted in
    lean_statistics. The .NET endpoint should still persist this so the failed
    run appears in the unified history.

    All timestamps in the returned payload are int64 ms UTC (canonical).
    """
    result_path = workspace_path / "normalized" / "result.json"

    if not result_path.exists():
        return _failed_run_payload(
            run_id=run_id,
            starting_cash=starting_cash,
            symbol=symbol,
            algorithm_name=algorithm_name,
            start_date_ms=start_date_ms,
            end_date_ms=end_date_ms,
            workspace_path=workspace_path,
            error="No normalized/result.json — LEAN run did not produce output",
        )

    normalized = NormalizedResult.from_path(result_path)

    paired_trades, open_lot = pair_order_events(normalized.order_events)
    if open_lot is not None:
        synthetic = finalize_open_lot_as_synthetic(
            open_lot=open_lot,
            equity_curve=normalized.equity_curve,
            starting_cash=starting_cash,
            trade_number=len(paired_trades) + 1,
        )
        paired_trades.append(synthetic)

    total_fees = sum(
        float(ev.get("order_fee_amount") or 0.0) for ev in normalized.order_events if ev.get("status") == "filled"
    )
    agg = compute_aggregates(
        trades=paired_trades,
        starting_cash=starting_cash,
        total_fees=total_fees,
    )

    return {
        "lean_run_id": run_id,
        "source": "lean-sidecar",
        "strategy_name": algorithm_name,
        "symbol": symbol,
        "starting_cash": starting_cash,
        "start_date_ms": start_date_ms,
        "end_date_ms": end_date_ms,
        "total_trades": agg.total_trades,
        "winning_trades": agg.winning_trades,
        "losing_trades": agg.losing_trades,
        "total_pnl": agg.total_pnl,
        "total_fees": total_fees,
        "final_equity": agg.final_equity,
        "win_rate": agg.win_rate,
        "trades": [
            {
                "trade_number": t.trade_number,
                "entry_ms_utc": t.entry_ms_utc,
                "exit_ms_utc": t.exit_ms_utc,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "signal_reason": t.signal_reason,
                "is_synthetic_exit": t.is_synthetic_exit,
            }
            for t in paired_trades
        ],
        "lean_statistics": {
            "statistics": normalized.statistics,
            "runtime_statistics": normalized.runtime_statistics,
            "parser_version": normalized.parser_version,
            "workspace_path": str(workspace_path),
        },
    }


def _failed_run_payload(
    *,
    run_id: str,
    starting_cash: float,
    symbol: str,
    algorithm_name: str,
    start_date_ms: int,
    end_date_ms: int,
    workspace_path: Path,
    error: str,
) -> dict[str, Any]:
    """Build a zero-trade payload for a LEAN run that failed or produced no result."""
    return {
        "lean_run_id": run_id,
        "source": "lean-sidecar",
        "strategy_name": algorithm_name,
        "symbol": symbol,
        "starting_cash": starting_cash,
        "start_date_ms": start_date_ms,
        "end_date_ms": end_date_ms,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "final_equity": starting_cash,
        "win_rate": 0.0,
        "trades": [],
        "lean_statistics": {
            "error": error,
            "workspace_path": str(workspace_path),
        },
    }
