"""GET /api/calendar/sessions — the trading calendar a browser cannot derive (#2858)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.trading_calendar import TradingCalendarWindow
from app.services.trading_calendar_service import calendar_window
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

router = APIRouter()

InstantMs = Annotated[int, Query(ge=0, le=MAX_TIMESTAMP_MS)]


@router.get("/sessions", response_model=TradingCalendarWindow)
async def get_sessions(start_ms: InstantMs, end_ms: InstantMs) -> TradingCalendarWindow:
    """Every scheduled NYSE session between two instants, with the holidays between them.

    The window is the ET calendar dates containing ``start_ms`` and
    ``end_ms``, both included. The shared date picker reads it once so its
    quick ranges land on real sessions and its calendar greys out the days
    the exchange is closed, without a second calendar in the browser.
    """
    try:
        return calendar_window(et_date_at_ms(start_ms), et_date_at_ms(end_ms))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
