"""Full capture validates every answer and resumes durable successful outcomes."""
from __future__ import annotations

import asyncio
import threading
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


@respx.mock
async def test_aged_out_answer_preserves_successful_progress_as_explicit_partial_coverage(fake_catalog, tmp_lake):
    from tests._helpers.option_vendor import outside_plan

    await prepare(fake_catalog, tmp_lake, CALL, PUT)
    respx.get(url(CALL)).mock(return_value=traded(CALL.occ_symbol, [bar()]))
    respx.get(url(PUT)).mock(return_value=outside_plan())
    result = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert result.failure.reason == "outside_data_plan"
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert not session.complete and session.captured == {CALL.occ_symbol}
    assert session.unresolved == {PUT.occ_symbol} and not session.confirmed_empty


@respx.mock
async def test_adjusted_listed_ticker_is_preserved_without_becoming_orderable(fake_catalog, tmp_lake):
    adjusted = "SPY1" + CALL.occ_symbol[3:]
    await store_listing([vendor_listing_row(CALL, ticker="O:" + adjusted), vendor_listing_row(PUT)])
    await store_underlying_day(fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "500", "500", "500", "500")])
    adjusted_url = f"https://api.polygon.io/v2/aggs/ticker/O:{adjusted}/range/1/minute/{FRIDAY}/{FRIDAY}"
    archive_request = respx.get(adjusted_url).mock(return_value=traded(adjusted, [bar()]))
    other_request = respx.get(url(PUT)).mock(return_value=no_trades(PUT.occ_symbol))
    result = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert result.failure is None and archive_request.call_count == other_request.call_count == 1
    session = await asyncio.to_thread(read_option_session, "SPY", FRIDAY)
    assert session.complete and adjusted in session.captured and PUT.occ_symbol in session.confirmed_empty
    with pytest.raises(ValueError):
        OptionContract.from_occ_symbol(adjusted)


@respx.mock
async def test_cancelled_collection_after_five_minutes_resumes_without_refetch(fake_catalog, tmp_lake, monkeypatch):
    from app.data_lake import option_lake

    await prepare(fake_catalog, tmp_lake, CALL, PUT)
    entered = threading.Event()
    release = threading.Event()
    elapsed_ms = 0
    clock_start_ms = fake_catalog._now_ms()
    monkeypatch.setattr(fake_catalog, "_now_ms", lambda: clock_start_ms + elapsed_ms)
    second_attempts = 0
    def first(request):
        nonlocal elapsed_ms
        elapsed_ms = 360_000  # a full collection outlives the 300-second publication lease
        return traded(CALL.occ_symbol, [bar()])
    async def second(request):
        nonlocal second_attempts
        second_attempts += 1
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.001)
        return no_trades(PUT.occ_symbol)
    call = respx.get(url(CALL)).mock(side_effect=first)
    put = respx.get(url(PUT)).mock(side_effect=second)
    monkeypatch.setattr(option_lake, "current_ms", lambda: NOW_2026_10_12_MS + elapsed_ms)
    task = asyncio.create_task(capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS))
    assert await asyncio.to_thread(entered.wait, 5)
    assert not any(row["symbol"] == "SPY" and row["artifact_kind"] == "time_series_bars"
                   and row["market"] == "usa_options" for row in fake_catalog.rows.values())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # Join the cancelled background task before retrying its collector lock.
    for _ in range(100):
        outcome = await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS + elapsed_ms)
        if outcome.failure is None:
            break
        assert outcome.failure.reason == "lease_timeout"
        await asyncio.sleep(0.001)
    assert outcome.failure is None and call.call_count == 1 and second_attempts == 2
    assert put.call_count == 1, "only the successful response is recorded; cancellation preserves prior progress"
    assert (await asyncio.to_thread(read_option_session, "SPY", FRIDAY)).complete


@pytest.mark.parametrize("escape", ["SPY/../../escape", "../escape", "root_symlink", "session_symlink", "lock_symlink"])
async def test_capture_refuses_escape_before_creating_a_collector(fake_catalog, tmp_lake: Path, tmp_path: Path, escape: str) -> None:
    from app.data_lake.path_policy import resolve_staging_root
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"")
    capture_root = resolve_staging_root("raw") / "option_sessions"
    underlying = escape if "/" in escape else "SPY"
    if escape == "root_symlink":
        capture_root.parent.mkdir(parents=True, exist_ok=True)
        capture_root.symlink_to(outside, target_is_directory=True)
    elif escape == "session_symlink":
        capture_root.mkdir(parents=True)
        (capture_root / "SPY").symlink_to(outside, target_is_directory=True)
    elif escape == "lock_symlink":
        staging = capture_root / "SPY" / str(session_open_ms_utc(FRIDAY))
        staging.mkdir(parents=True)
        (staging / ".collector.lock").symlink_to(sentinel)
    with pytest.raises(ValueError, match="escapes root"):
        await capture_option_session(underlying, FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_bytes() == b""
    assert not (resolve_staging_root("raw") / "escape").exists()


@pytest.mark.parametrize("escape", ["generation", "zip", "json", "attempts"])
@respx.mock
async def test_capture_refuses_symlinked_generation_and_members_before_provider_work(
    fake_catalog, tmp_lake: Path, tmp_path: Path, escape: str,
) -> None:
    from app.data_lake.option_listing import read_option_listing
    from app.data_lake.option_session import option_session_dch
    from app.data_lake.path_policy import resolve_staging_root
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    await prepare(fake_catalog, tmp_lake, CALL)
    listing = await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)
    staging = resolve_staging_root("raw") / "option_sessions" / "SPY" / str(session_open_ms_utc(FRIDAY))
    generation = staging / option_session_dch() / listing.sha256
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"0")
    if escape == "generation":
        generation.parent.mkdir(parents=True)
        generation.symlink_to(outside, target_is_directory=True)
    else:
        generation.mkdir(parents=True)
        (generation / f"{CALL.occ_symbol}.{escape}").symlink_to(sentinel)
    request = respx.get(url(CALL)).mock(return_value=no_trades(CALL.occ_symbol))
    with pytest.raises(ValueError, match="escapes root"):
        await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert request.call_count == 0
    assert list(outside.iterdir()) == [sentinel] and sentinel.read_bytes() == b"0"


@pytest.mark.parametrize("provider_failure", [False, True])
@respx.mock
async def test_capture_rechecks_confinement_after_a_provider_await(
    fake_catalog, tmp_lake: Path, tmp_path: Path, provider_failure: bool,
) -> None:
    from app.data_lake.option_listing import read_option_listing
    from app.data_lake.option_session import option_session_dch
    from app.data_lake.path_policy import resolve_staging_root
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    await prepare(fake_catalog, tmp_lake, CALL)
    listing = await asyncio.to_thread(read_option_listing, "SPY", FRIDAY)
    staging = resolve_staging_root("raw") / "option_sessions" / "SPY" / str(session_open_ms_utc(FRIDAY))
    outside = tmp_path / "outside"
    outside_generation = outside / option_session_dch() / listing.sha256
    outside_generation.mkdir(parents=True)
    sentinel = outside_generation / "sentinel"
    sentinel.write_bytes(b"retained")
    def replace_session_directory(request: httpx.Request) -> httpx.Response:
        staging.rename(staging.with_name(staging.name + "-original"))
        staging.symlink_to(outside, target_is_directory=True)
        return httpx.Response(500) if provider_failure else no_trades(CALL.occ_symbol)
    respx.get(url(CALL)).mock(side_effect=replace_session_directory)
    with pytest.raises(ValueError, match="escapes root"):
        await capture_option_session("SPY", FRIDAY, now_ms=NOW_2026_10_12_MS)
    assert list(outside_generation.iterdir()) == [sentinel] and sentinel.read_bytes() == b"retained"


@respx.mock
async def test_capture_cleanup_refuses_a_parent_symlink_before_deleting_outside_bytes(fake_catalog, tmp_lake: Path, tmp_path: Path) -> None:
    from app.data_lake.option_session_capture import _clear_committed_staging
    from app.data_lake.path_policy import resolve_staging_root

    captured = await store_listing([vendor_listing_row(CALL)])
    staging_root = resolve_staging_root("raw")
    capture_root = staging_root / "option_sessions"
    capture_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside_generation = outside / "generation"
    outside_generation.mkdir(parents=True)
    sentinel = outside_generation / "sentinel"
    sentinel.write_bytes(b"keep")
    (capture_root / "SPY").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes root"):
        await _clear_committed_staging(capture_root / "SPY" / "generation", captured, staging_root=staging_root)
    assert sentinel.read_bytes() == b"keep"
