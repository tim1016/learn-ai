"""Downloading what the past chain needs into the lake (#3030).

The vendor is faked with ``respx`` (the answer shapes it really sends, ``tests/_helpers/option_vendor.py``), the catalog
is the tests' fake one, and the stock engine's fetch of the underlying (``materialize_engine_run``) is replaced by a double
that writes the stock file and its receipt the way a fetch does, and counts how often it was asked. A route that is not
mocked fails the test if it is called, so "no request" is checked and not assumed.

Hand-worked figures: 2025-06-13 is a full Friday session; "now" is 2026-10-12 14:00 New York, so the options plan, which
reaches two years back with two days of headroom, reaches 2024-10-14. 2024-06-14 is before that.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake import option_bars as option_bars_module
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_listing import OptionListingFileError
from app.data_lake.run_materialization import LakeMaterializationError
from app.options.contract import OptionContract
from app.options_backtest import chain_download
from app.options_backtest.chain_download import (
    MAX_CONTRACTS_PER_DOWNLOAD,
    MAX_REQUESTS_IN_FLIGHT,
    ContractNotOfExpiry,
    DownloadItem,
    ItemKind,
    Outcome,
    TooManyContracts,
    ensure_chain_data,
)
from app.options_backtest.lake_data import LakeRunData
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_vendor import no_trades, traded
from tests.options_backtest._lake import (
    BARS_URL,
    CONTRACTS_URL,
    FRIDAY,
    FRIDAY_OPEN_MS,
    MINUTE_MS,
    StockFetchDouble,
    minute_bar,
    raw_root,
    replace_the_stored_file,
    stock_bar,
    stock_day_zip,
    store_underlying_day,
    symbol_asked,
    vendor_bars,
    vendor_lists,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

STOCK_PATH = "equity/usa/minute/spy/20250613_trade.zip"


def _call(strike: int, day: date = FRIDAY) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=day, right="call", strike=strike)


@pytest.fixture
def materializer(fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch) -> StockFetchDouble:
    double = StockFetchDouble(fake_catalog, tmp_lake)
    monkeypatch.setattr(chain_download, "materialize_engine_run", double)
    return double


def _summary(items: Sequence[DownloadItem]) -> list[tuple[str, str, str, str | None]]:
    return [(item.kind.value, item.subject, item.outcome.value, item.reason) for item in items]


CALL_600, CALL_601, CALL_602 = _call(600), _call(601), _call(602)
SYMBOL_600, SYMBOL_601, SYMBOL_602 = CALL_600.occ_symbol, CALL_601.occ_symbol, CALL_602.occ_symbol


# ── What is fetched, and in what order ────────────────────────────────────


@respx.mock
async def test_a_download_captures_the_listing_the_underlying_and_each_contract_and_reports_them_in_that_order(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    listing_route = vendor_lists(CALL_600, CALL_601, CALL_602)
    bars_route = vendor_bars(
        lambda symbol: (
            traded(symbol, [minute_bar(0, 1.27, 1.31, 1.25, 1.27)]) if symbol == SYMBOL_600 else no_trades(symbol)
        )
    )

    items = await ensure_chain_data("SPY", FRIDAY, expiry=FRIDAY, contracts=[CALL_600, CALL_601])

    assert _summary(items) == [
        ("listing", "SPY", "fetched", None),
        ("underlying", "SPY", "fetched", None),
        ("contract", SYMBOL_600, "fetched", None),
        ("contract", SYMBOL_601, "fetched", None),
    ]
    # One request for the listing, one for each contract, one fetch of the underlying, and for the day asked.
    assert (listing_route.call_count, bars_route.call_count, len(materializer.calls)) == (1, 2, 1)
    assert materializer.calls[0]["symbol"] == "SPY" and (
        materializer.calls[0]["start"],
        materializer.calls[0]["end"],
    ) == (
        FRIDAY,
        FRIDAY,
    )
    assert (materializer.calls[0]["resolution"], materializer.calls[0]["price_adjustment_mode"]) == ("minute", "raw")
    # And the lake now holds all of it, read back through the reader a run uses.
    data = LakeRunData("SPY")
    assert (await asyncio.to_thread(data.listing, FRIDAY)) is not None
    assert (await asyncio.to_thread(data.underlying_day, FRIDAY)) is not None
    traded_day = await asyncio.to_thread(data.option_day, CALL_600, FRIDAY)
    quiet_day = await asyncio.to_thread(data.option_day, CALL_601, FRIDAY)
    assert traded_day is not None and [bar.close_ms for bar in traded_day.value] == [FRIDAY_OPEN_MS + MINUTE_MS]
    assert quiet_day is not None and tuple(quiet_day.value) == ()
    assert (await asyncio.to_thread(data.option_day, CALL_602, FRIDAY)) is None  # listed, and not asked for


@respx.mock
async def test_asking_again_asks_the_vendor_for_nothing_and_says_it_was_already_stored(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    listing_route = vendor_lists(CALL_600, CALL_601)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))
    await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601])

    again = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601])

    assert _summary(again) == [
        ("listing", "SPY", "already_stored", None),
        ("underlying", "SPY", "already_stored", None),
        ("contract", SYMBOL_600, "already_stored", None),
        ("contract", SYMBOL_601, "already_stored", None),
    ]
    assert (listing_route.call_count, bars_route.call_count, len(materializer.calls)) == (1, 2, 1)


@respx.mock
async def test_a_contract_named_more_than_once_is_asked_for_once_and_reported_once(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600, CALL_601)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601, CALL_600, CALL_600])

    assert [item.subject for item in items if item.kind is ItemKind.CONTRACT] == [SYMBOL_600, SYMBOL_601]
    assert bars_route.call_count == 2


@respx.mock
async def test_with_no_contracts_named_only_the_listing_and_the_underlying_are_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[])

    assert _summary(items) == [("listing", "SPY", "fetched", None), ("underlying", "SPY", "fetched", None)]


@respx.mock
async def test_the_listing_is_read_once_and_handed_to_every_contract_capture(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor_lists(CALL_600, CALL_601, CALL_602)
    vendor_bars(lambda symbol: no_trades(symbol))
    reads = []
    real = option_bars_module.read_option_listing
    monkeypatch.setattr(
        option_bars_module, "read_option_listing", lambda *args, **kwargs: reads.append(args) or real(*args, **kwargs)
    )

    await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601, CALL_602])

    assert reads == []  # the lake's bars capture was handed the listing and did not read it again


# ── Typed failures are items, never raised ────────────────────────────────


@respx.mock
async def test_a_failure_is_one_item_with_the_lakes_reason_and_the_others_are_still_captured(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600, CALL_601)  # 602 is not listed
    bars_route = vendor_bars(
        lambda symbol: httpx.Response(429, json={"status": "ERROR"}) if symbol == SYMBOL_601 else no_trades(symbol)
    )

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601, CALL_602])

    assert _summary(items) == [
        ("listing", "SPY", "fetched", None),
        ("underlying", "SPY", "fetched", None),
        ("contract", SYMBOL_600, "fetched", None),
        ("contract", SYMBOL_601, "failed", "provider_rate_limited"),
        ("contract", SYMBOL_602, "failed", "not_listed"),
    ]
    # The unlisted contract cost no request: the vendor says the same for a contract that never existed.
    assert bars_route.call_count == 2
    assert items[3].detail and items[4].detail


@respx.mock
async def test_when_the_listing_cannot_be_captured_no_contract_is_tried_and_each_says_why(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    respx.get(CONTRACTS_URL).mock(return_value=httpx.Response(429, json={"status": "ERROR"}))
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601])

    assert _summary(items) == [
        ("listing", "SPY", "failed", "provider_rate_limited"),
        ("underlying", "SPY", "fetched", None),
        ("contract", SYMBOL_600, "failed", "listing_not_captured"),
        ("contract", SYMBOL_601, "failed", "listing_not_captured"),
    ]
    assert bars_route.call_count == 0
    assert "provider_rate_limited" in (items[2].detail or "")


@pytest.fixture
def stock_fetch_that_must_not_run(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> StockFetchDouble:
    """The stock engine's fetch, made to fail the test if it is asked for anything at all."""
    double = StockFetchDouble(
        fake_catalog, tmp_lake, error=AssertionError("the stock engine was asked for the underlying's bars")
    )
    monkeypatch.setattr(chain_download, "materialize_engine_run", double)
    return double


# 2025-06-13's session closes at 16:00 EDT = 20:00 UTC = 1749844800000, and the lake takes a session's data from the
# New York midnight after its date: 2025-06-14 00:00 EDT = 04:00 UTC. Worked from the epoch: 2025-01-01 is day 20089
# (55 years of 365 days and 14 leap days), 2025-06-14 is 164 days on (31 + 28 + 31 + 30 + 31 + 13), day 20253, and
# 20253 * 86 400 000 = 1749859200000 is that UTC midnight; four hours later is 1749859200000 + 14 400 000.
CAPTURE_OPENS_MS = 1749873600000


@respx.mock
async def test_a_session_that_is_not_over_asks_the_stock_engine_for_nothing_and_fails_the_underlying_like_the_listing(
    fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch_that_must_not_run: StockFetchDouble
) -> None:
    listing_route = vendor_lists(CALL_600)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))
    an_hour_into_the_session = (
        FRIDAY_OPEN_MS + 60 * MINUTE_MS
    )  # the session is not over, whatever the lake's rule for "over"

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600], now_ms=an_hour_into_the_session)

    assert _summary(items) == [
        ("listing", "SPY", "failed", "session_not_over"),
        ("underlying", "SPY", "failed", "session_not_over"),
        ("contract", SYMBOL_600, "failed", "listing_not_captured"),
    ]
    assert stock_fetch_that_must_not_run.calls == []
    assert listing_route.call_count == 0 and bars_route.call_count == 0
    # The lake's own words, the same for the underlying as for the listing: nothing here restates its rule.
    assert items[1].detail and items[1].detail == items[0].detail
    # And nothing is stored: asked again once the session is over, the underlying is fetched.
    assert (await asyncio.to_thread(LakeRunData("SPY").underlying_day, FRIDAY)) is None


@pytest.mark.parametrize(
    ("now_ms", "fetched"),
    [
        pytest.param(CAPTURE_OPENS_MS - 1, False, id="one millisecond before the New York midnight after the session"),
        pytest.param(CAPTURE_OPENS_MS, True, id="the millisecond the New York date has passed"),
    ],
)
@respx.mock
async def test_the_underlying_is_fetched_from_the_millisecond_the_lake_takes_the_sessions_data_and_not_before(
    now_ms: int, fetched: bool, fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[], now_ms=now_ms)

    assert _summary(items) == (
        [("listing", "SPY", "fetched", None), ("underlying", "SPY", "fetched", None)]
        if fetched
        else [("listing", "SPY", "failed", "session_not_over"), ("underlying", "SPY", "failed", "session_not_over")]
    )
    assert len(materializer.calls) == (1 if fetched else 0)


@pytest.mark.parametrize("no_session", [date(2025, 6, 14), date(2025, 6, 19)], ids=["a Saturday", "Juneteenth"])
@respx.mock
async def test_a_date_that_is_not_a_session_asks_the_stock_engine_for_nothing_and_fails_the_underlying_like_the_listing(
    no_session: date, fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch_that_must_not_run: StockFetchDouble
) -> None:
    items = await ensure_chain_data("SPY", no_session, expiry=None, contracts=[])

    assert _summary(items) == [
        ("listing", "SPY", "failed", "not_a_session"),
        ("underlying", "SPY", "failed", "not_a_session"),
    ]
    assert stock_fetch_that_must_not_run.calls == []  # and an un-mocked request to the vendor would have failed the test
    assert items[1].detail and items[1].detail == items[0].detail


@respx.mock
async def test_todays_session_before_new_york_midnight_is_not_fetched_on_the_lakes_own_clock(
    fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch_that_must_not_run: StockFetchDouble
) -> None:
    # No clock is passed, so the lake's is used: the tests' fixed one, 2026-10-12 at 14:00 New York, with the session of
    # that Monday still to close at 16:00 and its date to end at midnight.
    items = await ensure_chain_data("SPY", date(2026, 10, 12), expiry=None, contracts=[])

    assert _summary(items) == [
        ("listing", "SPY", "failed", "session_not_over"),
        ("underlying", "SPY", "failed", "session_not_over"),
    ]
    assert stock_fetch_that_must_not_run.calls == []


@respx.mock
async def test_an_underlying_already_held_is_reported_stored_even_while_its_session_is_not_over(
    fake_catalog: FakeCatalog, tmp_lake: Path, stock_fetch_that_must_not_run: StockFetchDouble
) -> None:
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25")]
    )

    items = await ensure_chain_data(
        "SPY", FRIDAY, expiry=None, contracts=[], now_ms=FRIDAY_OPEN_MS + 60 * MINUTE_MS
    )

    assert items[1].outcome is Outcome.ALREADY_STORED and items[1].reason is None
    assert stock_fetch_that_must_not_run.calls == []


@respx.mock
async def test_a_date_before_the_plans_reach_fails_each_contract_as_outside_the_plan_without_asking_the_vendor(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    old_friday = date(2024, 6, 14)  # before 2024-10-14, which the plan reaches from 2026-10-12
    contract = _call(600, old_friday)
    vendor_lists(contract)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))

    items = await ensure_chain_data("SPY", old_friday, expiry=None, contracts=[contract])

    assert (items[0].kind, items[0].outcome) == (ItemKind.LISTING, Outcome.FETCHED)  # a listing has no plan floor
    assert (items[2].kind, items[2].outcome, items[2].reason) == (
        ItemKind.CONTRACT,
        Outcome.FAILED,
        "outside_data_plan",
    )
    assert bars_route.call_count == 0


# ── The underlying ────────────────────────────────────────────────────────


@respx.mock
async def test_an_underlying_already_held_is_not_fetched_again(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600)
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25")]
    )

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[])

    assert items[1].outcome is Outcome.ALREADY_STORED and materializer.calls == []


@respx.mock
async def test_an_underlying_the_stock_function_cannot_fetch_is_a_failed_item_with_the_reason_and_the_rest_goes_on(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = StockFetchDouble(
        fake_catalog, tmp_lake, error=LakeMaterializationError("the lake could not materialize SPY")
    )
    monkeypatch.setattr(chain_download, "materialize_engine_run", failing)
    vendor_lists(CALL_600)
    vendor_bars(lambda symbol: no_trades(symbol))

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600])

    assert _summary(items) == [
        ("listing", "SPY", "fetched", None),
        ("underlying", "SPY", "failed", "underlying_not_fetched"),
        ("contract", SYMBOL_600, "fetched", None),
    ]
    assert "could not materialize" in (items[1].detail or "")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(lambda: b"admitted, but not a zip", id="a damaged file"),
        # The second bar starts at 09:31:30, 1749821400000 + 60 000 + 30 000 = 1749821490000, off the minute.
        pytest.param(
            lambda: stock_day_zip(
                [
                    stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25"),
                    stock_bar(FRIDAY_OPEN_MS + MINUTE_MS + 30_000, "600.25", "600.60", "600.20", "600.40"),
                ]
            ),
            id="a bar off the minute",
        ),
    ],
)
@respx.mock
async def test_a_damaged_underlying_file_is_reported_and_is_not_fetched_over(
    payload: Callable[[], bytes], fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600)
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(FRIDAY_OPEN_MS, "600.00", "600.50", "599.75", "600.25")]
    )
    replace_the_stored_file(fake_catalog, raw_root(tmp_lake), STOCK_PATH, payload(), admit=True)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[])

    assert (items[1].outcome, items[1].reason) == (Outcome.FAILED, "underlying_unreadable")
    assert materializer.calls == []


@pytest.mark.parametrize(
    "read_back",
    [
        pytest.param(
            OptionListingFileError("/srv/lake/raw/option/usa/listing/spy/20250613_contracts.zip: bad"), id="damaged"
        ),
        pytest.param(
            LakeAdmissionError("/srv/lake/raw/option/usa/listing/spy/20250613_contracts.zip: no receipt"),
            id="not admitted",
        ),
        pytest.param(None, id="gone"),
    ],
)
@respx.mock
async def test_a_listing_captured_and_then_not_readable_fails_the_listing_and_every_contract_and_asks_for_no_bars(
    read_back: Exception | None,
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    materializer: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_lists(CALL_600, CALL_601)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))

    def read_listing(*args: object, **kwargs: object) -> None:
        if read_back is not None:
            raise read_back

    monkeypatch.setattr(chain_download, "read_option_listing", read_listing)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601])

    assert _summary(items) == [
        ("listing", "SPY", "failed", "listing_not_captured"),
        ("underlying", "SPY", "fetched", None),
        ("contract", SYMBOL_600, "failed", "listing_not_captured"),
        ("contract", SYMBOL_601, "failed", "listing_not_captured"),
    ]
    assert bars_route.call_count == 0
    detail = items[0].detail or ""
    assert "SPY listing" in detail and "2025-06-13" in detail
    if read_back is not None:
        assert type(read_back).__name__ in detail
    # The lake's words carry the path of the file on the server; the item names what and the kind of error, not the file.
    assert "/srv" not in detail and ".zip" not in detail
    assert "so no contract of it can be vouched for" in (items[2].detail or "")


# ── Requests refused before anything is asked ─────────────────────────────


@respx.mock
async def test_more_than_sixty_distinct_contracts_are_refused_with_a_reason_before_anything_is_asked(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    sixty_one = [_call(500 + n) for n in range(MAX_CONTRACTS_PER_DOWNLOAD + 1)]

    with pytest.raises(TooManyContracts) as caught:
        await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=sixty_one)

    assert caught.value.code == "OPTIONS_BACKTEST_TOO_MANY_CONTRACTS" and "61" in str(caught.value)
    assert materializer.calls == []


@respx.mock
async def test_exactly_sixty_contracts_are_taken_and_a_repeat_is_not_counted_twice(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    sixty = [_call(500 + n) for n in range(MAX_CONTRACTS_PER_DOWNLOAD)]
    vendor_lists(*sixty)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[*sixty, sixty[0]])

    assert len(items) == 2 + MAX_CONTRACTS_PER_DOWNLOAD and bars_route.call_count == MAX_CONTRACTS_PER_DOWNLOAD
    assert all(item.outcome is Outcome.FETCHED for item in items)


@respx.mock
async def test_a_contract_that_does_not_expire_on_the_expiry_named_is_refused_before_anything_is_asked(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    other_expiry = _call(600, date(2025, 6, 20))

    with pytest.raises(ContractNotOfExpiry) as caught:
        await ensure_chain_data("SPY", FRIDAY, expiry=FRIDAY, contracts=[CALL_600, other_expiry])

    assert other_expiry.occ_symbol in str(caught.value) and SYMBOL_600 not in str(caught.value)
    assert caught.value.code == "OPTIONS_BACKTEST_CONTRACT_NOT_OF_EXPIRY"
    assert materializer.calls == []


@respx.mock
async def test_an_underlying_the_lake_cannot_hold_is_refused(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    with pytest.raises(ValueError, match="upper-case ticker"):
        await ensure_chain_data("spy", FRIDAY, expiry=None, contracts=[])

    assert materializer.calls == []


# ── Concurrency and outages ───────────────────────────────────────────────


@respx.mock
async def test_no_more_than_four_requests_are_in_flight_and_four_are(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    contracts = [_call(500 + n) for n in range(12)]
    vendor_lists(*contracts)
    in_flight = 0
    peak = 0

    async def slow_answer(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return no_trades(symbol_asked(request))

    respx.get(url__regex=BARS_URL).mock(side_effect=slow_answer)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=contracts)

    assert MAX_REQUESTS_IN_FLIGHT == 4 and peak == 4
    assert all(item.outcome is Outcome.FETCHED for item in items)


@respx.mock
async def test_the_underlyings_fetch_waits_for_a_free_place_among_the_requests_in_flight(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    # One call fetches the listing and the underlying together and only then the contracts, so the fetch of the
    # underlying never meets four contract requests; the place it takes is the rule that it is one of the requests
    # counted, and is shown here with every place taken.
    gate = asyncio.Semaphore(1)
    await gate.acquire()
    fetch = asyncio.create_task(chain_download._capture_underlying("SPY", FRIDAY, gate, None))

    await asyncio.sleep(0.2)
    waited = (fetch.done(), len(materializer.calls))
    gate.release()
    item = await fetch

    assert waited == (False, 0)
    assert (item.outcome, len(materializer.calls)) == (Outcome.FETCHED, 1)


@respx.mock
async def test_the_clock_the_caller_gives_is_the_one_the_contract_capture_uses_for_the_data_plan(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))
    # Two years after the fixed clock (2026-10-12 14:00 New York = 1 791 828 000 000 ms): 365 days to 2027-10-12 and 366
    # to 2028-10-12 (it holds 29 February 2028), 731 * 86 400 000 = 63 158 400 000 ms, so 1 854 986 400 000. The plan
    # then reaches back to 2026-10-14, and 2025-06-13 is before it; at the fixed clock it is well inside.
    two_years_on = 1_854_986_400_000

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600], now_ms=two_years_on)

    assert (items[0].kind, items[0].outcome) == (ItemKind.LISTING, Outcome.FETCHED)  # a listing has no plan floor
    assert (items[2].outcome, items[2].reason) == (Outcome.FAILED, "outside_data_plan")
    assert bars_route.call_count == 0


@respx.mock
async def test_an_unreachable_catalog_is_raised_after_the_other_capture_has_finished_and_not_cancelled(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: object, **kwargs: object) -> int:
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", down)
    vendor_lists(CALL_600)

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600])

    # The underlying's fetch ran to its end, and its file is stored.
    assert len(materializer.calls) == 1
    assert (await asyncio.to_thread(LakeRunData("SPY").underlying_day, FRIDAY)) is not None


@respx.mock
async def test_the_catalog_pool_of_the_calling_loop_is_made_before_the_first_capture_touches_the_catalog(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    made_pool, claimed = catalog_client.init_pool, catalog_client.claim_metadata_artifact

    async def init_pool() -> None:
        order.append("pool")
        await made_pool()

    async def claim_metadata_artifact(*args: object, **kwargs: object) -> int | None:
        order.append("claim")
        return await claimed(*args, **kwargs)

    monkeypatch.setattr(catalog_client, "init_pool", init_pool)
    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", claim_metadata_artifact)
    vendor_lists(CALL_600)

    await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[])

    assert order[:2] == ["pool", "claim"]
