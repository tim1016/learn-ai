"""One session's market over what a run loaded for the date.

Minutes are counted after the open; the bar of minute 5 closes five minutes after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from app.options_backtest.market import MinuteBar, Picked, PickRefusal, PickRefused
from app.options_backtest.session_market import (
    ContractDayNotLoaded,
    DayMarket,
    MalformedBars,
    window_only_market,
)
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup
from tests.options_backtest._market import leg
from tests.options_backtest._run_data import (
    FRI,
    HALF_DAY,
    THU,
    D,
    close_ms,
    contract_on,
    listing_on,
    open_ms,
    option_bars,
    underlying_bars,
)

CALL_500 = contract_on(FRI, "call", 500)
CALL_505 = contract_on(FRI, "call", 505)


def _leg() -> LegSetup:
    return LegSetup.model_validate(leg("call", "call"))


def _market(
    *,
    underlying: Sequence[MinuteBar] | None = None,
    options: dict | None = None,
    day: date = FRI,
    complete: bool = True,
) -> DayMarket:
    return DayMarket(
        day,
        underlying=underlying if underlying is not None else underlying_bars(day, {5: "500.40", 7: "500.60"}),
        listing=listing_on(day, complete=complete),
        option_days=options if options is not None else {CALL_500: option_bars(day, {5: ("2.00", "2.10")})},
    )


def test_a_full_days_market_gives_its_underlying_close_its_session_and_its_pick() -> None:
    market = _market()

    assert market.underlying_close(close_ms(FRI, 5)) == D("500.40"), "the close of the bar closing at minute 5"
    assert market.underlying_close(close_ms(FRI, 6)) is None, "none where the underlying did not trade"
    assert market.underlying_close(close_ms(FRI, 7)) == D("500.60"), "the close of the bar closing at minute 7"

    # 2026-10-09 is in daylight time (UTC-4): 09:30 is 13:30 UTC. The day is 20735 days after 1970-01-01, which is
    # 20735 x 86400 = 1,791,504,000 s; plus 13.5 h = 48,600 s gives 1,791,552,600 s. The close is 390 minutes later.
    assert market.session_open_ms == 1_791_552_600_000, "a full day opens at nine thirty New York time"
    assert market.session_close_ms == 1_791_552_600_000 + 390 * 60_000 == 1_791_576_000_000, (
        "a full day closes at four New York time"
    )

    # Strikes are listed 495, 500, 505, 510. 500.4 is nearest 500; 506.9 is nearest 505 (1.9 away; 510 is 3.1).
    low = market.pick(_leg(), at_ms=close_ms(FRI, 5), underlying_price=D("500.4"))
    high = market.pick(_leg(), at_ms=close_ms(FRI, 9), underlying_price=D("506.9"))

    assert isinstance(low, Picked) and isinstance(high, Picked), "a pick on this date's listing is a pick"
    assert (low.contract, high.contract) == (CALL_500, CALL_505), "a pick follows the underlying price"


def test_the_underlying_price_is_the_bars_close_and_not_its_open_high_or_low() -> None:
    # One bar, minute 5: opened at 500.0, reached 510.0 and 490.0, and closed at 505.2. Against the strikes listed
    # (495, 500, 505, 510) each price comes to a different contract: the open to 500, the high to 510, the low to 495
    # (490 is below the lowest strike) and the close to 505 (0.2 away). Only the close is the underlying's price.
    around = underlying_bars(FRI, {5: ("500.0", "510.0", "490.0", "505.2")})
    market = _market(underlying=around)

    price = market.underlying_close(close_ms(FRI, 5))
    picked = market.pick(_leg(), at_ms=close_ms(FRI, 5), underlying_price=price)

    assert price == D("505.2")
    assert isinstance(picked, Picked) and picked.contract == CALL_505


def test_a_contracts_bar_is_the_bar_closing_then_and_none_where_it_did_not_trade() -> None:
    bars = option_bars(FRI, {5: ("2.00", "2.10"), 8: ("2.20", "2.30")})
    market = _market(options={CALL_500: bars})

    first, second = market.option_bar(CALL_500, close_ms(FRI, 5)), market.option_bar(CALL_500, close_ms(FRI, 8))

    assert first is not None and (first.open, first.close) == (D("2.00"), D("2.10"))
    assert second is not None and (second.open, second.close) == (D("2.20"), D("2.30"))
    assert market.option_bar(CALL_500, close_ms(FRI, 6)) is None  # nothing traded in minute 6, and none is invented
    assert market.option_bar(CALL_500, close_ms(FRI, 9)) is None


def test_a_contract_whose_day_was_loaded_and_empty_never_traded_and_is_not_an_error() -> None:
    market = _market(options={CALL_500: ()})

    assert [market.option_bar(CALL_500, close_ms(FRI, minute)) for minute in (1, 5, 390)] == [None, None, None]


def test_a_contract_whose_day_was_never_loaded_is_a_named_error_and_not_a_minute_with_no_trade() -> None:
    market = _market(options={CALL_500: ()})

    with pytest.raises(ContractDayNotLoaded) as raised:
        market.option_bar(CALL_505, close_ms(FRI, 5))

    assert raised.value.contract == CALL_505
    assert raised.value.trading_date == FRI
    assert CALL_505.occ_symbol in str(raised.value)


def test_a_half_day_closes_at_one() -> None:
    # 2026-11-27 is in standard time (UTC-5): 09:30 is 14:30 UTC and 13:00 is 18:00 UTC. The day is 20784 days after
    # 1970-01-01 = 1,795,737,600 s; plus 52,200 s (14.5 h) opens at 1,795,789,800 s and plus 64,800 s (18 h) closes at
    # 1,795,802,400 s: 210 minutes.
    market = _market(day=HALF_DAY, underlying=underlying_bars(HALF_DAY, {5: "500.40"}), options={})

    assert market.session_open_ms == 1_795_789_800_000
    assert market.session_close_ms == 1_795_802_400_000
    assert (market.session_close_ms - market.session_open_ms) // MS_PER_MINUTE == 210


def test_a_day_that_is_not_a_session_has_no_market() -> None:
    saturday = date(2026, 10, 10)

    with pytest.raises(LookupError):
        DayMarket(saturday, underlying=(), listing=listing_on(saturday), option_days={})


def test_a_pick_the_listing_refuses_comes_back_as_the_refusal() -> None:
    market = _market(complete=False)

    refused = market.pick(_leg(), at_ms=close_ms(FRI, 5), underlying_price=D("500.4"))

    assert isinstance(refused, PickRefused) and refused.reason is PickRefusal.CHAIN_INCOMPLETE


@pytest.mark.parametrize(
    "at_ms",
    [
        open_ms(FRI),  # the open itself closes no bar
        open_ms(FRI) + 5 * MS_PER_MINUTE + 30_000,  # between minutes
        close_ms(FRI, 391),  # after the close
    ],
)
def test_a_pick_at_a_time_that_is_no_minute_close_of_the_session_is_a_mistake(at_ms: int) -> None:
    with pytest.raises(ValueError, match="minute close inside the session"):
        _market().pick(_leg(), at_ms=at_ms, underlying_price=D("500.4"))


def test_a_listing_of_another_date_cannot_make_a_market() -> None:
    with pytest.raises(ValueError, match="describes"):
        DayMarket(FRI, underlying=(), listing=listing_on(THU), option_days={})


def _bar(day: date, minute: int, price: str = "2.00") -> MinuteBar:
    return MinuteBar(close_ms(day, minute), D(price), D(price), D(price), D(price))


_BAD_BARS = {
    "a bar twice": ([_bar(FRI, 5), _bar(FRI, 5)], "twice"),
    "bars out of order": ([_bar(FRI, 6), _bar(FRI, 5)], "after a later one"),
    "a bar between minutes": ([MinuteBar(close_ms(FRI, 5) + 1_000, D(2), D(2), D(2), D(2))], "whole minute"),
    "the bars of another day": ([_bar(THU, 5)], "another day"),
}


@pytest.mark.parametrize(("bars", "words"), _BAD_BARS.values(), ids=_BAD_BARS.keys())
def test_bars_that_cannot_be_a_day_of_minute_bars_are_refused_for_the_underlying_and_for_a_contract_and_name_it(
    bars: list[MinuteBar], words: str
) -> None:
    with pytest.raises(MalformedBars, match=words) as for_the_underlying:
        _market(underlying=bars)
    with pytest.raises(MalformedBars, match=words) as for_a_contract:
        _market(options={CALL_500: bars})

    assert "the underlying" in str(for_the_underlying.value), "refused for the underlying, and it names the underlying"
    assert CALL_500.occ_symbol in str(for_a_contract.value), "refused for a contract, and it names the contract"


_PRICE_COLUMNS = ("open", "high", "low", "close")
_NOT_A_PRICE = {"zero": "0", "negative": "-1.5", "not a number": "NaN", "infinite": "Infinity"}


@pytest.mark.parametrize("column", _PRICE_COLUMNS)
@pytest.mark.parametrize("bad", _NOT_A_PRICE.values(), ids=_NOT_A_PRICE.keys())
def test_a_price_that_is_not_above_zero_is_refused_in_every_column_of_a_bar_for_the_underlying_and_a_contract(
    column: str, bad: str
) -> None:
    # The replay fills at a bar's open, so an open of nothing would be a free buy: every column is held to the same rule.
    prices = {"open": "2.00", "high": "2.00", "low": "2.00", "close": "2.00", column: bad}
    bar = MinuteBar(close_ms(FRI, 5), *(D(prices[name]) for name in _PRICE_COLUMNS))

    with pytest.raises(MalformedBars, match="not above zero") as for_the_underlying:
        _market(underlying=[bar])
    with pytest.raises(MalformedBars, match="not above zero") as for_a_contract:
        _market(options={CALL_500: [bar]})

    assert "the underlying" in str(for_the_underlying.value)
    assert CALL_500.occ_symbol in str(for_a_contract.value)


def test_bars_outside_the_session_on_the_same_day_are_allowed_and_the_session_is_still_read() -> None:
    # An option can trade after the underlying's close, and a pre-market bar may come with the day.
    around = [_bar(FRI, -30), _bar(FRI, 5, "2.05"), _bar(FRI, 395)]
    market = _market(underlying=around, options={CALL_500: around})

    assert market.underlying_close(close_ms(FRI, 5)) == D("2.05")
    bar = market.option_bar(CALL_500, close_ms(FRI, 5))
    assert bar is not None and bar.close == D("2.05")


_ASKS = {
    "the underlying": lambda market: market.underlying_close(close_ms(HALF_DAY, 5)),
    "a pick": lambda market: market.pick(_leg(), at_ms=close_ms(HALF_DAY, 5), underlying_price=Decimal("500")),
    "a contract's bar": lambda market: market.option_bar(contract_on(HALF_DAY, "call", 500), close_ms(HALF_DAY, 5)),
}


@pytest.mark.parametrize("ask", _ASKS.values(), ids=_ASKS.keys())
def test_a_session_the_window_does_not_fit_has_a_market_that_fails_when_asked_for_anything(ask) -> None:
    market = window_only_market(HALF_DAY)

    assert (market.session_open_ms, market.session_close_ms) == (1_795_789_800_000, 1_795_802_400_000)
    with pytest.raises(RuntimeError, match="should have been skipped"):
        ask(market)
