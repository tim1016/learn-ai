"""respx-mocked tests for Polygon corp-action endpoints."""

from __future__ import annotations

import re

import httpx
import pytest
import respx

from app.data_lake.polygon_corp_actions import (
    DividendEvent,
    SplitEvent,
    fetch_dividends,
    fetch_splits,
)

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_fetch_splits_single_page():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {"ticker": "SPY", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
                ],
            },
        )
    )
    events = await fetch_splits(symbol="SPY", api_key="test-key")
    assert len(events) == 1
    assert events[0] == SplitEvent(
        execution_date="2020-08-31",
        split_from=1.0,
        split_to=4.0,
    )


@respx.mock
async def test_fetch_splits_pagination():
    page_a = respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits\?.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {"ticker": "SPY", "execution_date": "2020-08-31", "split_from": 1, "split_to": 4},
                ],
                "next_url": "https://api.polygon.io/v3/reference/splits/page2",
            },
        )
    )
    page_b = respx.get("https://api.polygon.io/v3/reference/splits/page2").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {"ticker": "SPY", "execution_date": "2000-01-03", "split_from": 1, "split_to": 2},
                ],
            },
        )
    )
    events = await fetch_splits(symbol="SPY", api_key="test-key")
    assert len(events) == 2
    # Results are sorted ascending by execution_date.
    assert events[0].execution_date == "2000-01-03"
    assert events[1].execution_date == "2020-08-31"
    assert page_a.called and page_b.called


@respx.mock
async def test_fetch_dividends_single_page():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "results": [
                    {"ticker": "SPY", "ex_dividend_date": "2024-03-15", "cash_amount": 1.71, "currency": "USD"},
                ],
            },
        )
    )
    events = await fetch_dividends(symbol="SPY", api_key="test-key")
    assert len(events) == 1
    assert events[0] == DividendEvent(
        ex_dividend_date="2024-03-15",
        cash_amount=1.71,
    )


@respx.mock
async def test_fetch_splits_empty_results_returns_empty():
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    events = await fetch_splits(symbol="UNKNOWN", api_key="test-key")
    assert events == []
