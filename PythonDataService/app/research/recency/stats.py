"""Recency Chart statistics.

Formula: (a) trade_dollar_pnl = pnl_pts * quantity, the realized dollar PnL
implied by the engine's own trade log (gross of commission — the strategy
trade log this is sourced from does not deduct fees; see
``EngineTradeResponse.pnl_pts`` provenance). (b) total_pnl = the order-
independent sum of trade_dollar_pnl for every trade whose entry falls
inside ``[window_start_ms, window_end_ms]`` — the Recency Chart's hero-combo
selection metric. (c) sharpe = mean / sample-stddev (ddof=1) of a combo's
per-trade ``pnl_pct`` returns, non-annualized, undefined below
``min_trades`` or when variance is zero — the Recency Chart's bar-opacity
metric. (d) holding_sessions = the count of scheduled NYSE sessions a trade
spans, via the canonical calendar module.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design spec
docs/superpowers/specs/2026-08-16-recency-chart-design.md §7.1.
Canonical implementation: this file.
Validated against: tests/research/recency/test_stats.py.

Every number here is Python-authored per AGENTS.md #5 ("Python owns all
math") — .NET only passes these through and Angular only renders them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import trading_session_count

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TradeForStats:
    """The subset of a persisted trade needed to compute recency statistics."""

    entry_ms: int
    exit_ms: int
    pnl_pts: float
    pnl_pct: float
    quantity: int


def ms_to_et_date(ms: int):
    """Resolve an ``int64 ms UTC`` instant to its America/New_York calendar date.

    Public so callers elsewhere in the ``recency`` package can derive a
    trading date (per ``.claude/rules/temporal-rigor.md``) without each
    re-deriving their own ET conversion.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_ET).date()


def holding_sessions(entry_ms: int, exit_ms: int) -> int:
    """Count scheduled NYSE sessions spanned by ``[entry_ms, exit_ms]``, inclusive."""
    return trading_session_count(ms_to_et_date(entry_ms), ms_to_et_date(exit_ms))


def trade_dollar_pnl(trade: TradeForStats) -> float:
    """Realized dollar PnL implied by the trade's points and filled quantity."""
    return trade.pnl_pts * trade.quantity


def total_pnl(trades: list[TradeForStats], window_start_ms: int, window_end_ms: int) -> float:
    """Sum of dollar PnL for trades entering within the window (order-independent)."""
    return sum(
        trade_dollar_pnl(trade) for trade in trades if window_start_ms <= trade.entry_ms <= window_end_ms
    )


def sharpe(trades: list[TradeForStats], min_trades: int = 2) -> float | None:
    """Mean / sample-stddev (ddof=1) of per-trade ``pnl_pct``; ``None`` if undefined."""
    if len(trades) < min_trades:
        return None
    returns = [trade.pnl_pct for trade in trades]
    std = statistics.stdev(returns)
    if std == 0:
        return None
    return statistics.mean(returns) / std
