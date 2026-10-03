"""Wire shapes for the trading-calendar window the shared date picker reads."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS

TimestampMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class CalendarHoliday(BaseModel):
    """A weekday the exchange is closed, anchored at 09:30 ET on that date."""

    date_ms: TimestampMs
    name: str


class TradingCalendarWindow(BaseModel):
    """The scheduled NYSE sessions in a window, oldest first.

    Parallel arrays: session ``i`` opens at ``session_open_ms[i]`` and closes
    at ``session_close_ms[i]``. The close is the scheduled one, so a half-day
    carries its early close.
    """

    session_open_ms: list[TimestampMs]
    session_close_ms: list[TimestampMs]
    holidays: list[CalendarHoliday]
