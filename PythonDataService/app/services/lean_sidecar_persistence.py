"""Persistence layer: normalize LEAN sidecar output into StrategyExecution rows.

Consumed by lean_sidecar_service.run_trusted_sample() at the tail of a successful
run. Reads the normalized result.json, pairs filled order events into round-trip
trades, synthesizes a mark-to-market exit for any half-open position, computes
aggregate KPIs, and writes one StrategyExecution row + N BacktestTrade rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
