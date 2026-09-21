"""Seam tests for GET /api/tickers/catalog — the shared symbol picker's catalog.

The Polygon walk is stubbed at the service boundary; these assert the route's
transport contract — projection shape, single-flight caching, failure re-arm —
not any vendor behavior.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import tickers as tickers_router


@pytest.fixture(autouse=True)
def _clean_catalog_cache() -> Generator[None, None, None]:
    tickers_router.clear_ticker_catalog_cache_for_testing()
    yield
    tickers_router.clear_ticker_catalog_cache_for_testing()


def _stub_entries() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "MSFT",
            "name": "Microsoft Corporation",
            "asset_class": "us_equity",
            "exchange": "NASDAQ",
            "status": "active",
        },
        {
            "symbol": "OLD",
            "name": "Delisted Corp",
            "asset_class": "us_equity",
            "exchange": "NYSE",
            "status": "inactive",
        },
    ]


async def test_catalog_serves_full_membership_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_walk() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return _stub_entries()

    monkeypatch.setattr(tickers_router.polygon_client, "list_catalog_tickers", fake_walk)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tickers/catalog")

    assert response.status_code == 200
    body = response.json()
    # Inactive rows ship too: offering delisted symbols is the client's
    # choice (backfill panel), not the endpoint's filter to make.
    assert [row["symbol"] for row in body] == ["MSFT", "OLD"]
    assert set(body[0]) == {"symbol", "name", "asset_class", "exchange", "status"}
    assert calls == 1


async def test_catalog_single_flights_concurrent_walks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def slow_walk() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        # Hold the walk open so concurrent requests must interleave onto the
        # same task instead of each starting their own vendor walk.
        time.sleep(0.05)
        return _stub_entries()

    monkeypatch.setattr(tickers_router.polygon_client, "list_catalog_tickers", slow_walk)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get("/api/tickers/catalog") for _ in range(3))
        )

    assert all(response.status_code == 200 for response in responses)
    assert calls == 1


async def test_catalog_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[dict[str, Any]]:
        raise RuntimeError("polygon down")

    monkeypatch.setattr(tickers_router.polygon_client, "list_catalog_tickers", boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/api/tickers/catalog")
        assert first.status_code == 503

        monkeypatch.setattr(
            tickers_router.polygon_client,
            "list_catalog_tickers",
            lambda: _stub_entries(),
        )
        second = await client.get("/api/tickers/catalog")

    # A failed walk is not sticky: the picker's Retry must reach the vendor.
    assert second.status_code == 200
    assert second.json()[0]["symbol"] == "MSFT"
