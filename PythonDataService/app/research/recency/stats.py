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
from datetime import UTC, date, datetime
from math import fsum
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


@dataclass(frozen=True)
class HeroTrade:
    """One already-net trade value used for visible-window hero selection."""

    entry_ms: int
    pnl: float


@dataclass(frozen=True)
class HeroCandidate:
    """One parameter combo and the canonical trades claimed by that run."""

    recency_run_id: int
    symbol: str
    strategy_key: str
    params_hash: str
    trades: tuple[HeroTrade, ...]


@dataclass(frozen=True)
class HeroSelection:
    """Python-authored winner for one symbol/strategy pair."""

    recency_run_id: int
    symbol: str
    strategy_key: str
    params_hash: str
    total_pnl: float


def _ms_to_et_date(ms: int) -> date:
    """Resolve an ``int64 ms UTC`` instant to its America/New_York calendar date.

    Kept private so a language-native ``date`` never crosses the function's
    module boundary; canonical temporal values remain ``int64 ms UTC``.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(_ET).date()


def holding_sessions(entry_ms: int, exit_ms: int) -> int:
    """Count scheduled NYSE sessions spanned by ``[entry_ms, exit_ms]``, inclusive."""
    return trading_session_count(_ms_to_et_date(entry_ms), _ms_to_et_date(exit_ms))


def ms_to_et_date_string(ms: int) -> str:
    """Format an internal engine date without exposing a native date object."""
    return _ms_to_et_date(ms).isoformat()


def trade_dollar_pnl(trade: TradeForStats, commission_per_order: float = 0.0) -> float:
    """Realized dollar PnL net of two flat per-order commissions (entry + exit).

    Formula: gross (pnl_pts * quantity) - 2 * commission_per_order — the same
    flat-fee branch as the canonical
    ``app.routers.engine._persisted_trade_net_pnl`` (compatibility_profile=None),
    which this mirrors rather than duplicates independently (CLAUDE.md
    guiding philosophy #5). ``commission_per_order`` defaults to 0.0 so an
    uncommissioned launch's PnL is exactly the gross figure.
    Validated against:
      ``tests/research/recency/test_stats.py::TestTradeDollarPnl::test_matches_the_canonical_engine_flat_fee_formula``.
    """
    gross = trade.pnl_pts * trade.quantity
    return gross - (2 * commission_per_order)


def total_pnl(
    trades: list[TradeForStats], window_start_ms: int, window_end_ms: int, commission_per_order: float = 0.0
) -> float:
    """Sum of net dollar PnL for trades entering within the window (order-independent)."""
    return fsum(
        trade_dollar_pnl(trade, commission_per_order)
        for trade in trades
        if window_start_ms <= trade.entry_ms <= window_end_ms
    )


def select_window_heroes(
    candidates: list[HeroCandidate],
    window_start_ms: int,
    window_end_ms: int,
) -> list[HeroSelection]:
    """Select the highest net-PnL combo per symbol/strategy in a window.

    Formula: total_pnl(c, W) = fsum(t.pnl for t in c if t.entry_ms in W);
      hero(symbol, strategy, W) = argmax_c(total_pnl(c, W)).
    Reference: docs/superpowers/specs/2026-08-16-recency-chart-design.md
      D5 and section 7.1.
    Canonical implementation: this file.
    Validated against: tests/research/recency/test_stats.py::TestSelectWindowHeroes
      and tests/routers/test_recency_hero_endpoint.py.
    """
    winners: dict[tuple[str, str], HeroSelection] = {}
    for candidate in candidates:
        visible_pnl = [
            trade.pnl for trade in candidate.trades if window_start_ms <= trade.entry_ms <= window_end_ms
        ]
        if not visible_pnl:
            continue
        selection = HeroSelection(
            recency_run_id=candidate.recency_run_id,
            symbol=candidate.symbol,
            strategy_key=candidate.strategy_key,
            params_hash=candidate.params_hash,
            total_pnl=fsum(visible_pnl),
        )
        key = (candidate.symbol, candidate.strategy_key)
        current = winners.get(key)
        if current is None or (selection.total_pnl, selection.recency_run_id) > (
            current.total_pnl,
            current.recency_run_id,
        ):
            winners[key] = selection
    return [winners[key] for key in sorted(winners)]


def sharpe(trades: list[TradeForStats], min_trades: int = 2) -> float | None:
    """Mean / sample-stddev (ddof=1) of per-trade ``pnl_pct``; ``None`` if undefined."""
    if len(trades) < min_trades:
        return None
    returns = [trade.pnl_pct for trade in trades]
    std = statistics.stdev(returns)
    if std == 0:
        return None
    return statistics.mean(returns) / std
