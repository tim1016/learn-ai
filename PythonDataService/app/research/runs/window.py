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
*exclusive* — a window ``[2026-05-19, 2026-05-26)`` includes
2026-05-25 (Memorial Day) as an excluded session and stops before
2026-05-26.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd
import pandas_market_calendars as mcal
from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.trading_calendar import (
    blocked_dates_in_range,
    is_trading_day,
)

# Reuse the NYSE calendar singleton for holiday-name lookups. The
# ``regular_holidays`` attribute returns a ``pandas.tseries.holiday``
# calendar whose ``.holidays(start, end, return_name=True)`` produces
# a Series indexed by date with the holiday name as the value.
_NYSE = mcal.get_calendar("NYSE")


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

    ``requested_end_date`` is exclusive — sessions are evaluated over
    the half-open interval ``[requested_start_date, requested_end_date)``,
    matching the runner's ``set_end_date`` semantics.
    """

    model_config = ConfigDict(extra="forbid")

    requested_start_date: Date
    requested_end_date: Date
    sessions_included: list[Date] = Field(default_factory=list)
    sessions_excluded: list[ExcludedDay] = Field(default_factory=list)


def _holiday_names(start: Date, end_inclusive: Date) -> dict[Date, str]:
    """Return ``{date: holiday_name}`` for NYSE holidays in
    ``[start, end_inclusive]``.

    Calls into the underlying ``pandas.tseries.holiday`` calendar that
    backs ``pandas_market_calendars``. Returns an empty dict if no
    holidays fall in the range.
    """
    series = _NYSE.regular_holidays.holidays(
        pd.Timestamp(start),
        pd.Timestamp(end_inclusive),
        return_name=True,
    )
    return {ts.date(): str(name) for ts, name in series.items()}


def summarize_window(start: Date, end: Date) -> WindowSummary:
    """Build a :class:`WindowSummary` for ``[start, end)``.

    ``end`` is exclusive (runner convention). Raises ``ValueError``
    when ``end <= start`` — caller is expected to translate to a 400.
    """
    if end <= start:
        raise ValueError(
            f"end must be strictly after start (got start={start.isoformat()}, end={end.isoformat()})"
        )

    inclusive_end = end - timedelta(days=1)
    blocked = blocked_dates_in_range(start, inclusive_end)
    holiday_names = _holiday_names(start, inclusive_end)

    included: list[Date] = []
    excluded: list[ExcludedDay] = []
    current = start
    one_day = timedelta(days=1)
    while current < end:
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


def summarize_window_from_ms(start_ms: int, end_ms: int) -> WindowSummary:
    """Build a :class:`WindowSummary` from canonical ``int64 ms UTC``
    timestamps, interpreted as ``America/New_York`` local midnights.

    The runner stores ``start_ms`` / ``end_ms`` in the ledger as
    NY-midnight epoch ms (see ``runner._date_to_ny_midnight_ms``).
    Round-tripping back to a ``date`` for calendar lookups happens
    here so callers in the runner don't replicate the conversion.
    """
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    start = datetime.fromtimestamp(start_ms / 1000, tz=ny).date()
    end = datetime.fromtimestamp(end_ms / 1000, tz=ny).date()
    return summarize_window(start, end)
