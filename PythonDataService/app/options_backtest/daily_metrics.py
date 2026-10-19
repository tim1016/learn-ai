"""Shared fixed-capital daily statistics for Options reports and search (ADR 0080).

Formula: Sharpe = sqrt(A) * mean(r-rf) / sample_stdev(r-rf); A is the mean scheduled
  session count of the complete calendar years touched by the window. Decimal ratios
  use 50 digits; Sortino uses downside RMS over all observations and a zero target.
Reference: Sharpe (1994), The Sharpe Ratio; ADR 0080's fixed-capital convention;
  app/engine/results/statistics.py for Sortino and sample volatility.
Canonical implementation: this file for Options daily Sharpe, annualization and ratios;
  Sortino and volatility delegate to app/engine/results/statistics.py.
Validated against: tests/options_backtest/test_dashboard.py and
  tests/research/options_golden_search/test_metrics.py (hand-worked, atol=1e-12, rtol=0).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from functools import lru_cache
from statistics import fmean, stdev

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.options_backtest import fills
from app.utils.session_anchors import et_date_at_ms

_RATIO = Context(prec=50)


def decimal_ratio(numerator: Decimal, denominator: Decimal | int) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(_RATIO):
        return numerator / denominator


@lru_cache(maxsize=32)
def _year_sessions(year: int) -> int:
    return len(session_windows_ms_utc(date(year, 1, 1), date(year, 12, 31)))


def calendar_annualization(session_ms: Sequence[int]) -> float:
    if not session_ms:
        raise ValueError("annualization needs at least one scheduled session")
    first, last = et_date_at_ms(min(session_ms)).year, et_date_at_ms(max(session_ms)).year
    return fmean(_year_sessions(year) for year in range(first, last + 1))


def annualized_sharpe(returns: Sequence[float], annualization: float) -> tuple[float | None, str | None]:
    if len(returns) < 2:
        return None, "insufficient_daily_observations"
    if not all(math.isfinite(value) for value in returns):
        return None, "numeric_range"
    deviation = stdev(returns)
    if deviation == 0:
        return None, "constant_daily_returns"
    value = math.sqrt(annualization) * fmean(returns) / deviation
    return (value, None) if math.isfinite(value) else (None, "numeric_range")


@dataclass(frozen=True)
class SessionEconomics:
    wins: tuple[Decimal, ...]
    losses: tuple[Decimal, ...]
    gross_win: Decimal
    gross_loss: Decimal
    average_win: Decimal | None
    average_loss: Decimal | None
    longest_winning_streak: int
    longest_losing_streak: int


def session_economics(profits: Sequence[Decimal]) -> SessionEconomics:
    """Shared Options session unit, including break-even streak resets (ADR 0080).

    Formula: means = sum(positive/negative session dollars)/their counts; a
      streak advances only on the corresponding sign in traded-session order.
    Reference: ADR 0080 and #3332's completed traded-session definition.
    Canonical implementation: this file.
    Validated against: tests/options_backtest/test_dashboard.py and
      tests/research/options_golden_search/test_metrics.py.
    """
    with localcontext(fills.EXACT):
        wins, losses = tuple(p for p in profits if p > 0), tuple(p for p in profits if p < 0)
        gross_win, gross_loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
        win = loss = longest_win = longest_loss = 0
        for profit in profits:
            win, loss = win + 1 if profit > 0 else 0, loss + 1 if profit < 0 else 0
            longest_win, longest_loss = max(longest_win, win), max(longest_loss, loss)
        return SessionEconomics(wins, losses, gross_win, gross_loss, decimal_ratio(gross_win, len(wins)),
                                decimal_ratio(-gross_loss, len(losses)), longest_win, longest_loss)
