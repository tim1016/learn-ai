"""Full capture validates every answer and resumes durable successful outcomes."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_capture import capture_option_session
from app.options.contract import OptionContract
from tests._helpers.option_vendor import no_trades, traded
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    NOW_2026_10_12_MS,
    stock_bar,
    store_listing,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
PUT = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)


def url(contract):
    return f"https://api.polygon.io/v2/aggs/ticker/O:{contract.occ_symbol}/range/1/minute/{FRIDAY}/{FRIDAY}"


def bar(**changes):
    return {"t": FRIDAY_OPEN_MS, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, **changes}


async def prepare(fake_catalog, tmp_lake, *contracts):
    await store_listing([vendor_listing_row(c) for c in contracts])
    await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "500", "500", "500", "500")])


@respx.mock
async def test_complete_capture_and_pause_resume_never_refetches_staged_ticker(fake_catalog, tmp_lake: Path):
    await prepare(fake_catalog, tmp_lake, CALL, PUT)
    paused = False
    def first(request):
        nonlocal paused
        paused = True
        return traded(CALL.occ_symbol, [bar()])
    call = respx.get(url(CALL)).mock(side_effect=first)
    put = respx.get(url(PUT)).mock(return_value=no_trades(PUT.occ_symbol))
    result = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS, should_pause=lambda: paused)
    assert result.failure.reason == "lease_timeout"
    assert call.call_count == 1 and put.call_count == 0
    assert await asyncio.to_thread(read_option_session, "SPY", FRIDAY) is None
    completed = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert completed.failure is None
    assert call.call_count == 1 and put.call_count == 1
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.complete and session.captured == {CALL.occ_symbol} and session.confirmed_empty == {PUT.occ_symbol}
    again = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert again.reused and call.call_count == 1 and put.call_count == 1


@pytest.mark.parametrize("damage", ["ticker", "count", "page", "duplicate", "off_minute", "cross_date", "price", "auth", "rate", "timeout"])
@respx.mock
async def test_bad_vendor_answer_never_claims_complete_coverage(fake_catalog, tmp_lake, damage):
    await prepare(fake_catalog, tmp_lake, CALL)
    body = {"status": "OK", "ticker": f"O:{CALL.occ_symbol}", "resultsCount": 1, "results": [bar()]}
    status = 200
    if damage == "ticker":
        body["ticker"] = f"O:{PUT.occ_symbol}"
    elif damage == "count":
        body["resultsCount"] = 2
    elif damage == "page":
        body["next_url"] = "https://api.polygon.io/page2"
    elif damage == "duplicate":
        body["results"].append(bar())
        body["resultsCount"] = 2
    elif damage == "off_minute":
        body["results"] = [bar(t=FRIDAY_OPEN_MS + 1)]
    elif damage == "cross_date":
        body["results"] = [bar(t=FRIDAY_OPEN_MS + 86_400_000)]
    elif damage == "price":
        body["results"] = [bar(o=0)]
    elif damage in {"auth", "rate"}:
        status = 403 if damage == "auth" else 429
        body = {"status": "ERROR", "message": "entitlement denied"}
    route = respx.get(url(CALL))
    if damage == "timeout":
        route.mock(side_effect=httpx.ReadTimeout("unavailable"))
    else:
        route.mock(return_value=httpx.Response(status, json=body))
    outcome = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert outcome.failure is not None
    assert await asyncio.to_thread(read_option_session, "SPY", FRIDAY) is None
    assert not any(row["symbol"] == "SPY" and row["market"] == "usa_options" and row["artifact_kind"] == "time_series_bars"
                   for row in fake_catalog.rows.values())


@respx.mock
async def test_explicit_transient_retry_budget_and_planned_pause(fake_catalog, tmp_lake):
    await prepare(fake_catalog, tmp_lake, CALL)
    route = respx.get(url(CALL)).mock(return_value=httpx.Response(429))
    for _ in range(5):
        paused = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS, should_pause=lambda: True)
        assert paused.failure.reason == "lease_timeout"
    assert route.call_count == 0
    for _ in range(3):
        await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    refused = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert refused.failure.reason == "fetch_timeout" and route.call_count == 3
    route.mock(return_value=no_trades(CALL.occ_symbol))
    recovered = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS, retry_exhausted=True)
    assert recovered.failure is None and route.call_count == 4
