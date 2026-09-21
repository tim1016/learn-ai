"""Seam tests for GET /api/tickers/catalog — the shared symbol picker's catalog.

The Polygon walk is stubbed at the service seam; these assert the route's
transport contract — projection shape, single-flight caching, failure
re-arm — not any vendor behavior. The vendor-call parameters themselves are
pinned in tests/services/test_polygon_client_catalog.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.ticker_catalog_service import (
    TickerCatalogService,
    get_ticker_catalog_service,
)


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


@pytest.fixture(autouse=True)
def _clear_catalog_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_ticker_catalog_service, None)


def _install(stub: _StubCatalogClient) -> None:
    service = TickerCatalogService(client=stub)
    app.dependency_overrides[get_ticker_catalog_service] = lambda: service


async def test_catalog_serves_full_membership_projection() -> None:
    stub = _StubCatalogClient()
    _install(stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tickers/catalog")

    assert response.status_code == 200
    body = response.json()
    # Inactive rows ship too: offering delisted symbols is the client's
    # choice (backfill panel), not the endpoint's filter to make.
    assert [row["symbol"] for row in body] == ["MSFT", "OLD"]
    assert set(body[0]) == {"symbol", "name", "asset_class", "exchange", "status"}
    assert stub.calls == 1


async def test_catalog_single_flights_concurrent_walks() -> None:
    stub = _StubCatalogClient(delay_s=0.05)
    _install(stub)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/api/tickers/catalog") for _ in range(3)))

    assert all(response.status_code == 200 for response in responses)
    assert stub.calls == 1


async def test_catalog_failure_is_not_cached() -> None:
    stub = _StubCatalogClient(error=RuntimeError("polygon down"))
    _install(stub)

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


async def test_catalog_cancelled_waiter_does_not_cancel_shared_walk() -> None:
    started = threading.Event()
    release = threading.Event()

    class _BlockingCatalogClient(_StubCatalogClient):
        def list_catalog_tickers(self) -> list[dict[str, Any]]:
            self.calls += 1
            started.set()
            if not release.wait(timeout=1.0):
                raise TimeoutError("test did not release the catalog walk")
            return self.entries

    stub = _BlockingCatalogClient()
    service = TickerCatalogService(stub)
    first = asyncio.create_task(service.get())
    assert await asyncio.to_thread(started.wait, 1.0)
    second = asyncio.create_task(service.get())

    first.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()

    second_result = await second
    third_result = await service.get()

    assert [entry.symbol for entry in second_result] == ["MSFT", "OLD"]
    assert third_result == second_result
    assert stub.calls == 1
