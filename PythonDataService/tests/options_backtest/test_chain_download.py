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
import threading
from collections.abc import Callable, Coroutine, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import respx

from app.data_lake import catalog_client, option_lake
from app.data_lake import option_bars as option_bars_module
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_lake import MAX_VENDOR_REQUESTS_IN_FLIGHT, vendor_request_gate
from app.data_lake.option_listing import OptionListingFileError
from app.data_lake.run_materialization import LakeMaterializationError
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest import chain_download
from app.options_backtest.chain_download import (
    MAX_CONTRACTS_PER_DOWNLOAD,
    ContractNotOfExpiry,
    DownloadItem,
    ItemKind,
    Outcome,
    TooManyContracts,
    ensure_chain_data,
    fetch_refusal,
)
from app.options_backtest.data_check import lake_reason_words
from app.options_backtest.fetching import ensure_run_data
from app.options_backtest.lake_data import LakeRunData
from app.utils.background_loop import background_loop
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.option_lake_cases import DISK_FULL_AT_THE_RENAME, install_write_fault
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
    vendor_listing_row,
    vendor_lists,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import THU, WED
from tests.options_backtest._run_lake import Vendor, eight_legs_setup, ladder_day

STOCK_PATH = "equity/usa/minute/spy/20250613_trade.zip"
# 2026-10-12 14:00 New York.
NOW_MS = 1791828000000


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
    assert (
        stock_fetch_that_must_not_run.calls == []
    )  # and an un-mocked request to the vendor would have failed the test
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

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[], now_ms=FRIDAY_OPEN_MS + 60 * MINUTE_MS)

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


@respx.mock
async def test_a_contract_whose_file_cannot_be_written_is_a_failed_item_with_io_error_and_the_rest_is_stored(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor_lists(CALL_600, CALL_601)
    vendor_bars(lambda symbol: no_trades(symbol))
    fault = install_write_fault(monkeypatch, DISK_FULL_AT_THE_RENAME, only_bars=True)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601])

    assert _summary(items) == [
        ("listing", "SPY", "fetched", None),
        ("underlying", "SPY", "fetched", None),
        ("contract", SYMBOL_600, "failed", "io_error"),
        ("contract", SYMBOL_601, "failed", "io_error"),
    ]
    assert fault.hits == 2
    # The words say what failed and the system's reason, and name no path on the server's disk.
    assert all("No space left on device" in (item.detail or "") for item in items[2:])
    assert all(str(tmp_lake) not in (item.detail or "") for item in items)
    # Nothing is left fetching: the listing and the underlying's day are complete and the two contract rows are failed.
    assert sorted(row["status"] for row in fake_catalog.rows.values()) == ["complete", "complete", "failed", "failed"]


# ── A download that is cancelled finishes what it started ─────────────────

# The captures run on the process's background loop, so the vendor's answers (mocked, and awaited by the lake's HTTP
# client) are awaited there too, and the test's loop is another. What a test holds the vendor with is therefore a
# ``threading.Event`` polled on the background loop, never an ``asyncio.Event`` of the test's loop.


async def _until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the captures never reached the vendor")


async def _held_until(release: threading.Event) -> None:
    """Wait, on the loop the vendor's answer is awaited on, until the test sets ``release``."""
    while not release.is_set():
        await asyncio.sleep(0.002)


def _statuses(fake_catalog: FakeCatalog) -> list[str]:
    # A snapshot: the captures add rows from another thread.
    return sorted(row["status"] for row in list(fake_catalog.rows.values()))


class SlowBars:
    """The vendor's answer to every contract's bars: held until ``release`` is set, then ``latency`` seconds more."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.latency = 0.0
        self.asked: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self._together = 0

    def hold_the_first(self, count: int, *, for_at_most: float = 10.0) -> None:
        """Answer none until ``count`` are in flight together, so a count of the most in flight at once sees that many
        however slowly the requests arrive. If they never are, they are let go after ``for_at_most`` seconds, so a
        fault shows as a count that is wrong and not as a hang."""
        self.release = threading.Event()
        self._together = count
        opens_it_anyway = threading.Timer(for_at_most, self.release.set)
        opens_it_anyway.daemon = True
        opens_it_anyway.start()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        symbol = symbol_asked(request)
        self.asked.append(symbol)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self._together and self.in_flight >= self._together:
            self.release.set()
        try:
            await _held_until(self.release)
            await asyncio.sleep(self.latency)
        finally:
            self.in_flight -= 1
        return no_trades(symbol)


@respx.mock
async def test_a_download_cancelled_while_the_listing_is_fetched_finishes_the_listing_and_the_underlying_then_ends_cancelled(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    release = threading.Event()
    listing_asked = threading.Event()

    async def listing_after_release(request: httpx.Request) -> httpx.Response:
        listing_asked.set()
        await _held_until(release)
        return httpx.Response(200, json={"request_id": "r", "status": "OK", "results": [vendor_listing_row(CALL_600)]})

    respx.get(CONTRACTS_URL).mock(side_effect=listing_after_release)
    bars_route = vendor_bars(lambda symbol: no_trades(symbol))
    download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600]))
    await _until(listing_asked.is_set)
    assert "fetching" in _statuses(fake_catalog)  # the listing holds its claimed row and waits on the vendor

    download.cancel()
    await asyncio.sleep(0.1)
    ended_before_the_listing_did = download.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await download

    assert ended_before_the_listing_did is False
    assert download.cancelled()
    # Two rows, the listing and the underlying's day, and both ran to their end (the listing reads back); the contract
    # was never started, because it had not begun when the download was cancelled.
    assert _statuses(fake_catalog) == ["complete", "complete"]
    assert (await asyncio.to_thread(LakeRunData("SPY").listing, FRIDAY)) is not None
    assert bars_route.call_count == 0


@pytest.mark.parametrize("cancels", [1, 2], ids=["cancelled once", "cancelled again while it waits"])
@respx.mock
async def test_a_download_cancelled_while_contracts_are_fetched_finishes_each_one_then_ends_cancelled(
    cancels: int, fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    vendor_lists(CALL_600, CALL_601)
    bars = SlowBars()
    respx.get(url__regex=BARS_URL).mock(side_effect=bars)
    download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601]))
    await _until(lambda: len(bars.asked) == 2)  # both contracts hold their claimed rows and wait on the vendor

    for _ in range(cancels):
        download.cancel()
        await asyncio.sleep(0.05)
    ended_before_the_contracts_did = download.done()
    bars.release.set()
    with pytest.raises(asyncio.CancelledError):
        await download

    assert ended_before_the_contracts_did is False
    assert download.cancelled()
    # Four rows, the listing, the underlying's day and the two contracts; each ran to its end and none is left fetching.
    assert _statuses(fake_catalog) == ["complete", "complete", "complete", "complete"]


@respx.mock
async def test_a_download_cancelled_while_captures_wait_for_a_place_at_the_gate_ends_every_one_and_frees_every_place(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    # Six contracts and four places: four captures hold a place and wait on the vendor, two wait at the gate. The
    # download is cancelled then. Every capture ends, the two that waited included, so there are eight rows, the listing,
    # the underlying's day and six contracts, all complete, and six requests for bars (4 held + 2 that got a place after).
    # Then a second download of eight other contracts, each request taking 0.05 s, gets all four places: it has
    # eight contracts to ask for and the most in flight at once is four. A place the first download kept would show as three.
    first_six = [_call(600 + n) for n in range(6)]
    next_eight = [_call(610 + n) for n in range(8)]
    vendor_lists(*first_six, *next_eight)
    bars = SlowBars()
    respx.get(url__regex=BARS_URL).mock(side_effect=bars)
    download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=first_six))
    await _until(lambda: len(bars.asked) == MAX_VENDOR_REQUESTS_IN_FLIGHT)

    download.cancel()
    await asyncio.sleep(0.1)
    waited = (download.done(), len(bars.asked))
    bars.release.set()
    with pytest.raises(asyncio.CancelledError):
        await download

    assert MAX_VENDOR_REQUESTS_IN_FLIGHT == 4
    assert waited == (False, 4), "two captures still waited for a place, and the download had not ended"
    assert download.cancelled()
    assert _statuses(fake_catalog) == ["complete"] * 8
    assert len(bars.asked) == 6

    bars.peak, bars.latency = 0, 0.05
    bars.hold_the_first(4)
    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=next_eight)

    assert bars.peak == 4, "a place the cancelled download kept would leave three"
    assert [item.outcome for item in items[2:]] == [Outcome.FETCHED] * 8


@respx.mock
async def test_an_error_the_captures_meet_after_the_download_was_cancelled_is_logged_and_the_download_ends_cancelled(
    fake_catalog: FakeCatalog,
    tmp_lake: Path,
    materializer: StockFetchDouble,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    claimed, release = threading.Event(), threading.Event()

    async def down_once_released(*args: object, **kwargs: object) -> int:
        claimed.set()
        await _held_until(release)
        raise catalog_client.CatalogUnavailableError("down")

    monkeypatch.setattr(catalog_client, "claim_metadata_artifact", down_once_released)
    vendor_lists(CALL_600)
    download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600]))
    await _until(claimed.is_set)

    download.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with caplog.at_level("WARNING"), pytest.raises(asyncio.CancelledError):
        await download

    assert download.cancelled(), "the caller was cancelled, so it ends cancelled and not with the outage"
    [record] = [r for r in caplog.records if getattr(r, "action", "") == "chain_download_error_after_cancel"]
    assert record.error_type == "CatalogUnavailableError"
    # The underlying's fetch ran to its end beside the listing that failed.
    assert len(materializer.calls) == 1


@respx.mock
async def test_a_download_cancelled_before_its_first_capture_begins_captures_nothing(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The captures wait for the loop's catalog pool before the first of them is asked. The download is cancelled while
    # that pool is being made, so by the time it is, the caller has gone and the listing and the underlying, which had
    # not begun, never do: no request for the listing, no fetch of the underlying, no row.
    reached, release = threading.Event(), threading.Event()
    made_pool = catalog_client.init_pool

    async def slow_pool() -> None:
        reached.set()
        await _held_until(release)
        await made_pool()

    monkeypatch.setattr(catalog_client, "init_pool", slow_pool)
    listing_route = vendor_lists(CALL_600)
    download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600]))
    await _until(reached.is_set)

    download.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await download

    assert (listing_route.call_count, len(materializer.calls), _statuses(fake_catalog)) == (0, 0, [])


async def test_a_cancel_scope_that_cancels_again_at_every_checkpoint_does_not_make_the_wait_spin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The captures take 0.3 s. A scope of anyio sends a cancellation at every checkpoint of the task it holds, so each
    # wait ends at once and a loop that only swallows them goes round tens of thousands of times in that 0.3 s (42 209
    # in 1.5 s when it was measured). The wait is entered twice: once to meet the first cancellation, once shielded
    # from the scope for the rest (the bound allows one more). The captures end first, and the first cancellation is
    # raised after them.
    loop = asyncio.get_running_loop()
    work: asyncio.Future[tuple[DownloadItem, ...]] = loop.create_future()
    loop.call_later(0.3, work.set_result, ())
    caller_has_gone = threading.Event()
    waits = 0
    wait = asyncio.wait

    async def counted_wait(*args: Any, **kwargs: Any) -> Any:
        nonlocal waits
        waits += 1
        return await wait(*args, **kwargs)

    monkeypatch.setattr(chain_download.asyncio, "wait", counted_wait)
    cancelled = False

    with anyio.CancelScope() as scope:
        scope.cancel()
        try:
            await chain_download._waited_for(work, caller_has_gone)
        except asyncio.CancelledError:
            cancelled = True

    assert cancelled and work.done() and caller_has_gone.is_set()
    assert waits <= 3


# ── Why the lake would fetch nothing for a session ───────────────────────
# The lake takes a session's option data from the New York midnight after its date. Friday 2025-06-13 is taken from
# Saturday's midnight, 2025-06-14 00:00 EDT = 04:00 UTC = 1 749 873 600 000 ms.
TAKEN_FROM_MS = 1749873600000


def test_a_session_is_refused_until_the_millisecond_the_lake_takes_it_and_not_after() -> None:
    before = fetch_refusal("SPY", FRIDAY, now_ms=TAKEN_FROM_MS - 1)
    at_it = fetch_refusal("SPY", FRIDAY, now_ms=TAKEN_FROM_MS)

    assert before is not None and before.reason == "session_not_over"
    assert before.words == lake_reason_words("session_not_over")
    assert at_it is None


def test_a_date_that_is_not_a_session_is_refused_as_that_whatever_the_clock_says() -> None:
    # 2025-06-14 is a Saturday.
    refusal = fetch_refusal("SPY", date(2025, 6, 14), now_ms=TAKEN_FROM_MS * 2)

    assert refusal is not None and refusal.reason == "not_a_session"
    assert refusal.words == lake_reason_words("not_a_session")


def test_the_lakes_clock_is_the_one_asked_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tests' clock is fixed at 2026-10-12 14:00 New York, so a session long over is taken and today's is not.
    assert fetch_refusal("SPY", FRIDAY) is None
    assert fetch_refusal("SPY", date(2026, 10, 12)) is not None
    monkeypatch.setattr(option_lake, "current_ms", lambda: TAKEN_FROM_MS - 1)
    assert fetch_refusal("SPY", FRIDAY) is not None


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
    bars = SlowBars()
    bars.hold_the_first(4)
    bars.latency = 0.02
    respx.get(url__regex=BARS_URL).mock(side_effect=bars)

    items = await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=contracts)

    assert MAX_VENDOR_REQUESTS_IN_FLIGHT == 4 and bars.peak == 4
    assert all(item.outcome is Outcome.FETCHED for item in items)


@respx.mock
async def test_a_chain_download_and_a_run_at_once_share_the_one_limit_of_four_requests_in_flight(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The run asks for the eight contract days of Wednesday (a call at each strike from 496 to 504 but the money, 500),
    # and the chain download for the eight of Thursday: sixteen requests in all, each taking 0.4 seconds, and four
    # places. The chain starts when the run's first request reaches the vendor, so its listing and its contracts meet
    # the run's two waves of four. With the one limit the most in flight is four. With a limit of four each it would be
    # eight, the chain's four beside the run's. The chain is awaited here, on the test's loop, as the route awaits it on
    # the server's, and the run goes where a job's goes.
    run_asked = threading.Event()
    vendor = Vendor({WED: ladder_day(), THU: ladder_day()}, option_latency_s=0.4)
    vendor.on_option_request = lambda _symbol: run_asked.set()
    vendor.hold_the_first(4)  # the run's first four are in flight together on any machine
    vendor.mount()
    thursday_calls = [
        OptionContract.of(root="SPY", expiry_date=THU, right="call", strike=strike)
        for strike in (496, 497, 498, 499, 501, 502, 503, 504)
    ]

    async def the_chain_download() -> tuple[DownloadItem, ...]:
        await asyncio.to_thread(run_asked.wait, 20)
        return await ensure_chain_data("SPY", THU, expiry=THU, contracts=thursday_calls)

    report, items = await asyncio.gather(
        asyncio.to_thread(ensure_run_data, eight_legs_setup(), [WED], now_ms=NOW_MS, should_stop=lambda: False),
        the_chain_download(),
    )

    assert MAX_VENDOR_REQUESTS_IN_FLIGHT == 4
    assert (vendor.peak_in_flight, len(vendor.option_requests)) == (4, 16), "two limits of four would peak at eight"
    assert report.option_days_fetched == 8
    assert [item.outcome for item in items] == [Outcome.FETCHED] * 10  # the listing, the underlying and the eight


def _on_the_background_loop[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Future[T]:
    """The coroutine run on the process's background loop, where the vendor request gate lives, to await here."""
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, background_loop()))


async def _take_every_place_at_the_gate() -> None:
    gate = vendor_request_gate()
    for _ in range(MAX_VENDOR_REQUESTS_IN_FLIGHT):
        await gate.acquire()


async def _give_back_places_at_the_gate(places: int) -> None:
    for _ in range(places):
        vendor_request_gate().release()


async def test_the_vendor_request_gate_is_made_on_the_background_loop_only() -> None:
    # The test's own loop is not the background loop: a gate asked for here would be a second limit of four.
    with pytest.raises(RuntimeError, match="background loop"):
        vendor_request_gate()

    async def asked_twice() -> bool:
        return vendor_request_gate() is vendor_request_gate()

    assert await _on_the_background_loop(asked_twice()) is True


@respx.mock
async def test_the_underlyings_fetch_does_not_wait_for_a_place_among_the_option_requests_in_flight(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    # Every place of the process's gate is taken by option requests. The underlying's fetch is the stock pipeline's and
    # not an option request, so it goes ahead at once: one fetch, in far less than the five seconds it is given.
    await _on_the_background_loop(_take_every_place_at_the_gate())
    try:
        item = await asyncio.wait_for(
            _on_the_background_loop(chain_download._capture_underlying("SPY", FRIDAY, None)), 5
        )
    finally:
        await _on_the_background_loop(_give_back_places_at_the_gate(MAX_VENDOR_REQUESTS_IN_FLIGHT))

    assert (item.outcome, len(materializer.calls)) == (Outcome.FETCHED, 1)


class _HeldStockFetch(StockFetchDouble):
    """The stock fetch of the underlying, held on its thread until ``release`` is set."""

    def __init__(self, fake_catalog: FakeCatalog, tmp_lake: Path) -> None:
        super().__init__(fake_catalog, tmp_lake)
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, **arguments: Any) -> Any:
        self.started.set()
        assert self.release.wait(30), "the test never let the fetch go"
        return super().__call__(**arguments)


@respx.mock
async def test_four_option_requests_can_be_in_flight_beside_a_fetch_of_the_underlying(
    fake_catalog: FakeCatalog, tmp_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One download is held in the fetch of its underlying (2025-06-13). A second download, for 2025-06-12 whose
    # underlying is stored, asks for eight contracts at 0.05 s each. All four places are free for it, because the
    # held fetch holds none: four are in flight at once. A fetch that held a place would leave it three.
    held = _HeldStockFetch(fake_catalog, tmp_lake)
    monkeypatch.setattr(chain_download, "materialize_engine_run", held)
    thursday = date(2025, 6, 12)
    open_ms = session_window_for_date(thursday).open_ms_utc
    await store_underlying_day(
        fake_catalog, tmp_lake, [stock_bar(open_ms, "600.00", "600.50", "599.75", "600.25")], day=thursday
    )
    eight = [_call(500 + n) for n in range(8)]
    vendor_lists(*eight)
    bars = SlowBars()
    bars.hold_the_first(4)
    bars.latency = 0.05
    respx.get(url__regex=BARS_URL).mock(side_effect=bars)
    held_download = asyncio.create_task(ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[]))
    await _until(held.started.is_set)

    try:
        items = await ensure_chain_data("SPY", thursday, expiry=None, contracts=eight)
    finally:
        held.release.set()
    await held_download

    assert bars.peak == 4
    assert [item.outcome for item in items[2:]] == [Outcome.FETCHED] * 8


@respx.mock
async def test_the_listings_capture_waits_for_a_free_place_among_the_requests_in_flight(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble
) -> None:
    # Every place of the process's gate is taken; the listing is one of the requests counted, so it waits for one.
    listing_route = vendor_lists(CALL_600)
    await _on_the_background_loop(_take_every_place_at_the_gate())
    try:
        capture = _on_the_background_loop(chain_download._capture_listing("SPY", FRIDAY, None))
        await asyncio.sleep(0.2)
        waited = (capture.done(), listing_route.call_count)
        await _on_the_background_loop(_give_back_places_at_the_gate(1))
        item, listing = await asyncio.wait_for(capture, 5)
    finally:
        await _on_the_background_loop(_give_back_places_at_the_gate(MAX_VENDOR_REQUESTS_IN_FLIGHT - 1))

    assert waited == (False, 0)
    assert (item.outcome, listing_route.call_count, listing is not None) == (Outcome.FETCHED, 1, True)


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
async def test_an_error_in_the_contracts_stage_reaches_the_caller_after_the_other_contracts_have_ended(
    fake_catalog: FakeCatalog, tmp_lake: Path, materializer: StockFetchDouble, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Three contracts. The catalog goes away for the 601 call, whose capture fails before it asks; the 600 and 602 calls
    # take 0.3 s at the vendor. The caller gets the outage, and gets it only when those two have ended: four rows, the
    # listing, the underlying's day and the two contracts, all complete and none fetching. The 601 call is not in an
    # answer that drops it silently: there is no answer.
    vendor_lists(CALL_600, CALL_601, CALL_602)

    async def slow_answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return no_trades(symbol_asked(request))

    respx.get(url__regex=BARS_URL).mock(side_effect=slow_answer)
    capture = chain_download.ensure_option_day_bars

    async def down_for_the_601_call(contract: OptionContract, *args: Any, **kwargs: Any) -> Any:
        if contract.occ_symbol == SYMBOL_601:
            raise catalog_client.CatalogUnavailableError("down")
        return await capture(contract, *args, **kwargs)

    monkeypatch.setattr(chain_download, "ensure_option_day_bars", down_for_the_601_call)

    with pytest.raises(catalog_client.CatalogUnavailableError):
        await ensure_chain_data("SPY", FRIDAY, expiry=None, contracts=[CALL_600, CALL_601, CALL_602])

    assert _statuses(fake_catalog) == ["complete"] * 4


@respx.mock
async def test_the_catalog_pool_of_the_loop_the_captures_run_on_is_made_before_the_first_capture_touches_the_catalog(
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
