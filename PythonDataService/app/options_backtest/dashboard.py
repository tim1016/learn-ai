"""Descriptive dashboard statistics derived from recorded Options session results.

Formula: session means = sum(net session dollars)/session count; session PF =
  positive session dollars/absolute negative session dollars. Underwater dollars
  = equity-running session-close peak. Recovery is the first close >= the worst
  dollar drawdown's peak, counted from its trough. Histogram: ten equal exact
  Decimal intervals [low, high), last high inclusive; a constant sample has one bin.
Reference: ADR 0080 for fixed-capital daily returns and session economics; Sharpe
  (1994), The Sharpe Ratio. Session-close scope follows ADR 0078 decision 8.
Canonical implementation: this file for report projections and session-close recovery;
  daily_metrics.py owns the shared daily Sharpe and calendar annualization.
Validated against: tests/options_backtest/test_dashboard.py (hand-worked dollar,
  coverage, recovery and distribution receipts; Sharpe/Sortino atol=1e-12, rtol=0).

The saved summary is unchanged. Minute drawdown and minute recovery cannot be
reconstructed from it. Missing daily evidence is never filled with a zero.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from app.engine.results.statistics import _sample_standard_deviation, _sortino
from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION, session_windows_ms_utc
from app.options_backtest import fills
from app.options_backtest.daily_metrics import (
    annualized_sharpe,
    calendar_annualization,
    decimal_ratio,
    session_economics,
)
from app.options_backtest.results import SessionStatus, SkipReason
from app.options_backtest.statistics import Ratio, RunSummary, equity_path
from app.utils.session_anchors import et_date_at_ms


@dataclass(frozen=True)
class DashboardDay:
    session_open_ms: int
    year: int
    month: int
    underwater_usd: Decimal
    rolling_sharpe: float | None


@dataclass(frozen=True)
class ProfitBin:
    lower_usd: Decimal
    upper_usd: Decimal
    count: int


@dataclass(frozen=True)
class Dashboard:
    annualization_sessions: float | None
    daily_observations: int
    risk_unavailable_reason: str | None
    annualization_unavailable_reason: str | None
    sharpe_unavailable_reason: str | None
    annualized_sharpe: float | None
    annualized_sortino: float | None
    annualized_volatility_percent: float | None
    return_percent: str | None
    average_winning_session_usd: Decimal | None
    average_losing_session_usd: Decimal | None
    average_session_usd: Decimal | None
    session_profit_factor: str | None
    payoff_ratio: str | None
    best_session_usd: Decimal | None
    worst_session_usd: Decimal | None
    profit_without_best_session_usd: Decimal | None
    top_five_profit_percent: str | None
    longest_losing_streak: int
    recovery_sessions: int | None
    recovered_session_open_ms: int | None
    longest_underwater_sessions: int
    ongoing_underwater: bool
    daily: tuple[DashboardDay, ...]
    histogram: tuple[ProfitBin, ...]


def _money_mean(total: Decimal, count: int) -> Decimal | None:
    if count == 0:
        return None
    # Means are display amounts, explicitly rounded to cents; original totals stay exact.
    with localcontext(fills.EXACT):
        return Decimal(round(Fraction(total) * 100 / count)).scaleb(-2)


def _display_ratio(numerator: Decimal, denominator: Decimal | int) -> str | None:
    value = decimal_ratio(numerator, denominator)
    return None if value is None else format(value, ".2f")


def profit_histogram(values: Sequence[Decimal]) -> tuple[ProfitBin, ...]:
    """Exact-dollar bins; the float/cent trade histograms cannot preserve these boundaries."""
    if not values:
        return ()
    low, high = min(values), max(values)
    if low == high:
        return (ProfitBin(low, high, len(values)),)
    with localcontext(fills.EXACT):
        width = (high - low) / 10
        counts = [0] * 10
        for value in values:
            counts[min(9, int((value - low) // width))] += 1
        return tuple(ProfitBin(low + index * width, low + (index + 1) * width, count)
                     for index, count in enumerate(counts))


def dashboard_statistics(
    summary: RunSummary, *, first_session_open_ms: int | None = None, last_session_open_ms: int | None = None,
    recorded_calendar: tuple[str, str] | None = None,
) -> Dashboard:
    """Project the recorded path; share the precise Sharpe kernel with Options Golden Search."""
    with localcontext(fills.EXACT):
        return _dashboard(summary, first_session_open_ms, last_session_open_ms, recorded_calendar)


def _dashboard(summary: RunSummary, first_ms: int | None, last_ms: int | None,
               recorded_calendar: tuple[str, str] | None) -> Dashboard:
    rows = summary.by_day
    instants = [row.session_open_ms for row in rows]
    reason = None
    annualization = None
    calendar_reason = ("The saved run used a different trading calendar; annualized risk statistics are unavailable."
                       if recorded_calendar is not None and recorded_calendar != (CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION)
                       else None)
    if not rows:
        reason = "No daily observations were recorded."
    else:
        first_ms = instants[0] if first_ms is None else first_ms
        last_ms = instants[-1] if last_ms is None else last_ms
        expected = None if calendar_reason else [window.open_ms_utc for window in session_windows_ms_utc(et_date_at_ms(first_ms), et_date_at_ms(last_ms))]
        annualization = None if calendar_reason else calendar_annualization([first_ms, last_ms])
        if len(rows) != summary.sessions_tested or (expected is not None and instants != expected):
            reason = "Daily observations do not cover every scheduled session in the run."
        elif any(row.status is SessionStatus.INCOMPLETE for row in rows):
            reason = "A session ended with a marked, unclosed position."
        elif any(row.skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE for row in rows):
            reason = "A session lacks the underlying trading evidence."
    if summary.starting_cash_usd <= 0:
        reason = "Positive starting cash is required for fixed-capital returns."
    annualized_reason = reason or calendar_reason
    returns = [] if annualized_reason else [float(decimal_ratio(row.net_profit_usd, summary.starting_cash_usd)) for row in rows]
    sharpe, sharpe_reason = (None, None) if annualized_reason else annualized_sharpe(returns, annualization)
    sortino = volatility = None
    if returns and all(math.isfinite(value) for value in returns) and sharpe_reason != "numeric_range":
        sortino = _sortino(returns, annualization)
        deviation = _sample_standard_deviation(returns)
        volatility = None if deviation is None else deviation * math.sqrt(annualization) * 100
        sortino = sortino if sortino is None or math.isfinite(sortino) else None
        volatility = volatility if volatility is None or math.isfinite(volatility) else None
    path = equity_path(rows, summary.starting_cash_usd)
    high = summary.starting_cash_usd
    underwater = longest_underwater = 0
    daily = []
    for index, (row, point) in enumerate(zip(rows, path[1:], strict=True)):
        high = max(high, point.equity_usd)
        underwater = underwater + 1 if point.equity_usd < high else 0
        longest_underwater = max(longest_underwater, underwater)
        day = et_date_at_ms(row.session_open_ms)
        rolling = None if annualized_reason or index < 19 else annualized_sharpe(returns[index-19:index+1], annualization)[0]
        daily.append(DashboardDay(row.session_open_ms, day.year, day.month, point.equity_usd - high, rolling))
    fall = summary.drawdown_by_dollars
    recovery = recovered_at = None
    if fall is not None and reason is None:
        trough = next((i for i, point in enumerate(path) if point.session_open_ms == fall.trough_session_open_ms), None)
        if trough is not None:
            for i in range(trough + 1, len(path)):
                if path[i].equity_usd >= fall.peak_equity_usd:
                    recovery, recovered_at = i - trough, path[i].session_open_ms
                    break
    profits = [row.net_profit_usd for row in rows if row.trades]
    economics = session_economics(profits)
    wins, losses = economics.wins, economics.losses
    gross_win, gross_loss = economics.gross_win, economics.gross_loss
    average_win, average_loss = economics.average_win, economics.average_loss
    net = sum(profits, Decimal(0))
    all_profits = [row.net_profit_usd for row in rows]
    return Dashboard(
        annualization_sessions=annualization, daily_observations=len(rows), risk_unavailable_reason=reason,
        annualization_unavailable_reason=calendar_reason,
        sharpe_unavailable_reason=sharpe_reason, annualized_sharpe=sharpe, annualized_sortino=sortino,
        annualized_volatility_percent=volatility,
        return_percent=None if summary.starting_cash_usd <= 0 else str(Ratio(summary.net_profit_usd, summary.starting_cash_usd).percent_1_place()),
        average_winning_session_usd=_money_mean(gross_win, len(wins)),
        average_losing_session_usd=_money_mean(-gross_loss, len(losses)), average_session_usd=_money_mean(net, len(profits)),
        session_profit_factor=_display_ratio(gross_win, gross_loss),
        payoff_ratio=None if average_win is None or average_loss is None else _display_ratio(average_win, -average_loss),
        best_session_usd=max(profits, default=None), worst_session_usd=min(profits, default=None),
        profit_without_best_session_usd=None if not all_profits else sum(all_profits, Decimal(0)) - max(all_profits),
        top_five_profit_percent=None if net <= 0 else str(Ratio(sum(sorted(wins, reverse=True)[:5], Decimal(0)), net).percent_1_place()),
        longest_losing_streak=economics.longest_losing_streak, recovery_sessions=recovery, recovered_session_open_ms=recovered_at,
        longest_underwater_sessions=longest_underwater, ongoing_underwater=bool(underwater),
        daily=tuple(daily), histogram=profit_histogram(all_profits),
    )
