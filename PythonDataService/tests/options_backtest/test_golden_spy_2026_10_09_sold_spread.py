"""The vendor's real answers for SPY on Friday 2026-10-09, through the lake and the run core, to a sold call spread worked by hand (#3110).

The first golden fixture of this feature (``test_golden_spy_2025_06_10.py``) proves bought legs on real data. This one proves
a sold leg and its cover. The four answers the vendor really sent (recorded on 2026-10-13, byte for byte, in
``tests/fixtures/golden/options-backtest/options-backtest-spy-2026-10-09-sold-spread/v1``) are served by ``respx`` to the
lake's real fetchers, stored in a lake in ``tmp_path``, read back by ``LakeRunData`` and replayed by ``run_sessions`` into
the fills, fees, cash and collateral of a call credit spread that a person works out with a pencil from the same bars.
Nothing here touches the network or a database.

How the expectations were made. Every figure below was read off the recorded files (the standard library's ``json``
printing a bar) and worked out by hand from the rules of ADR 0078 decision 4 and of #3095; none was taken from the code
under test. Money is ``Decimal`` and compared exactly, because this is exact arithmetic on recorded prices and a
tolerance would only hide a wrong minute.

Times (all New York, EDT = UTC-4 in October, so 09:30 is 13:30 UTC):

* 2026-10-09 is a Friday and a full session. It opens at 1791552600000 ms (the day count from 1970-01-01 is 20 735, so
  20 735 x 86 400 s + 13 h 30 min = 1 791 552 600 s) and closes 390 minutes later, at 20:00 UTC = 1791552600000 +
  390 x 60 000 = 1791576000000. The contracts expire that same day, at 16:00 New York.
* The vendor stamps a bar with its START. The bar that closes at 09:35 starts at 09:34 = 1791552600000 + 4 x 60 000 =
  1791552840000 and closes at 1791552900000. The bar the entry fills in starts at 09:35 (1791552900000) and closes at 09:36
  (1791552960000).

The recorded files, counted as the number of objects in the ``results`` array of each body and checked against the
``resultsCount`` each body states: SPY 844 bars (390 of them in the regular session, one for every minute), the listing
366 contracts (183 calls, 183 puts, every one American, 100 shares, expiring 2026-10-09), ``O:SPY261009C00777000`` 400
bars and ``O:SPY261009C00779000`` 400 bars. Neither call has a bar stamped 09:32, 09:36, 09:39, 09:43 or 09:46.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.data_lake.option_bars import OptionBar, ensure_option_day_bars, read_option_day_bars
from app.data_lake.option_lake import OptionCaptureResult
from app.data_lake.option_listing import ensure_option_listing, read_option_listing
from app.data_lake.run_materialization import EngineRunMaterialization, materialize_engine_run
from app.options.contract import OptionContract
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.market import MinuteBar
from app.options_backtest.results import EventKind, ExitReason, Fill, FillGroup, LegTrade, SessionStatus
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.session_result import SessionResult
from app.options_backtest.setup import (
    AtTheMoney,
    FeePolicy,
    LegSetup,
    OptionsBacktestSetup,
    SameDayExpiryRule,
    SessionTime,
    StepsFromTheMoney,
    StopLoss,
)
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._golden_vendor import THE_KEY, recorded_vendor, requests_to_the_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest.test_golden_spy_2025_06_10 import catalog_row, off_the_loop, sale_of, stored_rows

D = Decimal

RECORDING = (
    Path(__file__).parents[1] / "fixtures/golden/options-backtest/options-backtest-spy-2026-10-09-sold-spread/v1"
)
TRADING_DATE = date(2026, 10, 9)
EXPIRY = TRADING_DATE

SOLD_CALL = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=777)
BOUGHT_CALL = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=779)

# The request path of each recorded answer, and the file that holds the bytes the vendor answered with.
LISTING_PATH = "/v3/reference/options/contracts"
STOCK_PATH = "/v2/aggs/ticker/SPY/range/1/minute/2026-10-09/2026-10-09"
SOLD_CALL_PATH = "/v2/aggs/ticker/O:SPY261009C00777000/range/1/minute/2026-10-09/2026-10-09"
BOUGHT_CALL_PATH = "/v2/aggs/ticker/O:SPY261009C00779000/range/1/minute/2026-10-09/2026-10-09"
RECORDED_FILES = {
    LISTING_PATH: "spy_contracts_as_of_2026-10-09_expiring_2026-10-09.json",
    STOCK_PATH: "spy_minute_2026-10-09.json",
    SOLD_CALL_PATH: "O_SPY261009C00777000_minute_2026-10-09.json",
    BOUGHT_CALL_PATH: "O_SPY261009C00779000_minute_2026-10-09.json",
}
# What the recording asked, and so what the lake's fetchers must ask for the recording to speak for them. The listing
# differs on purpose: it was recorded with ``expiration_date=2026-10-09`` added to keep it to one page, and the lake asks
# for the whole listing as of the date.
BARS_QUERY = {"adjusted": "false", "sort": "asc", "limit": "50000"}
LISTING_QUERY = {"underlying_ticker": "SPY", "as_of": "2026-10-09", "limit": "1000"}

STORED_STOCK_FILE = "equity/usa/minute/spy/20261009_trade.zip"
STORED_LISTING_FILE = "option/usa/listing/spy/20261009_contracts.zip"
STORED_SOLD_CALL_FILE = "option/usa/minute/spy/spy261009c00777000/20261009_trade.zip"
STORED_BOUGHT_CALL_FILE = "option/usa/minute/spy/spy261009c00779000/20261009_trade.zip"

# The setup: a call credit spread of two contracts, entered at 09:35 (5 minutes after the open) and left by the clock at
# 15:30 (30 minutes before the close), on 10 000.00 of cash. 0.3 percent of slippage (the page's own default) and 0.65 a
# contract in fees, so that both move the money of every fill.
ENTRY_0935_MS = 300_000
EXIT_1530_BEFORE_THE_CLOSE_MS = 1_800_000
SLIPPAGE = D("0.3")
FEE_PER_CONTRACT = D("0.65")
STARTING_CASH = D("10000.00")

# The instants the scenes use (ms UTC), each worked out in the scenes' comments below.
DECIDED_ENTRY_MS = 1791552900000
ENTRY_FILL_MS = 1791552960000
STOP_DECIDED_MS = 1791559140000
STOP_FILL_MS = 1791559200000
EXIT_DECIDED_MS = 1791574200000
EXIT_FILL_MS = 1791574260000

# The fixture's answer key, ``expected.json``: the figures of each scene below, worked by hand from the recorded bytes
# and written down once more beside the recording. The scenes assert the same figures as literals; this check keeps the
# file the fixture registers as its output true to the run.
ANSWER_KEY = json.loads((RECORDING / "expected.json").read_bytes())["scenes"]


def recorded(name: str) -> bytes:
    return (RECORDING / name).read_bytes()


def recorded_rows(name: str) -> list[dict]:
    """The rows of a recorded body as the standard library reads them: the one oracle here that is not our code."""
    body = json.loads(recorded(name), parse_float=D)
    return body["results"]


@pytest.fixture
def the_vendor() -> Iterator[respx.MockRouter]:
    """The vendor, answering each recorded request with the recorded bytes. A request for any other path fails.

    Every option request must carry the key in the ``Authorization`` header and in no URL (``_golden_vendor``).
    """
    with recorded_vendor(RECORDING, RECORDED_FILES, STOCK_PATH) as router:
        yield router


@dataclass(frozen=True)
class Stored:
    """What capturing the recording left in the lake, and what each capture said."""

    listing: OptionCaptureResult
    days: dict[OptionContract, OptionCaptureResult]
    underlying: EngineRunMaterialization


async def capture_the_recording() -> Stored:
    """The listing, the two contracts' day bars and the underlying's day, each through the lake's own function.

    The listing goes first because a contract's empty day is accepted only for a contract the stored listing holds.
    The underlying goes through ``materialize_engine_run``, the stock engine's function, on a worker thread.
    """
    listing = await ensure_option_listing("SPY", TRADING_DATE)
    days = {
        contract: await ensure_option_day_bars(contract, TRADING_DATE, listed_under="SPY")
        for contract in (SOLD_CALL, BOUGHT_CALL)
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


# ── a. Through the lake's real fetchers ─────────────────────────────────────────────────────────────────────────


async def test_the_listing_is_captured_whole_by_the_lakes_own_function_and_asked_for_as_the_recording_asked(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await ensure_option_listing("SPY", TRADING_DATE)

    assert result.failure is None, result.failure
    assert requests_to_the_vendor(the_vendor, STOCK_PATH) == [(LISTING_PATH, LISTING_QUERY)]
    listing = await off_the_loop(read_option_listing, "SPY", TRADING_DATE)
    assert listing is not None
    # 366 objects in the recorded body's ``results``, 183 of them calls and 183 puts, all expiring on the day itself.
    assert len(listing.rows) == 366
    assert sum(row.contract_type == "call" for row in listing.rows) == 183
    assert sum(row.contract_type == "put" for row in listing.rows) == 183
    assert all(row.standard for row in listing.rows)
    assert {row.expiry_ms for row in listing.rows} == {1791576000000}
    # The rows of the two contracts of the spread: strike 777 is 777 000 thousandths, the expiry is 2026-10-09 16:00 New York.
    assert SOLD_CALL.expiry_ms == 1791576000000 and SOLD_CALL.occ_symbol == "SPY261009C00777000"
    [sold] = [row for row in listing.rows if row.ticker == "O:SPY261009C00777000"]
    [bought] = [row for row in listing.rows if row.ticker == "O:SPY261009C00779000"]
    assert (sold.contract_type, sold.strike_thousandths) == ("call", 777000)
    assert (bought.contract_type, bought.strike_thousandths) == ("call", 779000)
    assert (sold.exercise_style, sold.shares_per_contract) == ("american", 100)


async def test_each_contract_day_is_stored_with_exactly_the_bars_the_vendor_sent_and_every_request_carried_the_key_in_a_header(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    assert [stored.listing.failure, *(each.failure for each in stored.days.values())] == [None] * 3
    asked = requests_to_the_vendor(the_vendor, STOCK_PATH)
    for path in (SOLD_CALL_PATH, BOUGHT_CALL_PATH):
        assert (path, BARS_QUERY) in asked
    # Four answers were recorded and four requests were made, one for each: the listing, the two contracts and the
    # underlying's day. Nothing else was asked of the vendor.
    assert sorted(path for path, _ in asked) == sorted(RECORDED_FILES)
    # The key went in an ``Authorization: Bearer`` header and in no URL, for the listing and for both contracts. (The stock
    # pipeline's request for the underlying puts it in the URL: that older fetcher is known and not part of this fixture.)
    option_requests = [
        call.request
        for call in the_vendor.calls
        if call.request.url.host == "api.polygon.io" and call.request.url.path != STOCK_PATH
    ]
    assert len(option_requests) == 3
    assert all(request.headers["authorization"] == f"Bearer {THE_KEY}" for request in option_requests)
    assert not any(THE_KEY in str(request.url) for request in option_requests)
    sold = await off_the_loop(read_option_day_bars, SOLD_CALL, TRADING_DATE)
    bought = await off_the_loop(read_option_day_bars, BOUGHT_CALL, TRADING_DATE)
    assert sold is not None and bought is not None
    # 400 and 400: the number of ``"t":`` stamps in each recorded file, which is also the ``resultsCount`` it states.
    assert (len(sold.bars), len(bought.bars)) == (400, 400)
    # The first and last bar of each, as the recorded file writes them: the start (ms UTC), the open, high, low and close,
    # the volume. Both start at 09:30 = 1791552600000 and end with the bar stamped 16:14 = 1791552600000 + 404 x 60 000 =
    # 1791576840000.
    assert sold.bars[0] == OptionBar(1791552600000, D("0.99"), D("1.07"), D("0.90"), D("0.98"), D("2886"))
    assert sold.bars[-1] == OptionBar(1791576840000, D("1.59"), D("1.65"), D("1.59"), D("1.62"), D("32"))
    assert bought.bars[0] == OptionBar(1791552600000, D("0.36"), D("0.40"), D("0.32"), D("0.36"), D("1463"))
    assert bought.bars[-1] == OptionBar(1791576840000, D("0.04"), D("0.05"), D("0.04"), D("0.04"), D("485"))
    # The stored file itself, in LEAN's format: milliseconds after New York midnight, then four prices in ten-thousandths
    # of a dollar, then the volume. 09:30 is 9.5 h = 34 200 000 ms after midnight and 0.99 is 9 900; the last bar starts at
    # 16:14 = 58 440 000 ms and 1.59 is 15 900. (Summer time: 09:30 New York is 13:30 UTC, so a lake that counted New York
    # as UTC-5 all year would write 30 600 000 for the first bar and still read it back right; the file would be wrong.)
    rows = stored_rows(tmp_lake, stored.days[SOLD_CALL])
    assert (rows[0], rows[-1]) == ("34200000,9900,10700,9000,9800,2886", "58440000,15900,16500,15900,16200,32")
    assert len(rows) == 400
    # And no bar anywhere differs from the recording, read here by the standard library alone: same stamps, same prices,
    # same volumes, same order.
    for day, name in ((sold, RECORDED_FILES[SOLD_CALL_PATH]), (bought, RECORDED_FILES[BOUGHT_CALL_PATH])):
        assert [(b.start_ms, b.open, b.high, b.low, b.close, b.volume) for b in day.bars] == [
            (row["t"], row["o"], row["h"], row["l"], row["c"], D(row["v"])) for row in recorded_rows(name)
        ]


async def test_the_catalog_row_of_each_stored_day_counts_its_bars_and_dates_the_first_and_the_last(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    # Each recorded body holds 400 objects in its ``results``, the first stamped 09:30 (1791552600000) and the last 16:14
    # (1791576840000). The capture returns a record and writes a catalog row, and both must say so.
    for contract, file_path in ((SOLD_CALL, STORED_SOLD_CALL_FILE), (BOUGHT_CALL, STORED_BOUGHT_CALL_FILE)):
        record = stored.days[contract].record
        assert record is not None
        row = catalog_row(fake_catalog, file_path)
        assert (record.row_count, record.first_bar_start_ms, record.last_bar_start_ms) == (
            400,
            1791552600000,
            1791576840000,
        )
        assert (row["row_count"], row["first_bar_start_ms"], row["last_bar_start_ms"]) == (
            400,
            1791552600000,
            1791576840000,
        )


# ── b. The underlying, through the stock pipeline ──────────────────────────────────────────────────────────────


async def test_the_underlying_is_fetched_by_the_stock_pipeline_unadjusted_and_stored_as_the_vendor_priced_it(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    stored = await capture_the_recording()

    # One request for SPY's day, asking for raw bars. The stock pipeline (an older code than the option fetchers) is
    # driven here for real. Its answer holds fractional volume in the early hours (the first bar's ``v`` is 5967.495636),
    # as the vendor has sent since 2026-02-23.
    assert requests_to_the_vendor(the_vendor, STOCK_PATH).count((STOCK_PATH, BARS_QUERY)) == 1
    assert stored.underlying.fetched_artifact_count > 0 and stored.underlying.incomplete_summary is None
    assert (raw_root(tmp_lake) / STORED_STOCK_FILE).is_file()
    held = await off_the_loop(LakeRunData("SPY").underlying_day, TRADING_DATE)
    assert held is not None
    # The regular session is 390 minutes and SPY traded in every one. Price for price, in time order, they are the 390
    # recorded bars whose stamp lies from 09:30 (1791552600000) up to but not including 16:00 (1791576000000).
    regular = [row for row in recorded_rows(RECORDED_FILES[STOCK_PATH]) if 1791552600000 <= row["t"] < 1791576000000]
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
    # The recorded bar stamped t = 1791552840000 (09:34): open 776.57, high 776.77, low 776.54, close 776.55. It closes at
    # 09:35 = 1791552900000 and is the bar the entry is decided on.
    assert by_close[DECIDED_ENTRY_MS] == MinuteBar(
        close_ms=DECIDED_ENTRY_MS, open=D("776.57"), high=D("776.77"), low=D("776.54"), close=D("776.55")
    )
    # The first bar of the session starts at 09:30 and closes at 09:31 = 1791552660000; the last starts at 15:59
    # (1791575940000) and closes with the session at 1791576000000. Nothing closes at the open itself.
    assert (held.value[0].close_ms, held.value[-1].close_ms) == (1791552660000, 1791576000000)
    assert 1791552600000 not in by_close


async def test_the_reader_names_each_option_bar_by_its_close_and_gives_a_minute_without_a_trade_no_bar(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    data = LakeRunData("SPY")

    sold = await off_the_loop(data.option_day, SOLD_CALL, TRADING_DATE)
    bought = await off_the_loop(data.option_day, BOUGHT_CALL, TRADING_DATE)

    assert sold is not None and bought is not None
    assert (len(sold.value), len(bought.value)) == (400, 400)
    sold_by_close = {bar.close_ms: bar for bar in sold.value}
    bought_by_close = {bar.close_ms: bar for bar in bought.value}
    # The four bars of the sold call the scenes are read from, each named by its close. Stamped 09:35, it closes at 09:36
    # (1791552960000) and the entry fills at its open. Stamped 11:18 it closes at 11:19 (1791559140000): the close 1.20
    # that reaches the stop. Stamped 11:19 it closes at 11:20 (1791559200000) and the buy-back fills at its open. Stamped
    # 15:30 it closes at 15:31 (1791574260000) and the exit fills at its open.
    assert sold_by_close[ENTRY_FILL_MS] == MinuteBar(
        close_ms=ENTRY_FILL_MS, open=D("1.00"), high=D("1.07"), low=D("0.94"), close=D("0.98")
    )
    assert sold_by_close[STOP_DECIDED_MS] == MinuteBar(
        close_ms=STOP_DECIDED_MS, open=D("1.10"), high=D("1.26"), low=D("1.09"), close=D("1.20")
    )
    assert sold_by_close[STOP_FILL_MS] == MinuteBar(
        close_ms=STOP_FILL_MS, open=D("1.19"), high=D("1.28"), low=D("1.14"), close=D("1.15")
    )
    assert sold_by_close[EXIT_FILL_MS] == MinuteBar(
        close_ms=EXIT_FILL_MS, open=D("1.90"), high=D("1.92"), low=D("1.84"), close=D("1.88")
    )
    # The bought call's two fill bars, the same minutes.
    assert bought_by_close[ENTRY_FILL_MS] == MinuteBar(
        close_ms=ENTRY_FILL_MS, open=D("0.35"), high=D("0.38"), low=D("0.33"), close=D("0.33")
    )
    assert bought_by_close[EXIT_FILL_MS] == MinuteBar(
        close_ms=EXIT_FILL_MS, open=D("0.32"), high=D("0.32"), low=D("0.28"), close=D("0.31")
    )
    # Neither call has a bar stamped 09:32 or 09:36 (neither stamp is in the recorded files; the stamps either side are):
    # nothing traded in those minutes. The bars closing at 09:33 (1791552780000) and 09:37 (1791553020000) do not
    # exist, and nothing is carried across.
    for by_close in (sold_by_close, bought_by_close):
        assert 1791552780000 not in by_close and 1791553020000 not in by_close
        assert 1791552720000 in by_close and 1791552960000 in by_close and 1791553080000 in by_close


async def test_the_reader_gives_the_listing_whole_and_the_hash_of_each_file_it_read_the_same_twice(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    await capture_the_recording()
    data = LakeRunData("SPY")

    async def read_all() -> tuple:
        return (
            await off_the_loop(data.underlying_day, TRADING_DATE),
            await off_the_loop(data.listing, TRADING_DATE),
            await off_the_loop(data.option_day, SOLD_CALL, TRADING_DATE),
        )

    first, second = await read_all(), await read_all()

    underlying, listing, sold = first
    assert underlying is not None and listing is not None and sold is not None
    assert len(listing.value.contracts) == 366
    assert all(contract.is_pickable for contract in listing.value.contracts)
    # The hash is of the bytes of the stored file, which this test hashes itself.
    root = raw_root(tmp_lake)
    stored_files = (STORED_STOCK_FILE, STORED_LISTING_FILE, STORED_SOLD_CALL_FILE)
    assert tuple(read.sha256 for read in first) == tuple(
        hashlib.sha256((root / path).read_bytes()).hexdigest() for path in stored_files
    )
    assert tuple(read.sha256 for read in second) == tuple(read.sha256 for read in first)


# ── d. Through the run core ────────────────────────────────────────────────────────────────────────────────────


def a_call_credit_spread(*, stop_percent: str | None = None) -> OptionsBacktestSetup:
    """Sell two at-the-money calls for today's expiry and buy two calls two listed strikes above them as their cover.

    ``stop_percent`` puts a stop loss of that many percent on the sold leg alone.
    """
    expiry = SameDayExpiryRule(kind="same_day")
    stop = None if stop_percent is None else StopLoss(kind="percent", value=D(stop_percent))
    return OptionsBacktestSetup(
        underlying="SPY",
        entry=SessionTime(anchor="open", offset_ms=ENTRY_0935_MS),
        exit=SessionTime(anchor="close", offset_ms=EXIT_1530_BEFORE_THE_CLOSE_MS),
        square_off="partial",
        legs=(
            LegSetup(
                leg_id="short_call",
                side="sell",
                right="call",
                contracts=2,
                expiry=expiry,
                strike=AtTheMoney(kind="atm"),
                stop_loss=stop,
            ),
            LegSetup(
                leg_id="long_call",
                right="call",
                contracts=2,
                expiry=expiry,
                strike=StepsFromTheMoney(kind="atm_offset", steps=2),
            ),
        ),
        slippage_percent=SLIPPAGE,
        fees=FeePolicy(kind="per_contract", per_contract_usd=FEE_PER_CONTRACT),
        starting_cash_usd=STARTING_CASH,
    )


async def the_run(setup: OptionsBacktestSetup) -> RunResult:
    """The run core over the one session, reading the lake through ``LakeRunData``, on a worker thread as a run is.

    One slippage and one cash level, the setup's own, so that only the run itself is replayed.
    """
    return await asyncio.to_thread(
        run_sessions, setup, [TRADING_DATE], LakeRunData("SPY"), slippage_ladder=(SLIPPAGE,), cash_ladder=(D(1),)
    )


def what_a_group_did(session: SessionResult, at_ms: int) -> dict[str, Any]:
    """The fill group that filled at ``at_ms``, in the shape of the answer key: its fills, fees, cash and collateral.

    The fills and their fees are the trades' own (an entry, or a close that is a fill); the cash and the collateral
    before and after are the replay's money step for the group; the profit is that of the trades the group closed.
    """
    [step] = [step for step in session.outcome.money_steps if step.at_ms == at_ms]
    fills: list[dict[str, Any]] = []
    closed = D(0)
    fees = D(0)
    for trade in session.outcome.trades:
        for fill, fee in ((trade.entry, trade.entry_fee_usd), (trade.exit, trade.exit_fee_usd)):
            if isinstance(fill, Fill) and fill.bar_close_ms == at_ms:
                fills.append(
                    {
                        "leg": fill.leg_id,
                        "contract": f"O:{fill.contract.occ_symbol}",
                        "side": fill.side,
                        "contracts": fill.contracts,
                        "bar_close_ms": fill.bar_close_ms,
                        "seen": fill.seen,
                        "filled": fill.filled,
                        "fee_usd": fee,
                    }
                )
                fees += fee
        if isinstance(trade.exit, Fill) and trade.exit.bar_close_ms == at_ms:
            closed += trade.pnl_usd
    return {
        "kind": step.group.value,
        "fills": sorted(fills, key=lambda each: each["leg"]),
        "fees_usd": fees,
        "cash_before_usd": step.cash_before_usd,
        "cash_after_usd": step.cash_after_usd,
        "collateral_before_usd": step.collateral_before_usd,
        "collateral_after_usd": step.collateral_after_usd,
        "closed_profit_usd": closed,
    }


def the_key_group(group: dict[str, Any]) -> dict[str, Any]:
    """A group of the answer key, as ``what_a_group_did`` would say it."""
    return {
        "kind": group["kind"],
        "fills": [
            {
                **fill,
                "seen": D(fill["seen"]),
                "filled": D(fill["filled"]),
                "fee_usd": D(fill["fee_usd"]),
            }
            for fill in sorted(group["fills"], key=lambda each: each["leg"])
        ],
        "fees_usd": D(group["fees_usd"]),
        "cash_before_usd": D(group["cash_before_usd"]),
        "cash_after_usd": D(group["cash_after_usd"]),
        "collateral_before_usd": D(group["collateral_before_usd"]),
        "collateral_after_usd": D(group["collateral_after_usd"]),
        "closed_profit_usd": D(group["closed_profit_usd"]),
    }


def trades_of(session: SessionResult) -> dict[str, LegTrade]:
    """The session's two trades, by leg."""
    return {trade.leg_id: trade for trade in session.outcome.trades}


async def test_a_call_credit_spread_is_sold_at_0936s_open_and_left_by_the_clock_for_exactly_the_hand_worked_money(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # THE SPREAD, ENTERED AND LEFT BY THE CLOCK, WORKED BY HAND FROM THE RECORDED BYTES.
    #
    # THE ENTRY. Decision at 09:35. The bar closing at 09:35 is the one stamped 09:34 (t = 1791552840000): close 776.55.
    # The listed call strikes around it are ... 776, 777, 778, 779 ...; 776.55 is 0.55 from 776 and 0.45 from 777, so the
    # money is 777. The leg that sells is the call at the money, O:SPY261009C00777000. The leg that covers it is two
    # listed strikes above the money, 777 -> 778 -> 779: O:SPY261009C00779000. The listing's one expiry is today, so the
    # same-day rule gives 2026-10-09 for both. The orders are decided at 1791552900000.
    #
    # The group fills at the OPEN of the bar closing at 09:36 (1791552960000), stamped 09:35 (t = 1791552900000) in both
    # files: the 777 call {"o":1,"h":1.07,"l":0.94,"c":0.98} and the 779 call {"o":0.35,"h":0.38,"l":0.33,"c":0.33}.
    #   * The 777 call is SOLD. The sale moves down by the slippage and rounds down to the step: 1.00 x (1 - 0.3/100) =
    #     0.997, down to 0.99. 0.99 x 100 x 2 = 198.00 comes in, less the fee 0.65 x 2 = 1.30: +196.70.
    #   * The 779 call is BOUGHT. The buy moves up and rounds up: 0.35 x 1.003 = 0.35105, up to 0.36. 0.36 x 100 x 2 =
    #     72.00 goes out, and the fee 1.30: -73.30.
    #   Cash 10000.00 + 196.70 - 73.30 = 10123.40. The two fees are 2.60.
    #   Collateral of what is held: 2 sold 777 calls and 2 bought 779 calls. At or above 779 the payoff of the pair is
    #   2 x (p - 779) - 2 x (p - 777) = -4 per share, and below it is no worse, so the loss is bounded at 4 x 100 =
    #   400.00. That is (779 - 777) x 100 x 2, the width of the spread, and the premiums are left out. 10123.40 is more
    #   than 400.00, so the group is taken.
    #
    # THE EXIT. The exit time is 30 minutes before the 16:00 close: 15:30 (1791574200000), where the bars stamped 15:29
    # close. The orders are decided there and fill at the OPEN of the bar closing at 15:31 (1791574260000), stamped 15:30
    # (t = 1791574200000): the 777 call {"o":1.9,"h":1.92,"l":1.84,"c":1.88} and the 779 call {"o":0.32,"h":0.32,
    # "l":0.28,"c":0.31}. The sold leg is bought back first.
    #   * The 777 call is BOUGHT BACK at 1.90 x 1.003 = 1.9057, up to 1.91: 1.91 x 100 x 2 = 382.00 and the fee 1.30 go
    #     out: -383.30.
    #   * The 779 call is SOLD at 0.32 x 0.997 = 0.31904, down to 0.31: 0.31 x 100 x 2 = 62.00 comes in, less the fee
    #     1.30: +60.70. It may leave because the sold calls are gone.
    #   Cash 10123.40 - 383.30 + 60.70 = 9800.80. Nothing is held, so the collateral is 0.00.
    #
    # PROFIT. The sold leg: (0.99 - 1.91) x 100 x 2 - 1.30 - 1.30 = -184.00 - 2.60 = -186.60. The bought leg:
    # (0.31 - 0.36) x 100 x 2 - 1.30 - 1.30 = -10.00 - 2.60 = -12.60. Together -199.20, and 10000.00 - 199.20 = 9800.80.
    await capture_the_recording()

    result = await the_run(a_call_credit_spread())

    [session] = result.sessions
    assert session.status is SessionStatus.TRADED
    trades = trades_of(session)
    short, long = trades["short_call"], trades["long_call"]
    assert (short.contract, long.contract) == (SOLD_CALL, BOUGHT_CALL)
    assert (short.contracts, long.contracts, short.multiplier) == (2, 2, 100)
    assert short.entry_decided_at_ms == long.entry_decided_at_ms == DECIDED_ENTRY_MS
    # The entry group.
    assert (short.entry.side, short.entry.bar_close_ms, short.entry.seen, short.entry.filled) == (
        "sell",
        ENTRY_FILL_MS,
        D("1.00"),
        D("0.99"),
    )
    assert (long.entry.side, long.entry.bar_close_ms, long.entry.seen, long.entry.filled) == (
        "buy",
        ENTRY_FILL_MS,
        D("0.35"),
        D("0.36"),
    )
    assert (short.entry_fee_usd, long.entry_fee_usd) == (D("1.30"), D("1.30"))
    opening = what_a_group_did(session, ENTRY_FILL_MS)
    assert (opening["kind"], opening["cash_before_usd"], opening["cash_after_usd"]) == (
        FillGroup.OPENING.value,
        D("10000.00"),
        D("10123.40"),
    )
    assert (opening["collateral_before_usd"], opening["collateral_after_usd"]) == (D("0.00"), D("400.00"))
    assert opening["fees_usd"] == D("2.60")
    assert opening == the_key_group(ANSWER_KEY["entry"]["group"])
    # The exit group.
    assert short.exit_reason is long.exit_reason is ExitReason.EXIT_TIME
    assert short.exit_decided_at_ms == long.exit_decided_at_ms == EXIT_DECIDED_MS
    assert (sale_of(short).side, sale_of(short).bar_close_ms, sale_of(short).seen, sale_of(short).filled) == (
        "buy",
        EXIT_FILL_MS,
        D("1.90"),
        D("1.91"),
    )
    assert (sale_of(long).side, sale_of(long).bar_close_ms, sale_of(long).seen, sale_of(long).filled) == (
        "sell",
        EXIT_FILL_MS,
        D("0.32"),
        D("0.31"),
    )
    assert (short.exit_fee_usd, long.exit_fee_usd) == (D("1.30"), D("1.30"))
    closing = what_a_group_did(session, EXIT_FILL_MS)
    assert (closing["kind"], closing["cash_before_usd"], closing["cash_after_usd"]) == (
        FillGroup.CLOSING.value,
        D("10123.40"),
        D("9800.80"),
    )
    assert (closing["collateral_before_usd"], closing["collateral_after_usd"]) == (D("400.00"), D("0.00"))
    assert closing == the_key_group(ANSWER_KEY["exit"]["group"])
    # The profit, leg by leg and together, and the cash the session ends with.
    assert (short.pnl_usd, long.pnl_usd) == (D("-186.60"), D("-12.60"))
    assert session.outcome.pnl_usd == D("-199.20") == D(ANSWER_KEY["exit"]["total_profit_usd"])
    assert (session.cash_before_usd, session.cash_after_usd) == (D("10000.00"), D("9800.80"))
    assert D(ANSWER_KEY["exit"]["ending_cash_usd"]) == session.cash_after_usd
    assert (result.summary.net_profit_usd, result.summary.ending_cash_usd) == (D("-199.20"), D("9800.80"))
    # Nothing else happened: two picks, two entries, two exits decided at the clock, two closing fills.
    assert session.outcome.event_counts == {
        EventKind.PICKED: 2,
        EventKind.ENTRY_FILLED: 2,
        EventKind.EXIT_TIME: 2,
        EventKind.CLOSE_FILLED: 2,
    }


async def test_a_stop_on_the_sold_leg_buys_it_back_alone_at_the_next_open_and_leaves_its_cover_for_the_exit_time(
    the_vendor: respx.MockRouter, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    # THE SAME SPREAD WITH A STOP LOSS OF 18 PERCENT ON THE SOLD LEG, WORKED BY HAND FROM THE RECORDED BYTES.
    #
    # The entry is the one above: the 777 call sold at 0.99 and the 779 call bought at 0.36 at the open of the bar
    # closing 09:36, cash 10123.40, collateral 400.00.
    #
    # THE STOP. A sold leg's level is what it was SOLD FOR (the price filled, 0.99, and not the 1.00 seen) moved up by the
    # rule: 0.99 x (1 + 18/100) = 1.1682. It is hit by a minute that CLOSES at or above it, and a high inside a minute
    # never does. 18 percent was chosen after reading the recorded bars, because it makes one bar tell the two readings
    # apart: the 777 call's bar stamped 10:58 (t = 1791557880000) is {"o":1,"h":1.18,"l":0.99,"c":1.15}. Its high of 1.18
    # is over 1.1682 and its close of 1.15 is under it, so a stop read on a high would be hit there and the rule is not.
    # It is the only bar from the bar stamped 09:35 on, up to the one below, whose high reaches the level, and 1.15 is the
    # highest close up to the one below. The closes stay under 1.1682 until the bar stamped 11:18 (t = 1791559080000),
    # which closes at 1.20: {"o":1.1,"h":1.26,"l":1.09,"c":1.2}. So the stop is hit at the close at 11:19 = 1791559140000,
    # at the price 1.20, and that decides the close of the sold leg.
    #
    # THE BUY-BACK fills at the OPEN of the bar closing at 11:20 (1791559200000), stamped 11:19: {"o":1.19,"h":1.28,
    # "l":1.14,"c":1.15}. 1.19 x 1.003 = 1.19357, up to 1.20: 1.20 x 100 x 2 = 240.00 and the fee 1.30 go out: -241.30.
    # The level was 1.1682 and the close that hit it 1.20: the fill is the next open, and it is not the level. Cash
    # 10123.40 - 241.30 = 9882.10. The stop closes only the leg that carries it, so the 779 call stays held: a bought
    # call has no requirement, and the collateral is 0.00 where it was 400.00.
    #   Profit of the sold leg: (0.99 - 1.20) x 100 x 2 - 1.30 - 1.30 = -42.00 - 2.60 = -44.60.
    #
    # THE EXIT. The 779 call is held until the exit time 15:30, and is sold as above, at 0.31 for 62.00 less the fee
    # 1.30: +60.70. Cash 9882.10 + 60.70 = 9942.80. Its profit is -12.60 as above. Together -44.60 - 12.60 = -57.20, and
    # 10000.00 - 57.20 = 9942.80.
    await capture_the_recording()

    result = await the_run(a_call_credit_spread(stop_percent="18"))

    [session] = result.sessions
    assert session.status is SessionStatus.TRADED
    trades = trades_of(session)
    short, long = trades["short_call"], trades["long_call"]
    # The same entry group as the spread held by the clock.
    assert what_a_group_did(session, ENTRY_FILL_MS) == the_key_group(ANSWER_KEY["entry"]["group"])
    # The stop on the sold leg alone.
    assert short.stop_level == D("1.1682") == D(ANSWER_KEY["stop_on_the_sold_leg"]["stop_level"])
    assert short.exit_reason is ExitReason.STOP and short.exit_decided_at_ms == STOP_DECIDED_MS
    # The bar stamped 10:58 has a high at the level and a close under it, and the stop was not hit there: it was read on
    # a close. The bar is read from the recorded file by the standard library, as the one oracle here that is not our code.
    bar_1058 = {row["t"]: row for row in recorded_rows(RECORDED_FILES[SOLD_CALL_PATH])}[1791557880000]
    assert bar_1058["h"] >= short.stop_level > bar_1058["c"]
    [stop_hit] = [event for event in session.outcome.events if event.kind is EventKind.STOP_HIT]
    assert (stop_hit.at_ms, stop_hit.leg_id, stop_hit.price) == (STOP_DECIDED_MS, "short_call", D("1.20"))
    assert D(ANSWER_KEY["stop_on_the_sold_leg"]["stop_close"]) == stop_hit.price
    assert (sale_of(short).side, sale_of(short).bar_close_ms, sale_of(short).seen, sale_of(short).filled) == (
        "buy",
        STOP_FILL_MS,
        D("1.19"),
        D("1.20"),
    )
    assert short.pnl_usd == D("-44.60")
    stopped = what_a_group_did(session, STOP_FILL_MS)
    assert (stopped["kind"], stopped["cash_before_usd"], stopped["cash_after_usd"]) == (
        FillGroup.CLOSING.value,
        D("10123.40"),
        D("9882.10"),
    )
    assert (stopped["collateral_before_usd"], stopped["collateral_after_usd"]) == (D("400.00"), D("0.00"))
    assert stopped == the_key_group(ANSWER_KEY["stop_on_the_sold_leg"]["group"])
    # The bought leg was left alone by the stop, and leaves at the exit time.
    assert long.exit_reason is ExitReason.EXIT_TIME and long.exit_decided_at_ms == EXIT_DECIDED_MS
    assert (sale_of(long).side, sale_of(long).bar_close_ms, sale_of(long).seen, sale_of(long).filled) == (
        "sell",
        EXIT_FILL_MS,
        D("0.32"),
        D("0.31"),
    )
    assert long.pnl_usd == D("-12.60")
    exit_group = ANSWER_KEY["stop_on_the_sold_leg"]["then_the_exit_time_sells_the_cover"]
    assert what_a_group_did(session, EXIT_FILL_MS) == the_key_group(exit_group["group"])
    assert session.outcome.pnl_usd == D("-57.20") == D(exit_group["total_profit_usd"])
    assert (session.cash_before_usd, session.cash_after_usd) == (D("10000.00"), D("9942.80"))
    assert D(exit_group["ending_cash_usd"]) == session.cash_after_usd
    assert session.outcome.event_counts == {
        EventKind.PICKED: 2,
        EventKind.ENTRY_FILLED: 2,
        EventKind.STOP_HIT: 1,
        EventKind.EXIT_TIME: 1,
        EventKind.CLOSE_FILLED: 2,
    }


# ── The script that records the fixture ───────────────────────────────────────────────────────────────────────


def _the_regeneration_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``regenerate.py`` loaded from the fixture's folder. Loading it runs nothing: it acts only as ``__main__``."""
    name = "regenerate_options_backtest_spy_2026_10_09_sold_spread"
    spec = importlib.util.spec_from_file_location(name, RECORDING / "regenerate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # its dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def _the_vendor_answers_with(bodies: dict[str, bytes]) -> None:
    """Make the vendor answer each request path with these bytes (called in a test that is under ``@respx.mock``)."""
    for path, body in bodies.items():
        respx.get(host="api.polygon.io", path=path).mock(
            return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"})
        )


@respx.mock
def test_the_regeneration_script_run_against_the_recorded_answers_writes_the_same_files_and_the_same_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline: the vendor is the recording itself. The script must ask for exactly the requests the manifest lists, send
    # the key in a header only, and write the bodies as received and a manifest equal to the committed one.
    script = _the_regeneration_script(monkeypatch)
    monkeypatch.setenv("POLYGON_API_KEY", THE_KEY)
    _the_vendor_answers_with({path: recorded(name) for path, name in RECORDED_FILES.items()})
    out = tmp_path / "recording"

    facts = script.main(out)

    assert sorted(path.name for path in out.iterdir()) == sorted([*RECORDED_FILES.values(), "manifest.json"])
    for name in (*RECORDED_FILES.values(), "manifest.json"):
        assert (out / name).read_bytes() == recorded(name), f"{name} differs from the recording"
    assert facts["contracts"] == {"sold_call": "O:SPY261009C00777000", "bought_call": "O:SPY261009C00779000"}
    asked = [call.request for call in respx.calls]
    assert sorted(request.url.path for request in asked) == sorted(RECORDED_FILES)
    assert all(request.headers["authorization"] == f"Bearer {THE_KEY}" for request in asked)
    assert not any(THE_KEY in str(request.url) for request in asked)


@respx.mock
def test_the_regeneration_script_writes_nothing_when_a_contract_has_no_bar_in_a_minute_a_scene_fills_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sold call's answer with the bar stamped 15:30 (t = 1791574200000), where the exit fills, taken out of it. The
    # check comes after every request and before any file is written: nothing is left in the folder, which is not made.
    script = _the_regeneration_script(monkeypatch)
    monkeypatch.setenv("POLYGON_API_KEY", THE_KEY)
    sold = json.loads(recorded(RECORDED_FILES[SOLD_CALL_PATH]))
    sold["results"] = [row for row in sold["results"] if row["t"] != 1791574200000]
    bodies = {path: recorded(name) for path, name in RECORDED_FILES.items()}
    bodies[SOLD_CALL_PATH] = json.dumps(sold).encode()
    _the_vendor_answers_with(bodies)
    out = tmp_path / "recording"

    with pytest.raises(RuntimeError, match="O:SPY261009C00777000 has no bar stamped 15:30"):
        script.main(out)

    assert not out.exists()


@pytest.mark.parametrize("name", ["mine.txt", "work/"])
@respx.mock
def test_the_regeneration_script_makes_no_request_for_a_folder_that_is_not_empty(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline: respx answers every request with an error, and the key is a made-up one. The check comes first, so the
    # script never gets as far as a request.
    script = _the_regeneration_script(monkeypatch)
    monkeypatch.setenv("POLYGON_API_KEY", THE_KEY)
    folder = tmp_path / "to-record-into"
    folder.mkdir()
    if name.endswith("/"):
        (folder / name).mkdir()
    else:
        (folder / name).write_text("keep me")

    with pytest.raises(ValueError, match="not an empty folder"):
        script.main(folder)

    assert len(respx.calls) == 0
    assert [path.name for path in folder.iterdir()] == [name.rstrip("/")]
