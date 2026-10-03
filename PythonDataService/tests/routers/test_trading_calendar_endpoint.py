"""GET /api/calendar/sessions (#2858)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.trading_calendar import router

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
