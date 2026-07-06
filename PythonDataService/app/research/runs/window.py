"""``WindowSummary`` — calendar breakdown for a backtest window.

Surfaces which calendar dates inside ``[start, end)`` are tradeable
sessions vs blocked dates (weekends, US-equity holidays). Two
consumers:

  * ``GET /api/research/trading-calendar`` — date-picker preview so
    the UI can warn before a run is submitted (e.g., Memorial Day
    silently truncating a "last 7 days" backtest).
  * Stamped onto every persisted :class:`RunLedger` so a completed
    run is self-describing about *which* days the engine actually
    saw.

The calendar source is :mod:`app.lean_sidecar.trading_calendar`
(NYSE via ``pandas_market_calendars``). It lives under
``lean_sidecar/`` for historical reasons; moving it is out of scope
here.

Date semantics mirror the runner's window contract: ``end_date`` is
*inclusive* — a window ``[2026-05-19, 2026-05-26]`` includes the
named end day. ``StrategyAlgorithm.set_end_date(y, m, d)`` sets
``end_date = datetime(y, m, d, 23, 59, 59, tz=NY)`` (engine/strategy/
base.py:212) — i.e. the bar emitter loops while ``current <= end``,
so 5/26 is the last day the engine sees. The canonical Memorial-Day
example (2026-05-19 → 2026-05-26 = 5 trading days: 5/19, 5/20, 5/21,
5/22, 5/26) only holds when 5/26 is included.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.trading_calendar import (
    blocked_dates_in_range,
    holiday_names_in_range,
    is_trading_day,
)


class ExcludedDay(BaseModel):
    """One calendar date that fell inside a requested window but is
    not a NYSE trading session.

    ``name`` is populated for holidays (e.g. ``"Memorial Day"``) and
    is ``None`` for weekends — weekends don't have proper names in the
    same sense.
    """

    model_config = ConfigDict(extra="forbid")

    date: Date
    reason: Literal["weekend", "holiday"]
    name: str | None = None


class WindowSummary(BaseModel):
    """Calendar breakdown of a requested backtest window.

    ``requested_end_date`` is inclusive — sessions are evaluated over
    the closed interval ``[requested_start_date, requested_end_date]``,
    matching the runner's ``set_end_date`` semantics (the named end day
    is the last bar the engine sees, not the first one it skips).
    """

    model_config = ConfigDict(extra="forbid")

    requested_start_date: Date
    requested_end_date: Date
    sessions_included: list[Date] = Field(default_factory=list)
    sessions_excluded: list[ExcludedDay] = Field(default_factory=list)


def summarize_window(start: Date, end: Date) -> WindowSummary:
    """Build a :class:`WindowSummary` for ``[start, end]`` (inclusive).

    Mirrors the runner's ``set_end_date`` semantics: the named end day
    is the last session the engine sees. ``start == end`` is a valid
    single-day window. Raises ``ValueError`` when ``end < start`` —
    caller is expected to translate to a 400.
    """
    if end < start:
        raise ValueError(
            f"end must be on or after start (got start={start.isoformat()}, end={end.isoformat()})"
        )

    blocked = blocked_dates_in_range(start, end)
    holiday_names = holiday_names_in_range(start, end)

    included: list[Date] = []
    excluded: list[ExcludedDay] = []
    current = start
    one_day = timedelta(days=1)
    while current <= end:
        reason = blocked.get(current)
        if reason is None:
            if is_trading_day(current):
                included.append(current)
            else:
                # Defensive: blocked_dates_in_range already classifies
                # weekends and holidays, so this branch is unreachable
                # in practice. Kept as a guard rather than a silent miss.
                excluded.append(
                    ExcludedDay(date=current, reason="holiday", name=holiday_names.get(current))
                )
        elif reason == "weekend":
            excluded.append(ExcludedDay(date=current, reason="weekend", name=None))
        else:  # "holiday"
            excluded.append(
                ExcludedDay(date=current, reason="holiday", name=holiday_names.get(current))
            )
        current = current + one_day

    return WindowSummary(
        requested_start_date=start,
        requested_end_date=end,
        sessions_included=included,
        sessions_excluded=excluded,
    )
