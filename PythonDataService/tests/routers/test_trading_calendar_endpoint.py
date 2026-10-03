"""GET /api/calendar/sessions (#2858)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.trading_calendar import router
from app.schemas.trading_calendar import TradingCalendarWindow
from app.services.trading_calendar_service import get_calendar_window

pytestmark = pytest.mark.asyncio


def _utc_ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


async def _get(params: dict[str, int]) -> tuple[int, dict]:
    app = FastAPI()
    app.include_router(router, prefix="/api/calendar")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/calendar/sessions", params=params)
    return response.status_code, response.json()


async def test_window_is_the_et_dates_containing_the_two_instants() -> None:
    # 02:00Z on the 29th is still the evening of Thanksgiving in New York, so the holiday is inside the window.
    status_code, body = await _get({"start_ms": _utc_ms(2024, 11, 29, 2), "end_ms": _utc_ms(2024, 11, 29, 20)})

    assert status_code == 200
    assert body["session_open_ms"] == [_utc_ms(2024, 11, 29, 14, 30)]
    assert body["session_close_ms"] == [_utc_ms(2024, 11, 29, 18)]
    assert [holiday["date_ms"] for holiday in body["holidays"]] == [_utc_ms(2024, 11, 28, 14, 30)]


async def test_a_window_the_calendar_cannot_serve_is_a_422_with_the_reason() -> None:
    status_code, body = await _get({"start_ms": _utc_ms(2024, 1, 3, 15), "end_ms": _utc_ms(2024, 1, 2, 15)})

    assert status_code == 422
    assert body["detail"] == "The window ends before it starts."


async def test_the_route_asks_its_calendar_dependency_for_the_two_et_dates() -> None:
    asked: list[tuple[date, date]] = []

    def window_for(start: date, end: date) -> TradingCalendarWindow:
        asked.append((start, end))
        return TradingCalendarWindow(session_open_ms=[], session_close_ms=[], holidays=[])

    app = FastAPI()
    app.include_router(router, prefix="/api/calendar")
    app.dependency_overrides[get_calendar_window] = lambda: window_for
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 03:00Z on January 3rd is still January 2nd in New York.
        response = await client.get("/api/calendar/sessions", params={"start_ms": _utc_ms(2024, 1, 3, 3), "end_ms": _utc_ms(2024, 1, 31, 15)})

    assert response.status_code == 200
    assert asked == [(date(2024, 1, 2), date(2024, 1, 31))]
