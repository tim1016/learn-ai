"""The vendor's real answers for SPY on Tuesday 2025-06-10, through the lake and the run core, to a trade worked by hand (#3030).

Every other test of this feature uses data its author made up. Here the five answers the vendor really sent (recorded on
2026-10-12, byte for byte, in ``tests/fixtures/golden/options-backtest/options-backtest-spy-2025-06-10/v1``) are served
by ``respx`` to the lake's real fetchers, stored in a lake in ``tmp_path``, read back by ``LakeRunData`` and replayed by
``run_sessions`` into a trade that a person works out with a pencil from the same bars. Nothing here touches the
network or a database.

How the expectations were made. Every figure below was read off the recorded files (with ``grep``, and the standard
library's ``json`` to print a bar) and worked out by hand; none was taken from the code under test. Money is ``Decimal`` and
compared exactly, because this is exact arithmetic on recorded prices and a tolerance would only hide a wrong minute.

Times (all New York, EDT = UTC-4 in June, so 09:30 is 13:30 UTC):

* 2025-06-10 is a Tuesday and a full session. The day count from 1970-01-01 is 20 249, so the session opens at
  20 249 x 86 400 s + 13 h 30 min = 1 749 562 200 s = 1749562200000 ms and closes 390 minutes later, at 20:00 UTC =
  1749562200000 + 390 x 60 000 = 1749585600000.
* The vendor stamps a bar with its START. The bar that closes at 09:35 starts at 09:34 = 1749562200000 + 4 x 60 000 =
  1749562440000 and closes at 1749562500000. The bar that closes at 09:36 starts at 09:35 (1749562500000) and closes
  at 1749562560000. The exit bar below starts at 10:00 = 1749562200000 + 30 x 60 000 = 1749564000000 and closes at
  10:01 = 1749564060000.
* The contracts expire on Friday 2025-06-13 at 16:00 New York = 20:00 UTC = 1749844800000 (day 20 252 x 86 400 s +
  20 h). That Friday is the last session of the week of Monday 2025-06-09 to Friday 2025-06-13, all five of them sessions.

The recorded files, counted with ``grep -c`` on the raw bytes and checked against the ``resultsCount`` each file states:
SPY 830 bars (390 of them in the regular session, one for every minute from 09:30 to 15:59), the listing 316
contracts (158 calls, 158 puts, every one American, 100 shares, expiring 2025-06-13), ``O:SPY250613C00600000`` 389
bars, ``O:SPY250613P00600000`` 400 bars, ``O:SPY250613C00680000`` none (``resultsCount: 0`` and no ``results`` key).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from app.data_lake.option_bars import OptionBar, ensure_option_day_bars, read_option_day_bars
from app.data_lake.option_lake import OptionCaptureResult
from app.data_lake.option_listing import ensure_option_listing, read_option_listing
from app.data_lake.run_materialization import EngineRunMaterialization, materialize_engine_run
from app.options.contract import OptionContract, OptionRight
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.market import MinuteBar
from app.options_backtest.results import Event, EventKind, ExitReason, Fill, LegTrade, SessionStatus
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.session_market import ContractDayNotLoaded, DayMarket
from app.options_backtest.setup import (
    AtTheMoney,
    FeePolicy,
    LegSetup,
    OptionsBacktestSetup,
    SessionTime,
    StepsFromTheMoney,
    StrikeRule,
    ThisWeekExpiryRule,
)
from tests._helpers.fake_lake_catalog import FakeCatalog, launcher_responder
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

D = Decimal

RECORDING = Path(__file__).parents[1] / "fixtures/golden/options-backtest/options-backtest-spy-2025-06-10/v1"
TRADING_DATE = date(2025, 6, 10)
EXPIRY = date(2025, 6, 13)

CALL_600 = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=600)
PUT_600 = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="put", strike=600)
CALL_680 = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=680)
# Not in the listing: the vendor never listed a 999 strike (``grep -c C00999000`` on the recorded listing says 0).
CALL_999 = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=999)

# The request path of each recorded answer, and the file that holds the bytes the vendor answered with.
LISTING_PATH = "/v3/reference/options/contracts"
STOCK_PATH = "/v2/aggs/ticker/SPY/range/1/minute/2025-06-10/2025-06-10"
CALL_600_PATH = "/v2/aggs/ticker/O:SPY250613C00600000/range/1/minute/2025-06-10/2025-06-10"
PUT_600_PATH = "/v2/aggs/ticker/O:SPY250613P00600000/range/1/minute/2025-06-10/2025-06-10"
CALL_680_PATH = "/v2/aggs/ticker/O:SPY250613C00680000/range/1/minute/2025-06-10/2025-06-10"
RECORDED_FILES = {
    LISTING_PATH: "spy_contracts_as_of_2025-06-10_expiring_2025-06-13.json",
    STOCK_PATH: "spy_minute_2025-06-10.json",
    CALL_600_PATH: "O_SPY250613C00600000_minute_2025-06-10.json",
    PUT_600_PATH: "O_SPY250613P00600000_minute_2025-06-10.json",
    CALL_680_PATH: "O_SPY250613C00680000_minute_2025-06-10.json",
}
# What the recording asked, and so what the lake's fetchers must ask for the recording to speak for them. The listing
# differs on purpose: it was recorded with ``expiration_date=2025-06-13`` added to keep it to one page, and the lake asks
# for the whole listing as of the date.
BARS_QUERY = {"adjusted": "false", "sort": "asc", "limit": "50000"}
LISTING_QUERY = {"underlying_ticker": "SPY", "as_of": "2025-06-10", "limit": "1000"}

STORED_STOCK_FILE = "equity/usa/minute/spy/20250610_trade.zip"
STORED_LISTING_FILE = "option/usa/listing/spy/20250610_contracts.zip"
STORED_CALL_600_FILE = "option/usa/minute/spy/spy250613c00600000/20250610_trade.zip"


def recorded(name: str) -> bytes:
    return (RECORDING / name).read_bytes()


def recorded_rows(name: str) -> list[dict]:
    """The rows of a recorded body as the standard library reads them: the one oracle here that is not our code."""
    body = json.loads(recorded(name), parse_float=D)
    return body["results"]


@pytest.fixture
def the_vendor() -> Iterator[respx.MockRouter]:
    """The vendor, answering each recorded request with the recorded bytes. A request for any other path fails.

    The LEAN launcher the stock pipeline asks for the exchange calendar is served by the suite's stand-in.
    """
    with respx.mock(assert_all_called=False) as router:
        for path, name in RECORDED_FILES.items():
            router.get(host="api.polygon.io", path=path).mock(
                return_value=httpx.Response(200, content=recorded(name), headers={"content-type": "application/json"})
            )
        router.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(side_effect=launcher_responder())
        yield router


def requests_to_the_vendor(router: respx.MockRouter) -> list[tuple[str, dict[str, str]]]:
    """Each request made to Polygon, as its path and its query (the stock pipeline's key left out, as a key is no fact)."""
    return [
        (call.request.url.path, {k: v for k, v in call.request.url.params.items() if k != "apiKey"})
        for call in router.calls
        if call.request.url.host == "api.polygon.io"
    ]


def stored_rows(tmp_lake: Path, captured: OptionCaptureResult) -> list[str]:
    """The rows of the CSV inside a stored day file, as the lake wrote them."""
    assert captured.record is not None
    with zipfile.ZipFile(raw_root(tmp_lake) / captured.record.file_path) as archive:
        [member] = archive.namelist()
        return archive.read(member).decode("ascii").splitlines()


@dataclass(frozen=True)
class Stored:
    """What capturing the recording left in the lake, and what each capture said."""

    listing: OptionCaptureResult
    days: dict[OptionContract, OptionCaptureResult]
    underlying: EngineRunMaterialization


async def capture_the_recording() -> Stored:
    """The listing, the three contracts' day bars and the underlying's day, each through the lake's own function.

    The listing goes first because a contract's empty day is accepted only for a contract the stored listing holds.
    The underlying goes through ``materialize_engine_run``, the stock engine's function, as the past chain's download
    does (``chain_download._capture_underlying``), on a worker thread.
    """
    listing = await ensure_option_listing("SPY", TRADING_DATE)
    days = {
        contract: await ensure_option_day_bars(contract, TRADING_DATE, listed_under="SPY")
        for contract in (CALL_600, PUT_600, CALL_680)
    }
    underlying = await asyncio.to_thread(
        materialize_engine_run,
        symbol="SPY",
        start=TRADING_DATE,
        end=TRADING_DATE,
        resolution="minute",
        price_adjustment_mode="raw",
    )
    return Stored(listing, days, underlying)


async def off_the_loop(function, *args):
    """The lake's readers block and need a thread with no event loop, as a run's worker thread is."""
    return await asyncio.to_thread(function, *args)


# ── a. Through the lake's real fetchers ─────────────────────────────────────────────────────────────────────────


async def test_the_listing_is_captured_whole_by_the_lakes_own_function_and_asked_for_as_the_recording_asked(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await ensure_option_listing("SPY", TRADING_DATE)

    assert result.failure is None, result.failure
    assert requests_to_the_vendor(the_vendor) == [(LISTING_PATH, LISTING_QUERY)]
    listing = await off_the_loop(read_option_listing, "SPY", TRADING_DATE)
    assert listing is not None
    # 316 tickers in the recorded body (grep -c), 158 of them calls and 158 puts.
    assert len(listing.rows) == 316
    assert sum(row.contract_type == "call" for row in listing.rows) == 158
    assert sum(row.contract_type == "put" for row in listing.rows) == 158
    assert all(row.standard for row in listing.rows)
    # The row of the at-the-money call: strike 600 is 600 000 thousandths, the expiry is 2025-06-13 16:00 New York.
    [row] = [row for row in listing.rows if row.ticker == "O:SPY250613C00600000"]
    assert CALL_600.expiry_ms == 1749844800000 and CALL_600.occ_symbol == "SPY250613C00600000"
    assert (row.contract_type, row.strike_thousandths, row.expiry_ms) == ("call", 600000, 1749844800000)
    assert (row.exercise_style, row.shares_per_contract) == ("american", 100)


async def test_each_contract_day_is_stored_with_exactly_the_bars_the_vendor_sent(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    assert [stored.listing.failure, *(each.failure for each in stored.days.values())] == [None] * 4
    asked = requests_to_the_vendor(the_vendor)
    for path in (CALL_600_PATH, PUT_600_PATH, CALL_680_PATH):
        assert (path, BARS_QUERY) in asked
    # Five answers were recorded and five requests were made, one for each: the listing, the three contracts and the
    # underlying's day. Nothing else was asked of the vendor.
    assert sorted(path for path, _ in asked) == sorted(RECORDED_FILES)
    call = await off_the_loop(read_option_day_bars, CALL_600, TRADING_DATE)
    put = await off_the_loop(read_option_day_bars, PUT_600, TRADING_DATE)
    assert call is not None and put is not None
    # 389 and 400: the number of ``"t":`` stamps in each recorded file, which is also the ``resultsCount`` it states.
    assert (len(call.bars), len(put.bars)) == (389, 400)
    # The first and last bar of each, as the recorded file writes them: the start (ms UTC), the open, high, low and close, the
    # volume. Call: 09:30 = 1749562200000, and 16:13 = 1749562200000 + 403 x 60 000 = 1749586380000. Put: the same first,
    # and 16:14 = 1749586440000.
    assert call.bars[0] == OptionBar(1749562200000, D("4.08"), D("4.30"), D("4.06"), D("4.18"), D("1122"))
    assert call.bars[-1] == OptionBar(1749586380000, D("5.14"), D("5.14"), D("5.14"), D("5.14"), D("2"))
    assert put.bars[0] == OptionBar(1749562200000, D("3.41"), D("3.41"), D("3.17"), D("3.22"), D("149"))
    assert put.bars[-1] == OptionBar(1749586440000, D("2.36"), D("2.42"), D("2.36"), D("2.39"), D("77"))
    # The stored file itself, in LEAN's format: milliseconds after New York midnight, then four prices in ten-thousandths of
    # a dollar, then the volume. 09:30 is 9.5 h = 34 200 000 ms after midnight and 4.08 is 40 800; the call's last bar starts
    # at 16:13 = 58 380 000 ms and 5.14 is 51 400. (Summer time: 09:30 New York is 13:30 UTC, so a lake that counted New York
    # as UTC-5 all year would write 30 600 000 for the first bar and still read it back right; the file would be wrong.)
    rows = stored_rows(tmp_lake, stored.days[CALL_600])
    assert (rows[0], rows[-1]) == ("34200000,40800,43000,40600,41800,1122", "58380000,51400,51400,51400,51400,2")
    assert len(rows) == 389
    # And no bar anywhere differs from the recording, read here by the standard library alone: same stamps, same prices,
    # same volumes, same order.
    for day, name in ((call, RECORDED_FILES[CALL_600_PATH]), (put, RECORDED_FILES[PUT_600_PATH])):
        assert [(b.start_ms, b.open, b.high, b.low, b.close, b.volume) for b in day.bars] == [
            (row["t"], row["o"], row["h"], row["l"], row["c"], D(row["v"])) for row in recorded_rows(name)
        ]


async def test_a_contract_that_never_traded_is_stored_as_a_captured_empty_day_because_the_listing_holds_it(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    never_traded = stored.days[CALL_680]
    # The vendor said: status OK, the ticker, ``resultsCount: 0`` and no ``results`` key. The contract is in the listing
    # the lake holds for the date (``grep`` finds ``O:SPY250613C00680000`` once in it), so the lake pins "listed, did
    # not trade": a complete row with no bars.
    assert never_traded.failure is None and never_traded.record is not None
    assert never_traded.record.row_count == 0
    day = await off_the_loop(read_option_day_bars, CALL_680, TRADING_DATE)
    assert day is not None, "a day that never traded was captured; it is not a day that is missing"
    assert day.bars == ()


async def test_the_same_empty_answer_for_a_contract_the_listing_does_not_hold_is_refused_and_nothing_is_stored(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    rows_before = {key: dict(row) for key, row in fake_catalog.rows.items()}
    files_before = sorted(path.name for path in raw_root(tmp_lake).rglob("*") if path.is_file())
    # An empty answer shaped like the recorded one with only the contract's strike changed, for a strike never listed.
    empty_for_999 = recorded(RECORDED_FILES[CALL_680_PATH]).replace(b"C00680000", b"C00999000")
    path_999 = "/v2/aggs/ticker/O:SPY250613C00999000/range/1/minute/2025-06-10/2025-06-10"
    route = the_vendor.get(host="api.polygon.io", path=path_999).mock(
        return_value=httpx.Response(200, content=empty_for_999, headers={"content-type": "application/json"})
    )

    refused = await ensure_option_day_bars(CALL_999, TRADING_DATE, listed_under="SPY")

    assert refused.record is None and refused.failure is not None
    assert refused.failure.reason == "not_listed"
    assert not route.called, "the vendor was never asked: the listing refuses first"
    assert await off_the_loop(read_option_day_bars, CALL_999, TRADING_DATE) is None
    assert {key: dict(row) for key, row in fake_catalog.rows.items()} == rows_before
    assert sorted(path.name for path in raw_root(tmp_lake).rglob("*") if path.is_file()) == files_before


# ── b. The underlying, through the stock pipeline ──────────────────────────────────────────────────────────────


async def test_the_underlying_is_fetched_by_the_stock_pipeline_unadjusted_and_stored_as_the_vendor_priced_it(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    # One request for SPY's day, asking for raw bars, and the stock pipeline (an older code than the option fetchers) is
    # driven here for real: ``materialize_engine_run`` on the respx vendor, the suite's fake catalog and launcher.
    assert requests_to_the_vendor(the_vendor).count((STOCK_PATH, BARS_QUERY)) == 1
    assert stored.underlying.fetched_artifact_count > 0 and stored.underlying.incomplete_summary is None
    assert (raw_root(tmp_lake) / STORED_STOCK_FILE).is_file()
    held = await off_the_loop(LakeRunData("SPY").underlying_day, TRADING_DATE)
    assert held is not None
    # The regular session is 390 minutes and SPY traded in every one. Price for price, in time order, they are the 390
    # recorded bars whose stamp lies from 09:30 (1749562200000) up to but not including 16:00 (1749585600000).
    regular = [row for row in recorded_rows(RECORDED_FILES[STOCK_PATH]) if 1749562200000 <= row["t"] < 1749585600000]
    assert len(regular) == len(held.value) == 390
    assert [(b.open, b.high, b.low, b.close) for b in held.value] == [
        (row["o"], row["h"], row["l"], row["c"]) for row in regular
    ]


# ── c. Through the lake reader ─────────────────────────────────────────────────────────────────────────────────


async def test_the_reader_names_the_underlying_bar_the_vendor_stamped_0934_by_its_close_at_0935(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()

    held = await off_the_loop(LakeRunData("SPY").underlying_day, TRADING_DATE)

    assert held is not None
    by_close = {bar.close_ms: bar for bar in held.value}
    # The recorded bar stamped t = 1749562440000 (09:34): open 600.71, high 600.76, low 600.29, close 600.375. It
    # closes at 09:35 = 1749562500000 and is the bar the trade is decided on.
    assert by_close[1749562500000] == MinuteBar(
        close_ms=1749562500000, open=D("600.71"), high=D("600.76"), low=D("600.29"), close=D("600.375")
    )
    # The bar before it, stamped 09:33 (open 600.65, high 600.96, low 600.62, close 600.71),
    # closes at 09:34 = 1749562440000, which is the stamp of the bar above and not that bar's close.
    assert by_close[1749562440000] == MinuteBar(
        close_ms=1749562440000, open=D("600.65"), high=D("600.96"), low=D("600.62"), close=D("600.71")
    )
    # The first bar of the session starts at 09:30 and closes at 09:31 = 1749562260000; the last starts at 15:59
    # (1749585540000) and closes with the session at 1749585600000. Nothing closes at the open itself.
    assert (held.value[0].close_ms, held.value[-1].close_ms) == (1749562260000, 1749585600000)
    assert 1749562200000 not in by_close


async def test_the_reader_names_each_option_bar_by_its_close_and_gives_a_minute_without_a_trade_no_bar(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    data = LakeRunData("SPY")

    call = await off_the_loop(data.option_day, CALL_600, TRADING_DATE)
    put = await off_the_loop(data.option_day, PUT_600, TRADING_DATE)

    assert call is not None and put is not None
    assert (len(call.value), len(put.value)) == (389, 400)
    # The first call bar starts at 09:30 and closes at 09:31 (1749562260000); the last starts at 16:13 = 1749586380000
    # and closes at 16:14 = 1749586440000. For the put, the first closes at 1749562260000 and the last (it starts at
    # 16:14 = 1749586440000) closes at 16:15 = 1749586500000.
    assert call.value[0] == MinuteBar(
        close_ms=1749562260000, open=D("4.08"), high=D("4.30"), low=D("4.06"), close=D("4.18")
    )
    assert call.value[-1] == MinuteBar(
        close_ms=1749586440000, open=D("5.14"), high=D("5.14"), low=D("5.14"), close=D("5.14")
    )
    assert put.value[0] == MinuteBar(
        close_ms=1749562260000, open=D("3.41"), high=D("3.41"), low=D("3.17"), close=D("3.22")
    )
    assert put.value[-1] == MinuteBar(
        close_ms=1749586500000, open=D("2.36"), high=D("2.42"), low=D("2.36"), close=D("2.39")
    )
    # The two bars the trade below is read from: the call's bar stamped 09:35 closes at 1749562560000 (09:36), and the one
    # stamped 10:00 closes at 1749564060000 (10:01).
    by_close = {bar.close_ms: bar for bar in call.value}
    assert by_close[1749562560000] == MinuteBar(
        close_ms=1749562560000, open=D("4.05"), high=D("4.10"), low=D("4.02"), close=D("4.02")
    )
    assert by_close[1749564060000] == MinuteBar(
        close_ms=1749564060000, open=D("4.09"), high=D("4.18"), low=D("4.09"), close=D("4.18")
    )
    # The call has no bar stamped 11:28 or 11:29 (neither stamp is in the recorded file; the stamps either side, 11:27 and
    # 11:30, are): nothing traded in those two minutes. The bars closing 11:29 (1749569340000) and 11:30 (1749569400000)
    # do not exist, the one before closes at 11:28 (1749569280000) and the one after at 11:31 (1749569460000), and
    # nothing is carried across.
    closes = sorted(by_close)
    before = closes.index(1749569280000)
    assert closes[before : before + 2] == [1749569280000, 1749569460000]
    assert 1749569340000 not in by_close and 1749569400000 not in by_close


async def test_a_contract_that_never_traded_reads_as_captured_and_empty_and_never_as_missing(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    data = LakeRunData("SPY")

    never_traded = await off_the_loop(data.option_day, CALL_680, TRADING_DATE)
    never_asked = await off_the_loop(data.option_day, CALL_999, TRADING_DATE)

    assert never_traded is not None and never_traded.value == ()
    assert never_asked is None
    # Through the run core's market, an empty day is a contract that did not trade in any minute; a contract whose day was
    # never loaded is a bug to see and raises.
    listing = await off_the_loop(data.listing, TRADING_DATE)
    assert listing is not None
    market = DayMarket(TRADING_DATE, underlying=(), listing=listing.value, option_days={CALL_680: never_traded.value})
    assert market.option_bar(CALL_680, 1749562560000) is None
    with pytest.raises(ContractDayNotLoaded):
        market.option_bar(CALL_999, 1749562560000)


async def test_the_reader_gives_the_listing_whole_and_the_hash_of_each_file_it_read_the_same_twice(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    data = LakeRunData("SPY")

    async def read_all() -> tuple:
        return (
            await off_the_loop(data.underlying_day, TRADING_DATE),
            await off_the_loop(data.listing, TRADING_DATE),
            await off_the_loop(data.option_day, CALL_600, TRADING_DATE),
        )

    first, second = await read_all(), await read_all()

    underlying, listing, call = first
    assert underlying is not None and listing is not None and call is not None
    assert len(listing.value.contracts) == 316
    assert all(contract.is_pickable for contract in listing.value.contracts)
    # The hash is of the bytes of the stored file, which this test hashes itself.
    root = raw_root(tmp_lake)
    stored_files = (STORED_STOCK_FILE, STORED_LISTING_FILE, STORED_CALL_600_FILE)
    assert tuple(read.sha256 for read in first) == tuple(
        hashlib.sha256((root / path).read_bytes()).hexdigest() for path in stored_files
    )
    assert tuple(read.sha256 for read in second) == tuple(read.sha256 for read in first)


# ── d. Through the run core ────────────────────────────────────────────────────────────────────────────────────

# The setup of the main trade: buy one at-the-money call expiring this week at 09:35, sell it by the clock at 10:00,
# with no stop, no momentum, no re-entry, no slippage and no fee, on 10 000.00 of cash. 09:35 is 5 minutes after the open
# (300 000 ms) and 10:00 is 30 minutes after it (1 800 000 ms).
ENTRY_0935_MS = 300_000
EXIT_1000_MS = 1_800_000
# The other times the scenes below use, as minutes after 09:30 times 60 000: 09:37 is 7, 09:58 is 28, 11:28 is 118
# (1 h 58 min) and 12:00 is 150.
ENTRY_0937_MS = 420_000
ENTRY_0958_MS = 1_680_000
TIME_1128_MS = 7_080_000
EXIT_1200_MS = 9_000_000


def a_setup(
    *,
    right: OptionRight = "call",
    strike: StrikeRule | None = None,
    entry_ms_after_open: int = ENTRY_0935_MS,
    exit_ms_after_open: int = EXIT_1000_MS,
) -> OptionsBacktestSetup:
    return OptionsBacktestSetup(
        underlying="SPY",
        entry=SessionTime(anchor="open", offset_ms=entry_ms_after_open),
        exit=SessionTime(anchor="open", offset_ms=exit_ms_after_open),
        square_off="partial",
        legs=(
            LegSetup(
                leg_id="leg",
                right=right,
                contracts=1,
                expiry=ThisWeekExpiryRule(kind="this_week"),
                strike=strike if strike is not None else AtTheMoney(kind="atm"),
            ),
        ),
        slippage_percent=D(0),
        fees=FeePolicy(kind="not_charged"),
        starting_cash_usd=D("10000.00"),
    )


def sale_of(trade: LegTrade) -> Fill:
    """The sale a trade left by (a trade left at the session's close is marked, not sold)."""
    assert isinstance(trade.exit, Fill), "the leg was sold, not marked"
    return trade.exit


async def the_run(setup: OptionsBacktestSetup) -> RunResult:
    """The run core over the one session, reading the lake through ``LakeRunData``, on a worker thread as a run is."""
    return await asyncio.to_thread(run_sessions, setup, [TRADING_DATE], LakeRunData("SPY"), slippage_ladder=(D(0),))


async def test_one_at_the_money_call_is_bought_at_0936s_open_and_sold_at_1001s_open_for_exactly_the_hand_worked_profit(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # THE TRADE, WORKED BY HAND FROM THE RECORDED BYTES.
    #
    # Decision at 09:35. The bar closing at 09:35 is the one stamped 09:34 (t = 1749562440000): close 600.375. The listed
    # call strikes near it are ... 599, 600, 601 ...; 600.375 is 0.375 from 600 and 0.625 from 601, so the money is 600.
    # The listing holds one expiry, Friday 2025-06-13, the last session of its week, so "this week" is that one:
    # the contract is O:SPY250613C00600000. The order is decided at 1749562500000.
    #
    # Entry fill. The order fills at the OPEN of the next bar, the one closing at 09:36 (1749562560000), stamped 09:35
    # (t = 1749562500000) in the recorded call file: {"o":4.05,"h":4.1,"l":4.02,"c":4.02}. Open 4.05. (The bar the order
    # was decided on, stamped 09:34, opens at 4.24 and the fill bar closes at 4.02: neither is the fill.) With no slippage
    # and a price step of 0.01, 4.05 is already on the step: the buy fills at 4.05. It costs 4.05 x 100 x 1 = 405.00,
    # leaving 10000.00 - 405.00 = 9595.00.
    #
    # Exit. The exit time is 10:00 (1749564000000). The order is decided there and sells at the OPEN of the bar closing
    # at 10:01 (1749564060000), stamped 10:00 (t = 1749564000000): {"o":4.09,"h":4.18,"l":4.09,"c":4.18}. Open 4.09.
    # (The bar closing at 10:00, stamped 09:59, opens at 4.11; the exit bar closes at 4.18: neither is the fill.) The
    # sale fills at 4.09 and brings in 4.09 x 100 x 1 = 409.00: cash 9595.00 + 409.00 = 10004.00.
    #
    # Profit = (4.09 - 4.05) x 100 x 1 = 0.04 x 100 = 4.00.
    await capture_the_recording()

    result = await the_run(a_setup())

    [session] = result.sessions
    assert session.status is SessionStatus.TRADED
    [trade] = session.outcome.trades
    assert trade.contract == CALL_600
    assert (trade.entry_decided_at_ms, trade.entry.bar_close_ms) == (1749562500000, 1749562560000)
    assert (trade.entry.side, trade.entry.seen, trade.entry.filled) == ("buy", D("4.05"), D("4.05"))
    assert trade.exit_reason is ExitReason.EXIT_TIME and trade.exit_decided_at_ms == 1749564000000
    assert sale_of(trade).side == "sell" and sale_of(trade).bar_close_ms == 1749564060000
    assert (sale_of(trade).seen, sale_of(trade).filled) == (D("4.09"), D("4.09"))
    assert (trade.contracts, trade.multiplier) == (1, 100)
    assert trade.pnl_usd == D("4.00")
    assert (session.cash_before_usd, session.cash_after_usd) == (D("10000.00"), D("10004.00"))
    assert (session.outcome.cash_change_usd, session.outcome.pnl_usd) == (D("4.00"), D("4.00"))
    assert (result.summary.net_profit_usd, result.summary.ending_cash_usd) == (D("4.00"), D("10004.00"))
    # What was read is what was stored: each pin is the sha256 of the stored file's bytes, hashed here.
    root = raw_root(tmp_lake)
    pinned = {pin.contract: pin.sha256 for pin in session.pins.option_days}
    assert pinned == {CALL_600: hashlib.sha256((root / STORED_CALL_600_FILE).read_bytes()).hexdigest()}
    assert session.pins.listing_sha256 == hashlib.sha256((root / STORED_LISTING_FILE).read_bytes()).hexdigest()
    assert session.pins.underlying_sha256 == hashlib.sha256((root / STORED_STOCK_FILE).read_bytes()).hexdigest()


async def test_one_at_the_money_put_is_bought_and_sold_at_the_same_minutes_for_exactly_the_hand_worked_loss(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # The same decision at 09:35 (the underlying closes at 600.375, the money is 600) for a put: O:SPY250613P00600000.
    # Entry: the put bar stamped 09:35 (closing 09:36) is {"o":3.37,"h":3.4,"l":3.34,"c":3.37}: bought at 3.37, costing
    # 337.00. Exit: the put bar stamped 10:00 (closing 10:01) is {"o":3.15,"h":3.2,"l":3.15,"c":3.2}: sold at 3.15, bringing
    # in 315.00. Cash 10000.00 - 337.00 + 315.00 = 9978.00; profit = (3.15 - 3.37) x 100 = -22.00.
    await capture_the_recording()

    result = await the_run(a_setup(right="put"))

    [session] = result.sessions
    [trade] = session.outcome.trades
    assert trade.contract == PUT_600
    assert (trade.entry.seen, trade.entry.filled, sale_of(trade).filled) == (D("3.37"), D("3.37"), D("3.15"))
    assert trade.pnl_usd == D("-22.00")
    assert session.cash_after_usd == D("9978.00")


async def test_an_exit_whose_next_minute_has_no_bar_sells_at_the_open_of_the_next_minute_that_has_one(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Entry as in the main trade: bought at 4.05 (cost 405.00). Exit time 11:28 = 118 minutes after the open =
    # 7 080 000 ms, decided at 1749569280000. The sale needs the bar closing 11:29 (stamped 11:28): the recorded call file
    # has no such bar. It tries again at the bar closing 11:30 (stamped 11:29): none either. The bar closing at 11:31
    # (1749569460000) is stamped 11:30 (t = 1749569400000): {"o":4.4,"h":4.4,"l":4.3,"c":4.3}. The sale fills at its
    # open, 4.40, not at the last price seen before the gap (the bar stamped 11:27 closes at 4.44) and not at any
    # price in between: 4.40 x 100 = 440.00, so the cash is 9595.00 + 440.00 = 10035.00 and the profit is
    # (4.40 - 4.05) x 100 = 35.00.
    await capture_the_recording()

    result = await the_run(a_setup(exit_ms_after_open=TIME_1128_MS))

    [session] = result.sessions
    [trade] = session.outcome.trades
    assert trade.exit_decided_at_ms == 1749569280000
    assert sale_of(trade).bar_close_ms == 1749569460000
    assert (sale_of(trade).seen, sale_of(trade).filled) == (D("4.40"), D("4.40"))
    assert trade.pnl_usd == D("35.00")
    assert session.cash_after_usd == D("10035.00")


async def test_one_strike_below_the_money_is_counted_from_the_strike_nearest_the_price_and_not_from_the_one_below_it(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Entry at 09:37 (7 minutes after the open, 420 000 ms), decided at the bar closing 09:37 (1749562620000), stamped
    # 09:36: SPY closes at 600.59. That is 0.41 below 601 and 0.59 above 600, so the money is 601, and one strike below it
    # is 600: O:SPY250613C00600000. (The strike just below the price is 600 too, so counting down from it would give 599,
    # which was not recorded.) The order fills at the open of the bar closing 09:38 (1749562680000), stamped 09:37:
    # {"o":4.19,"h":4.26,"l":4.15,"c":4.18}: bought at 4.19, costing 419.00. It sells at 10:00 as in the main trade, at
    # 4.09, bringing in 409.00. Profit (4.09 - 4.19) x 100 = -10.00; cash 10000.00 - 419.00 + 409.00 = 9990.00.
    await capture_the_recording()

    result = await the_run(a_setup(strike=StepsFromTheMoney(kind="atm_offset", steps=-1), entry_ms_after_open=ENTRY_0937_MS))

    [session] = result.sessions
    [trade] = session.outcome.trades
    assert trade.contract == CALL_600
    assert (trade.entry_decided_at_ms, trade.entry.bar_close_ms) == (1749562620000, 1749562680000)
    assert (trade.entry.filled, sale_of(trade).filled) == (D("4.19"), D("4.09"))
    assert trade.pnl_usd == D("-10.00")
    assert session.cash_after_usd == D("9990.00")


async def test_a_price_exactly_between_two_strikes_picks_the_lower_one(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Entry at 09:58 (28 minutes after the open, 1 680 000 ms), decided at the bar closing 09:58 (1749563880000), stamped
    # 09:57: SPY {"o":600.51,"h":600.57,"l":600.33,"c":600.5}. 600.50 is 0.50 from 600 and 0.50 from 601, so the two are
    # as near and the lower is the money: O:SPY250613C00600000. The order fills at the open of the bar closing 09:59
    # (1749563940000), stamped 09:58: {"o":4.07,"h":4.14,"l":4.07,"c":4.11}: bought at 4.07. It sells at 10:00 as in the
    # main trade, at 4.09. Profit (4.09 - 4.07) x 100 = 2.00; cash 10000.00 - 407.00 + 409.00 = 10002.00.
    await capture_the_recording()

    result = await the_run(a_setup(entry_ms_after_open=ENTRY_0958_MS))

    [session] = result.sessions
    [trade] = session.outcome.trades
    assert trade.contract == CALL_600
    assert (trade.entry_decided_at_ms, trade.entry.bar_close_ms) == (1749563880000, 1749563940000)
    assert (trade.entry.filled, sale_of(trade).filled) == (D("4.07"), D("4.09"))
    assert trade.pnl_usd == D("2.00")
    assert session.cash_after_usd == D("10002.00")


# ── e. A wanted contract with nothing to fill from ────────────────────────────────────────────────────────────


async def test_an_entry_into_a_minute_the_contract_did_not_trade_is_dropped_with_the_reason_recorded_and_no_money_moves(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # Why not the call struck at 680, which the recording holds as never having traded: it is 56 strikes above the
    # money (the listed call strikes after 600 are 601 ... 650 one dollar apart, then 655 ... 680 five apart: 50 + 6),
    # and a leg may step at most 50 strikes from the money, so no strike rule reaches it. The recording has another real
    # case of a wanted contract with nothing to fill from: the call struck at 600 has no bar stamped 11:28.
    #
    # Entry time 11:28 = 118 minutes after the open = 7 080 000 ms, so the order is decided at the bar closing 11:28
    # (1749569280000), stamped 11:27: SPY closes {"c":601.26}. The money is 601 (0.26 from 601, 0.74 from 602), and the leg
    # asks for one strike below the money (steps -1): 600, so the contract is O:SPY250613C00600000. The order fills in the
    # bar closing at 11:29 (1749569340000), stamped 11:28, which the recorded call file does not have. The entry is
    # dropped; the leg does not try again (a first entry is not a re-entry). Exit at 12:00 (9 000 000 ms) finds nothing
    # held. No trade, no cash moves: 10000.00 before and after.
    await capture_the_recording()

    result = await the_run(
        a_setup(
            strike=StepsFromTheMoney(kind="atm_offset", steps=-1),
            entry_ms_after_open=TIME_1128_MS,
            exit_ms_after_open=EXIT_1200_MS,
        )
    )

    [session] = result.sessions
    assert session.status is SessionStatus.NO_TRADE
    assert session.outcome.trades == ()
    assert session.outcome.events == (
        Event(EventKind.PICKED, 1749569280000, "leg", 0, CALL_600),
        Event(EventKind.ENTRY_DROPPED, 1749569340000, "leg", 0),
    )
    assert session.outcome.cash_change_usd == 0
    assert (session.cash_before_usd, session.cash_after_usd) == (D("10000.00"), D("10000.00"))
    assert result.summary.entries_dropped == 1 and result.summary.net_profit_usd == D("0")
