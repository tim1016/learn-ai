"""`/api/chart/data` router contract — a bad symbol is typed, never a 500.

The split-read (#1837) puts a symbol on the path to a filesystem join. A
ticker the lake cannot address must be rejected before that join, and the
caller must get the router's typed error body rather than an unhandled 500.

These assertions used to be made twice, once per ``DATA_LAKE_ENABLED``
setting, to prove the two paths agreed. #1893 left one path, so each is
made once — the typed answer is the contract, and it no longer has a
second implementation to be compared against.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import urllib3.exceptions
from fastapi import FastAPI
from httpx import ASGITransport

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
async def test_chart_data_path_unsafe_ticker_is_typed_no_data_not_a_server_error(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The symbol rejection routes through the router's existing typed
    no-data mapping — it must never surface as an unhandled 500."""
    response = await _post_chart(api)

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NO_DATA"


def _unreachable_provider(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
    """Stands in for Polygon's SDK when the host is unreachable.

    ``PolygonClientService`` re-raises whatever its underlying
    ``urllib3.PoolManager`` raises without translation — confirmed by hand
    against the real client with Polygon blocked at the container's
    ``/etc/hosts`` (#1867 offline-lake validation): a connection refusal
    surfaces as exactly this exception, not Python's builtin
    ``ConnectionError``.
    """
    raise urllib3.exceptions.MaxRetryError(
        pool=None,
        url="https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/minute/2025-11-26/2025-12-01",
        reason=Exception("Connection refused"),
    )


@pytest.mark.asyncio
async def test_chart_data_provider_unreachable_is_typed_not_a_server_error(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lake gap that falls back to Polygon, with Polygon unreachable, must
    answer a typed 503 — never leak urllib3's raw exception repr through an
    unhandled 500 the way it did before this fix."""
    monkeypatch.setattr(chart_service, "fetch_bars_chunked", _unreachable_provider)
    request = {**_REQUEST, "ticker": "AAPL"}

    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post("/api/chart/data", json=request)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error_code"] == "PROVIDER_UNREACHABLE"
    assert "MaxRetryError" not in detail["detail"]
