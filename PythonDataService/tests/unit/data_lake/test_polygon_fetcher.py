"""Unit tests for app.data_lake.polygon_fetcher.

Polygon /v2/aggs response shape (per docs):
  {
    "ticker": "SPY",
    "results": [{"v":..., "o":..., "c":..., "h":..., "l":..., "t":..., "vw":..., "n":...}, ...],
    "next_url": "..."  (optional; present when more pages exist)
  }
"""

from __future__ import annotations

import re
from datetime import date

import httpx
import pytest
import respx

from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)


def _aggs_url_pattern() -> re.Pattern:
    # /v2/aggs/ticker/{sym}/range/1/minute/{start}/{end}
    return re.compile(r"https://api\.polygon\.io/v2/aggs/ticker/.+/range/1/minute/.+")


@pytest.mark.asyncio
@respx.mock
async def test_single_page_response():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {
                        "v": 100,
                        "vw": 500.0,
                        "o": 499.5,
                        "c": 500.5,
                        "h": 501.0,
                        "l": 499.0,
                        "t": 1716206400000,
                        "n": 10,
                    },
                    {
                        "v": 200,
                        "vw": 500.6,
                        "o": 500.5,
                        "c": 500.7,
                        "h": 500.8,
                        "l": 500.4,
                        "t": 1716206460000,
                        "n": 15,
                    },
                ],
            },
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert len(bars) == 2
    assert bars[0].t_ms == 1716206400000
    assert bars[1].volume == 200


@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_next_url():
    first = respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {"v": 1, "vw": 1.0, "o": 1.0, "c": 1.0, "h": 1.0, "l": 1.0, "t": 1, "n": 1},
                ],
                "next_url": "https://api.polygon.io/v2/aggs/page2",
            },
        )
    )
    page2 = respx.get("https://api.polygon.io/v2/aggs/page2").mock(
        return_value=httpx.Response(
            200,
            json={
                "ticker": "SPY",
                "status": "OK",
                "results": [
                    {"v": 2, "vw": 2.0, "o": 2.0, "c": 2.0, "h": 2.0, "l": 2.0, "t": 2, "n": 2},
                ],
            },
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert len(bars) == 2
    assert first.called
    assert page2.called


@pytest.mark.asyncio
@respx.mock
async def test_empty_results_returns_empty_list():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={"ticker": "SPY", "status": "OK", "results": []},
        )
    )
    bars = await fetch_minute_trade_aggregates(
        symbol="SPY",
        start=date(2024, 5, 20),
        end=date(2024, 5, 20),
        api_key="test-key",
    )
    assert bars == []


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_PolygonAuthError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(401, json={"error": "Unauthorized"}))
    with pytest.raises(PolygonAuthError):
        await fetch_minute_trade_aggregates(
            symbol="SPY",
            start=date(2024, 5, 20),
            end=date(2024, 5, 20),
            api_key="bad",
        )


@pytest.mark.asyncio
@respx.mock
async def test_403_raises_PolygonEntitlementError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(403, json={"error": "Forbidden"}))
    with pytest.raises(PolygonEntitlementError):
        await fetch_minute_trade_aggregates(
            symbol="SPY",
            start=date(2024, 5, 20),
            end=date(2024, 5, 20),
            api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_429_raises_PolygonRateLimitedError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(429, json={"error": "Too many"}))
    with pytest.raises(PolygonRateLimitedError):
        await fetch_minute_trade_aggregates(
            symbol="SPY",
            start=date(2024, 5, 20),
            end=date(2024, 5, 20),
            api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_404_or_unknown_status_raises_PolygonUnknownSymbolError():
    respx.get(_aggs_url_pattern()).mock(
        return_value=httpx.Response(
            200,
            json={"ticker": "FAKE", "status": "NOT_FOUND", "results": []},
        )
    )
    with pytest.raises(PolygonUnknownSymbolError):
        await fetch_minute_trade_aggregates(
            symbol="FAKE",
            start=date(2024, 5, 20),
            end=date(2024, 5, 20),
            api_key="ok",
        )


@pytest.mark.asyncio
@respx.mock
async def test_500_raises_PolygonFetchError():
    respx.get(_aggs_url_pattern()).mock(return_value=httpx.Response(500, json={"error": "Server"}))
    with pytest.raises(PolygonFetchError):
        await fetch_minute_trade_aggregates(
            symbol="SPY",
            start=date(2024, 5, 20),
            end=date(2024, 5, 20),
            api_key="ok",
        )
