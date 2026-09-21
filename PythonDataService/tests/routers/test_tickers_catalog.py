"""Seam tests for GET /api/tickers/catalog — the shared symbol picker's catalog.

The Polygon walk is stubbed at the service seam; these assert the route's
transport contract — projection shape, single-flight caching, failure
re-arm — not any vendor behavior. The vendor-call parameters themselves are
pinned in tests/services/test_polygon_client_catalog.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import tickers as tickers_router
from app.services.ticker_catalog_service import TickerCatalogService


class _StubCatalogClient:
    """Stands in for the Polygon client at the TickerCatalogService seam."""

    def __init__(
        self,
        entries: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.entries = entries if entries is not None else _stub_entries()
        self.error = error
        self.delay_s = delay_s
        self.calls = 0

    def list_catalog_tickers(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        # Hold the walk open when asked so concurrent requests must
        # interleave onto the same task instead of each starting their own.
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        return self.entries


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


def _install(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubCatalogClient,
) -> None:
    monkeypatch.setattr(
        tickers_router,
        "catalog_service",
        TickerCatalogService(client=stub),  # type: ignore[arg-type]
    )


async def test_catalog_serves_full_membership_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubCatalogClient()
    _install(monkeypatch, stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tickers/catalog")

    assert response.status_code == 200
    body = response.json()
    # Inactive rows ship too: offering delisted symbols is the client's
    # choice (backfill panel), not the endpoint's filter to make.
    assert [row["symbol"] for row in body] == ["MSFT", "OLD"]
    assert set(body[0]) == {"symbol", "name", "asset_class", "exchange", "status"}
    assert stub.calls == 1


async def test_catalog_single_flights_concurrent_walks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubCatalogClient(delay_s=0.05)
    _install(monkeypatch, stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(
            *(client.get("/api/tickers/catalog") for _ in range(3))
        )

    assert all(response.status_code == 200 for response in responses)
    assert stub.calls == 1


async def test_catalog_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubCatalogClient(error=RuntimeError("polygon down"))
    _install(monkeypatch, stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/api/tickers/catalog")
        assert first.status_code == 503

        # A failed walk is not sticky: the picker's Retry must reach the
        # vendor — same service, next call succeeds.
        stub.error = None
        second = await client.get("/api/tickers/catalog")

    assert second.status_code == 200
    assert second.json()[0]["symbol"] == "MSFT"
    assert stub.calls == 2
