"""A run's daily equity, and the two series every equity view draws from it.

Formula:
  * daily equity — the engine's per-bar ``equity_curve`` (``timestamp`` int ms,
    strictly increasing) reduced to the LAST point of each America/New_York
    date, anchored at that date's scheduled NYSE session close (canonical
    calendar, so half-days close early).
  * cumulative return — ``r_t = E_t / C − 1`` for starting capital ``C``.
  * drawdown — ``d_t = E_t / max_{s<=t} E_s − 1`` (a fraction, ``<= 0``) over
    the daily points; undefined while the peak is not above 0.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Compare
  candidates and weaknesses"; every run keeps the daily equity since
  https://github.com/tim1016/learn-ai/issues/2930.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_daily_equity.py (hand-computed
  values, ``atol=1e-9, rtol=0``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.utils.session_anchors import et_date_at_ms


def daily_equity(equity_curve: Sequence[Mapping[str, Any]]) -> list[tuple[int, float]]:
    """``(session close ms, equity)`` for the last equity point of each ET date; refuses unordered timestamps."""
    last_by_day: dict[date, float] = {}
    previous: int | None = None
    for row in equity_curve:
        timestamp = row["timestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError(f"equity timestamp {timestamp!r} is not an int ms UTC")
        if previous is not None and timestamp <= previous:
            raise ValueError(f"equity timestamps must strictly increase; {timestamp} follows {previous}")
        previous = timestamp
        last_by_day[et_date_at_ms(timestamp)] = float(row["equity"])
    daily: list[tuple[int, float]] = []
    for day, equity in last_by_day.items():
        try:
            close_ms = session_close_ms_utc(day)
        except LookupError as exc:
            raise ValueError(f"an equity point falls on {day.isoformat()}, which is not a NYSE session") from exc
        daily.append((close_ms, equity))
    return daily


def cumulative_return_series(daily: Sequence[tuple[int, float]], capital: float) -> list[tuple[int, float]]:
    """``(ms, equity / capital − 1)``: the run's gain on its starting capital at each session close."""
    return [(ms, equity / capital - 1.0) for ms, equity in daily]


def drawdown_series(daily: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """``(ms, equity / running peak − 1)``; refuses a non-positive peak, where drawdown is undefined."""
    series: list[tuple[int, float]] = []
    peak = -math.inf
    for ms, equity in daily:
        peak = max(peak, equity)
        if peak <= 0:
            raise ValueError(f"drawdown is undefined while peak equity is {peak}")
        series.append((ms, equity / peak - 1.0))
    return series
