from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.data_lake.polygon_ticker_events import TickerEvent, fetch_ticker_events

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_fetch_no_events_returns_empty():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )
    events = await fetch_ticker_events(symbol="SPY", api_key="t")
    assert events == []


@respx.mock
async def test_fetch_returns_normalized_events():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/META/events.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": {
                    "events": [
                        {"type": "ticker_change", "date": "2022-06-09", "ticker_change": {"ticker": "META"}},
                    ]
                },
            },
        )
    )
    events = await fetch_ticker_events(symbol="META", api_key="t")
    assert events == [TickerEvent(date="2022-06-09", new_ticker="META")]
