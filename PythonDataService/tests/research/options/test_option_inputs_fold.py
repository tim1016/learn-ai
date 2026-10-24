"""The price of an option on a session is its last regular-hours trade: ``regular_session_close`` (#3540; owner decision D-A).

The vendor's daily bar for an option closes at the day's last trade, tail included, and carries a full-day VWAP. The lake stores
minute bars only. The study prices each contract at the close of the latest minute bar that lies wholly inside the scheduled
session, kept with its regular-hours volume (the price rule then wants at least 50 contracts). Hand-worked cases first, then
the vendor's real answer for SPY on 2025-06-10, which holds an after-hours tail on both contracts.

Times, New York (EDT = UTC-4 in June, EST = UTC-5 in winter), worked by hand:

* 2025-06-10 is a full session: it opens 09:30 = 13:30 UTC = 1749562200000 ms and closes 16:00 = 20:00 UTC =
  1749562200000 + 390 x 60 000 = 1749585600000. The bar that starts at 15:59 is minute 389 after the open (start
  1749562200000 + 389 x 60 000 = 1749585540000) and closes at 1749585600000, the session's close: it is the last bar that
  lies wholly inside. The bar that starts at 16:00 (minute 390) does not.
* 2025-07-03 is an early close, 13:00 EDT = 17:00 UTC = 1751562000000, so its session is 210 minutes long and its last
  whole-session bar starts at 12:59 (1751562000000 - 60 000 = 1751561940000).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import respx

from app.data_lake.option_bars import OptionBar, ensure_option_day_bars, read_option_day_bars
from app.data_lake.option_listing import ensure_option_listing
from app.options.contract import OptionContract
from app.options_backtest.lake_data import OptionBarsCannotBePlaced
from app.research.options.option_inputs import regular_session_close
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake

D = Decimal
MINUTE = 60_000
TUESDAY = date(2025, 6, 10)
TUESDAY_OPEN, TUESDAY_CLOSE = 1749562200000, 1749585600000
EARLY_CLOSE_DAY = date(2025, 7, 3)
EARLY_OPEN, EARLY_CLOSE = 1751549400000, 1751562000000


def bar(start_ms: int, close: str, volume: int = 1) -> OptionBar:
    price = D(close)
    return OptionBar(start_ms=start_ms, open=price, high=price, low=price, close=price, volume=D(volume))


# ── hand-worked ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_close_is_the_last_bar_inside_the_session_and_the_minute_stamped_16_00_and_the_tail_are_left_out():
    bars = [
        bar(TUESDAY_OPEN, "1.00", 10),  # 09:30, the first minute: in
        bar(TUESDAY_OPEN + 388 * MINUTE, "1.40", 7),  # 15:58: in
        bar(TUESDAY_CLOSE - MINUTE, "1.50", 13),  # 15:59: in, the last whole-session bar
        bar(TUESDAY_CLOSE, "9.99", 500),  # 16:00: the first minute after the session: out
        bar(TUESDAY_CLOSE + 13 * MINUTE, "8.88", 600),  # 16:13: the ETF-option tail: out
    ]

    close, volume, last_start = regular_session_close(bars, TUESDAY)

    assert close == D("1.50"), "the 15:59 close, not the 16:00 print and not the 16:13 print"
    assert volume == D(10 + 7 + 13), "the regular-hours volume: 30 contracts, the 1,100 after the close not counted"
    assert last_start == TUESDAY_CLOSE - MINUTE


def test_an_early_close_ends_the_session_at_the_calendars_close_so_the_12_59_bar_is_last():
    bars = [
        bar(EARLY_OPEN, "2.00", 4),
        bar(EARLY_CLOSE - MINUTE, "2.25", 6),  # 12:59: in
        bar(EARLY_CLOSE, "3.00", 90),  # 13:00: out
        bar(EARLY_CLOSE + 179 * MINUTE, "3.50", 90),  # 15:59: out on a half day
    ]

    assert regular_session_close(bars, EARLY_CLOSE_DAY) == (D("2.25"), D(10), EARLY_CLOSE - MINUTE)


def test_a_bar_before_the_open_is_not_in_the_session():
    bars = [bar(TUESDAY_OPEN - MINUTE, "0.50", 99), bar(TUESDAY_OPEN, "0.60", 1)]

    assert regular_session_close(bars, TUESDAY) == (D("0.60"), D(1), TUESDAY_OPEN)
    assert regular_session_close([bar(TUESDAY_OPEN - MINUTE, "0.50", 99)], TUESDAY) == (None, D(0), None)


@pytest.mark.parametrize(
    "bars",
    [
        pytest.param([], id="no bars: the contract was listed and did not trade"),
        pytest.param([bar(TUESDAY_CLOSE + 5 * MINUTE, "4.00", 800)], id="only a bar after the close"),
    ],
)
def test_a_contract_with_no_regular_hours_trade_has_no_price_and_no_volume(bars):
    assert regular_session_close(bars, TUESDAY) == (None, D(0), None)


def test_the_last_bar_is_the_one_with_the_greatest_start_whatever_the_order_the_bars_arrive_in():
    late, early = bar(TUESDAY_OPEN + 200 * MINUTE, "7.00", 2), bar(TUESDAY_OPEN + 100 * MINUTE, "6.00", 3)

    assert regular_session_close([late, early], TUESDAY)[0] == D("7.00")
    assert regular_session_close([early, late], TUESDAY)[0] == D("7.00")


def test_volume_is_summed_in_exact_decimals():
    bars = [bar(TUESDAY_OPEN + i * MINUTE, "1.00", 1) for i in range(0, 390)]

    assert regular_session_close(bars, TUESDAY)[1] == D(390)


@pytest.mark.parametrize(
    ("start_ms", "why"),
    [
        pytest.param(TUESDAY_OPEN + 1_000, "off a whole minute", id="off-minute"),
        pytest.param(TUESDAY_OPEN + (14 * 60 + 29) * MINUTE, "starts at 23:59 and closes on the next New York date", id="other-date"),
    ],
)
def test_a_stored_bar_that_cannot_be_placed_in_the_session_fails_the_whole_day_and_nothing_is_left_out(start_ms, why):
    """ADR 0078 decision 17: the bar left out could be the last trade, and the price would not say so."""
    bars = [bar(TUESDAY_OPEN, "1.00"), bar(start_ms, "2.00")]

    with pytest.raises(OptionBarsCannotBePlaced, match="cannot be placed"):
        regular_session_close(bars, TUESDAY)


# ── the vendor's real answer ────────────────────────────────────────────────────────────────────────────────────

RECORDING = Path(__file__).parents[2] / "fixtures/golden/options-backtest/options-backtest-spy-2025-06-10/v1"
LISTING_PATH = "/v3/reference/options/contracts"
STOCK_PATH = "/v2/aggs/ticker/SPY/range/1/minute/2025-06-10/2025-06-10"
CALL_PATH = "/v2/aggs/ticker/O:SPY250613C00600000/range/1/minute/2025-06-10/2025-06-10"
PUT_PATH = "/v2/aggs/ticker/O:SPY250613P00600000/range/1/minute/2025-06-10/2025-06-10"
RECORDED_FILES = {
    LISTING_PATH: "spy_contracts_as_of_2025-06-10_expiring_2025-06-13.json",
    STOCK_PATH: "spy_minute_2025-06-10.json",
    CALL_PATH: "O_SPY250613C00600000_minute_2025-06-10.json",
    PUT_PATH: "O_SPY250613P00600000_minute_2025-06-10.json",
}
CALL_600 = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="call", strike=600)
PUT_600 = OptionContract.of(root="SPY", expiry_date=date(2025, 6, 13), right="put", strike=600)

#: Read off the recorded bodies with the standard library's ``json`` (not our code): for each contract, the number of bars the
#: vendor sent and how many lie wholly inside the session; the close and the volume summed over them; and the full-day numbers
#: the vendor's own daily bar would have used (the last print of the day, and all the volume).
#: Call 600: 389 bars, 377 inside; the 15:59 bar closes at 5.57; 31,657 contracts inside, 31,899 in all; the last print of
#: the day, at 16:13, is 5.14. Put 600: 400 bars, 386 inside; the 15:59 bar closes at 2.08; 28,954 inside, 30,078 in all;
#: the minute stamped 16:00 closes at 2.26 and the last print of the day is 2.39.
REAL = {
    "call": (389, 377, D("5.57"), D(31657), D(31899), D("5.14")),
    "put": (400, 386, D("2.08"), D(28954), D(30078), D("2.39")),
}


def recorded_oracle(name: str) -> tuple[int, int, Decimal, Decimal]:
    """The recorded bars folded by the plainest loop there is: bars inside the session, the last one's close, the volume."""
    rows = json.loads((RECORDING / name).read_bytes(), parse_float=D)["results"]
    inside = [row for row in rows if row["t"] >= TUESDAY_OPEN and row["t"] + MINUTE <= TUESDAY_CLOSE]
    return len(rows), len(inside), inside[-1]["c"], D(sum(row["v"] for row in inside))


@pytest.mark.parametrize(
    ("kind", "contract", "file"),
    [("call", CALL_600, "O_SPY250613C00600000_minute_2025-06-10.json"), ("put", PUT_600, "O_SPY250613P00600000_minute_2025-06-10.json")],
)
@respx.mock
async def test_the_vendors_real_spy_day_through_the_lake_prices_each_option_at_its_15_59_close(
    kind, contract, file, fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    """389 and 400 real minutes, each with an after-hours tail that moves the last print of the day (the put's by 31 cents)."""
    bars, inside_count, close, volume, all_volume, last_print = REAL[kind]
    with recorded_vendor(RECORDING, RECORDED_FILES, STOCK_PATH):
        assert (await ensure_option_listing("SPY", TUESDAY)).failure is None
        assert (await ensure_option_day_bars(contract, TUESDAY, listed_under="SPY")).failure is None
    stored = await asyncio.to_thread(read_option_day_bars, contract, TUESDAY, listed_under="SPY")
    assert stored is not None

    folded = regular_session_close(stored.bars, TUESDAY)

    assert (len(stored.bars), recorded_oracle(file)[:2]) == (bars, (bars, inside_count))
    assert folded == (close, volume, TUESDAY_CLOSE - MINUTE) and folded[:2] == recorded_oracle(file)[2:]
    assert sum((b.volume for b in stored.bars), D(0)) == all_volume > volume, "regular-hours volume is less than the day's"
    assert stored.bars[-1].close == last_print != close, "the last print of the day is not the price: it sits in the tail"
