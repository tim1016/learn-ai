"""`/api/chart/data` router contract — flag-on and flag-off must agree.

The split-read (#1837) puts a symbol on the path to a filesystem join when
``DATA_LAKE_ENABLED`` is on. A ticker the lake cannot address must be rejected
before that join *and* must not change the answer the caller gets: the same
status and the same error body the flag-off provider path yields, never a 500.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.config import settings
from app.routers import chart as chart_router
from app.services import chart_service

# Rejected by the lake's ticker alphabet (path separators), accepted by
# ChartDataRequest (16 chars, under its 20-char cap) — so it reaches the
# service exactly as a caller could send it.
PATH_UNSAFE_TICKER = "../../etc/passwd"

_REQUEST: dict[str, Any] = {
    "ticker": PATH_UNSAFE_TICKER,
    "from_date": "2025-11-26",
    "to_date": "2025-12-01",
    "timeframe": "15m",
    "session": "rth",
    "adjusted": False,
    "indicators": [],
}


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(chart_router.router, prefix="/api/chart")
    return app


@pytest.fixture(autouse=True)
def _no_bars_from_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polygon returns nothing for a ticker it does not recognise."""
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()
    monkeypatch.setattr(
        chart_service,
        "fetch_bars_chunked",
        lambda *_args, **_kwargs: [],
    )


async def _post_chart(api: FastAPI) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        return await client.post("/api/chart/data", json=_REQUEST)


@pytest.mark.asyncio
async def test_chart_data_path_unsafe_ticker_answers_the_same_with_the_lake_on_or_off(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", False)
    flag_off = await _post_chart(api)

    monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", True)
    flag_on = await _post_chart(api)

    assert flag_on.status_code == flag_off.status_code
    assert flag_on.json() == flag_off.json()


@pytest.mark.asyncio
async def test_chart_data_path_unsafe_ticker_is_typed_no_data_not_a_server_error(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symbol rejection routes through the router's existing typed
    no-data mapping — it must never surface as an unhandled 500."""
    for lake_enabled in (False, True):
        monkeypatch.setattr(settings, "DATA_LAKE_ENABLED", lake_enabled)

        response = await _post_chart(api)

        assert response.status_code == 404, f"DATA_LAKE_ENABLED={lake_enabled}"
        assert response.status_code != 500
        assert response.json()["detail"]["error_code"] == "NO_DATA"
