"""The past chain: what an underlying had listed on a past date, and what each contract last traded at (#3030).

Every date is checked against the canonical calendar when the module loads, and every expectation is worked by hand.
A bar is named by its close, and "minute k" is the bar closing k minutes after the session's open (``_run_data.py``).

The scene most tests share is Friday 2026-10-09, a full session (open 13:30 UTC = 1791552600000 ms, close 20:00 UTC =
1791576000000 ms), with SPY options expiring that day. The underlying closed at 602.40 in minute 10 and at 602.60 in
minute 390. The calls listed are 598 to 606 in whole dollars, and a non-standard call at 602.5; the puts are 599, 600,
601, 603 and 604 (no 602).

* At minute 10 (the underlying at 602.40): the nearest call strike is 602, whose distance is 0.40. The puts have no 602;
  603 is 0.60 away and 601 is 1.40 away, so the money is 603. The two rights have different money at one instant.
* At the close (602.60): calls 603 (0.40, against 0.60 for 602), puts 603.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.lean_sidecar.trading_calendar import is_trading_day
from app.options.contract import OptionContract, OptionRight
from app.options.expiry import option_expiry_anchor_ms
from app.options_backtest.classification import CLASSIFICATION_VERSION, ExpirySeries
from app.options_backtest.data import Captured
from app.options_backtest.lake_data import StoredListing, UnderlyingDayUnreadable
from app.options_backtest.market import MinuteBar
from app.options_backtest.past_chain import (
    DEFAULT_STRIKES_EACH_SIDE,
    MAX_STRIKES_EACH_SIDE,
    MAX_WINDOW_CONTRACTS,
    ChainExpiry,
    ChainSession,
    InstantNotOnAMinute,
    InstantOutsideSession,
    ListingState,
    NotAContract,
    NotAnExpiryAnchor,
    NotASession,
    PriceState,
    UnderlyingState,
    WindowTooWide,
    chain_session,
    contract_from_symbol,
    describe_day,
    describe_expiry,
    expiry_date_of,
)
from app.options_backtest.picking import ListedContract, pick_contract, strike_for
from app.options_backtest.setup import AtTheMoney, LegSetup, SameDayExpiryRule
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._run_data import close_ms, open_ms, option_bars, underlying_bars

D = Decimal
SHA = "ab" * 32
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _on(year: int, month: int, day: int, weekday: str, *, session: bool = True) -> date:
    found = date(year, month, day)
    assert _WEEKDAYS[found.weekday()] == weekday, f"{found} is a {_WEEKDAYS[found.weekday()]}"
    assert is_trading_day(found) is session, f"{found} {'is not' if session else 'is'} a session on the calendar"
    return found


FRI = _on(2026, 10, 9, "Fri")  # the second Friday of its month: a weekly
FRI_THIRD = _on(2026, 10, 16, "Fri")  # the third Friday: a monthly
MON_AFTER = _on(2026, 10, 12, "Mon")
SAT = _on(2026, 10, 10, "Sat", session=False)
CHRISTMAS = _on(2026, 12, 25, "Fri", session=False)
THANKSGIVING = _on(2026, 11, 26, "Thu", session=False)
HALF_DAY = _on(2025, 11, 28, "Fri")

# 2026-10-09 in New York time, in ms UTC (EDT, four hours behind UTC), worked from the epoch by hand and by script.
FRI_NEW_YORK_MIDNIGHT_MS = 1791518400000  # 04:00 UTC
FRI_OPEN_MS = 1791552600000  # 13:30 UTC
FRI_CLOSE_MS = 1791576000000  # 20:00 UTC
FRI_LAST_MS = 1791604799999  # 03:59:59.999 UTC on the 10th: the last millisecond of the 9th, New York


def _put(strike: int | str, expiry: date = FRI) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right="put", strike=D(strike))


def _call(strike: int | str, expiry: date = FRI) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right="call", strike=D(strike))


def _listed(contract: OptionContract, *, standard: bool = True) -> ListedContract:
    return ListedContract(contract=contract, multiplier=100 if standard else 10, standard=standard)


def _listing(*listed: ListedContract, day: date = FRI, left_out: int = 0) -> StoredListing:
    return StoredListing(underlying="SPY", as_of=day, contracts=tuple(listed), complete=True, rows_left_out=left_out)


class FakeChainData:
    """``RunData`` over dictionaries. An exception as a value is raised; ``None`` is not captured; absent is not captured."""

    def __init__(
        self,
        *,
        underlying: Sequence[MinuteBar] | Exception | None = None,
        listing: StoredListing | Exception | None = None,
        options: Mapping[OptionContract, Sequence[MinuteBar] | Exception] | None = None,
    ) -> None:
        self._underlying = underlying
        self._listing = listing
        self._options = dict(options or {})
        self.asked: list[tuple[str, OptionContract | None]] = []

    @staticmethod
    def _answer(value: object) -> Captured | None:
        if isinstance(value, Exception):
            raise value
        return None if value is None else Captured(value=value, sha256=SHA)

    def underlying_day(self, trading_date: date):
        self.asked.append(("underlying", None))
        return self._answer(self._underlying)

    def listing(self, trading_date: date):
        self.asked.append(("listing", None))
        return self._answer(self._listing)

    def option_day(self, contract: OptionContract, trading_date: date):
        self.asked.append(("option", contract))
        return self._answer(self._options.get(contract))


def _session(day: date = FRI) -> ChainSession:
    return chain_session(open_ms(day))


# ── A trading date, an expiry, a contract: how each arrives ───────────────


@pytest.mark.parametrize(
    ("instant_ms", "expected"),
    [
        pytest.param(FRI_NEW_YORK_MIDNIGHT_MS, FRI, id="New York midnight, the first millisecond of the date"),
        pytest.param(FRI_OPEN_MS, FRI, id="the open"),
        pytest.param(FRI_LAST_MS, FRI, id="the last millisecond of the date"),
        pytest.param(
            FRI_NEW_YORK_MIDNIGHT_MS - 1, date(2026, 10, 8), id="a millisecond before midnight is the day before"
        ),
    ],
)
def test_a_trading_date_is_the_new_york_date_the_instant_falls_on(instant_ms: int, expected: date) -> None:
    assert chain_session(instant_ms).trading_date == expected


def test_the_session_is_answered_with_its_own_open_and_close() -> None:
    session = chain_session(FRI_NEW_YORK_MIDNIGHT_MS)

    assert (session.trading_date, session.open_ms, session.close_ms) == (FRI, FRI_OPEN_MS, FRI_CLOSE_MS)


def test_a_half_day_is_answered_with_its_own_early_close() -> None:
    # 2025-11-28 closes at 13:00 New York (EST, five hours behind UTC) = 18:00 UTC.
    session = chain_session(1764306000000)  # 05:00 UTC = New York midnight of the 28th

    assert (session.trading_date, session.open_ms, session.close_ms) == (HALF_DAY, 1764340200000, 1764352800000)


@pytest.mark.parametrize(
    "day", [SAT, CHRISTMAS, THANKSGIVING, date(2026, 10, 11)], ids=["Saturday", "Christmas", "Thanksgiving", "Sunday"]
)
def test_a_date_the_exchange_had_no_session_on_is_refused_with_the_date_in_the_reason(day: date) -> None:
    noon_utc = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)  # the same New York date

    with pytest.raises(NotASession) as caught:
        chain_session(noon_utc)

    assert day.isoformat() in str(caught.value)
    assert caught.value.code == "OPTIONS_BACKTEST_NOT_A_SESSION"


def test_a_date_beyond_what_the_calendar_can_schedule_is_refused_not_crashed() -> None:
    with pytest.raises(NotASession):
        chain_session(MAX_TIMESTAMP_MS)


@pytest.mark.parametrize(
    "instant_ms", [0, 1, 17_999_999], ids=["the epoch", "a millisecond after it", "the last before 1970 New York"]
)
def test_an_instant_in_the_first_hours_of_1970_is_a_session_of_1969_which_this_page_cannot_show(
    instant_ms: int,
) -> None:
    # 1970-01-01 New York midnight (EST, five hours behind UTC) is 05:00 UTC = 5 * 3 600 000 = 18 000 000 ms. Every
    # instant before it is on 1969-12-31, a Wednesday with a session whose open is 10:00 EST = 15:00 UTC the day before
    # = -32 400 000 ms, before the epoch: a wire instant is never negative, so it is refused.
    with pytest.raises(NotASession) as caught:
        chain_session(instant_ms)

    assert caught.value.code == "OPTIONS_BACKTEST_NOT_A_SESSION"
    assert "1969-12-31" in str(caught.value) and "1970" in str(caught.value)


def test_new_years_day_1970_is_the_calendars_holiday_and_the_next_day_is_the_first_session_this_page_can_show() -> None:
    with pytest.raises(NotASession, match="holiday"):
        chain_session(18_000_000)

    # 1970-01-02 New York midnight is 05:00 UTC the next day = 86 400 000 + 18 000 000 = 104 400 000 ms; the session
    # opens at 10:00 EST (the exchange's hour then) = 15:00 UTC = 86 400 000 + 15 * 3 600 000 = 140 400 000 ms.
    session = chain_session(104_400_000)

    assert (session.trading_date, session.open_ms) == (date(1970, 1, 2), 140_400_000)
    assert session.close_ms > session.open_ms


def test_an_expiry_is_its_sixteen_hundred_new_york_anchor_and_names_its_date() -> None:
    assert option_expiry_anchor_ms(FRI) == FRI_CLOSE_MS  # 16:00 EDT = 20:00 UTC
    assert expiry_date_of(FRI_CLOSE_MS) == FRI


@pytest.mark.parametrize(
    "instant_ms",
    [
        pytest.param(FRI_NEW_YORK_MIDNIGHT_MS, id="New York midnight"),
        pytest.param(FRI_CLOSE_MS + 1, id="a millisecond after the anchor"),
        pytest.param(FRI_CLOSE_MS - 1, id="a millisecond before the anchor"),
        pytest.param(1791504000000, id="midnight UTC, the old way to store a date"),
        pytest.param(FRI_CLOSE_MS + 3_600_000, id="an hour after the anchor"),
    ],
)
def test_an_instant_that_is_not_an_expiry_anchor_is_refused(instant_ms: int) -> None:
    with pytest.raises(NotAnExpiryAnchor) as caught:
        expiry_date_of(instant_ms)

    assert caught.value.code == "OPTIONS_BACKTEST_NOT_AN_EXPIRY"


def test_a_contract_is_named_by_its_occ_symbol_and_nothing_else_is_a_contract() -> None:
    assert contract_from_symbol("SPY261009C00602500") == _call("602.5")
    for text in ("", "SPY", "spy261009c00602500", "SPY261009X00602500", "O:SPY261009C00602500"):
        with pytest.raises(NotAContract):
            contract_from_symbol(text)


# ── The day ───────────────────────────────────────────────────────────────

MON = _on(2025, 10, 13, "Mon")
WED = _on(2025, 10, 15, "Wed")
THIRD_FRIDAY = _on(2025, 10, 17, "Fri")  # Oct 2025 starts on a Wednesday: its Fridays are the 3rd, 10th, 17th
SAT_AFTER = _on(2025, 10, 18, "Sat", session=False)
NEXT_FRIDAY = _on(2025, 10, 24, "Fri")
YEAR_END = _on(2025, 12, 31, "Wed")  # the last session of the fourth quarter
FRI_BEFORE = _on(2025, 10, 10, "Fri")
assert 15 <= THIRD_FRIDAY.day <= 21


def _expiry_of(day: date, strikes: Sequence[int | str], right: OptionRight = "call") -> list[ListedContract]:
    return [
        _listed(OptionContract.of(root="SPY", expiry_date=day, right=right, strike=D(strike))) for strike in strikes
    ]


def _the_days_listing() -> StoredListing:
    contracts = [
        # Today's: calls at 600, 601, 602 are standard and 603 is not (ten shares); puts at 600 and 601.
        *_expiry_of(MON, [600, 601, 602]),
        _listed(OptionContract.of(root="SPY", expiry_date=MON, right="call", strike=D(603)), standard=False),
        *_expiry_of(MON, [600, 601], "put"),
        *_expiry_of(WED, [600]),
        *_expiry_of(WED, [600], "put"),
        *_expiry_of(THIRD_FRIDAY, [600, 601]),
        *_expiry_of(NEXT_FRIDAY, [600]),
        *_expiry_of(NEXT_FRIDAY, [600], "put"),
        *_expiry_of(YEAR_END, [600], "put"),
        *_expiry_of(SAT_AFTER, [600]),
        # One that expired before the trading date: a listing as of a date holds none, and it must not crash the answer.
        *_expiry_of(FRI_BEFORE, [600]),
    ]
    return _listing(*contracts, day=MON, left_out=3)


def test_the_day_lists_every_expiry_with_its_series_counts_and_days_to_go() -> None:
    day = describe_day(FakeChainData(listing=_the_days_listing()), underlying="SPY", session=_session(MON))

    assert day.listing is ListingState.STORED and day.listing_problem is None
    summary = {s.expiry: s for s in day.expiries}
    # Ascending by date; the Saturday is a listed date that is no session; the expired one is not here.
    assert [s.expiry for s in day.expiries] == [MON, WED, THIRD_FRIDAY, SAT_AFTER, NEXT_FRIDAY, YEAR_END]
    # Monday the 13th: not the third Friday, not a quarter end, not the last session of its week: a daily.
    assert (summary[MON].tag.series, summary[MON].tag.days_to_expiry, summary[MON].tag.expires_today) == (
        ExpirySeries.DAILY,
        0,
        True,
    )
    assert (summary[WED].tag.series, summary[WED].tag.days_to_expiry) == (ExpirySeries.DAILY, 2)
    # Friday the 17th is the third Friday of October 2025: a monthly (and not merely the last session of its week).
    assert (summary[THIRD_FRIDAY].tag.series, summary[THIRD_FRIDAY].tag.days_to_expiry) == (ExpirySeries.MONTHLY, 4)
    assert (summary[NEXT_FRIDAY].tag.series, summary[NEXT_FRIDAY].tag.days_to_expiry) == (ExpirySeries.WEEKLY, 11)
    # Wednesday 31 December is the last session of the fourth quarter. 13 Oct to 31 Oct is 18 days, November has 30
    # and December's 31 make 79.
    assert (summary[YEAR_END].tag.series, summary[YEAR_END].tag.days_to_expiry) == (ExpirySeries.QUARTERLY, 79)
    assert (summary[SAT_AFTER].tag.series, summary[SAT_AFTER].tag.days_to_expiry) == (ExpirySeries.UNKNOWN, 5)
    assert all(s.tag.version == CLASSIFICATION_VERSION and s.tag.evidence for s in day.expiries)
    assert [s.expiry_ms for s in day.expiries] == [option_expiry_anchor_ms(s.expiry) for s in day.expiries]


def test_the_day_counts_the_calls_and_puts_of_each_expiry_and_how_many_are_standard() -> None:
    day = describe_day(FakeChainData(listing=_the_days_listing()), underlying="SPY", session=_session(MON))

    today = day.expiries[0]
    # Monday's: calls 600, 601, 602 and the non-standard 603 are four calls of which three are standard; two puts.
    assert (today.calls, today.puts, today.standard_calls, today.standard_puts) == (4, 2, 3, 2)
    third_friday = day.expiries[2]
    assert (third_friday.calls, third_friday.puts, third_friday.standard_calls, third_friday.standard_puts) == (
        2,
        0,
        2,
        0,
    )


def test_rows_the_picker_cannot_use_and_contracts_that_expired_are_counted_apart_and_not_crashed_on() -> None:
    day = describe_day(FakeChainData(listing=_the_days_listing()), underlying="SPY", session=_session(MON))

    assert day.rows_left_out_unusable == 3  # the three stored rows the lake could not turn into contracts
    assert day.rows_left_out_expired == 1  # the Friday before the trading date


def test_a_listing_that_left_nothing_out_says_none_were_left_out() -> None:
    day = describe_day(
        FakeChainData(listing=_listing(*_expiry_of(FRI, [600]))), underlying="SPY", session=_session(FRI)
    )

    assert day.rows_left_out_unusable == 0 and day.rows_left_out_expired == 0


def test_a_monthly_on_its_last_day_is_a_monthly_that_expires_today_and_not_a_daily() -> None:
    day = describe_day(
        FakeChainData(listing=_listing(*_expiry_of(THIRD_FRIDAY, [600]), day=THIRD_FRIDAY)),
        underlying="SPY",
        session=_session(THIRD_FRIDAY),
    )

    [only] = day.expiries
    assert (only.tag.series, only.tag.expires_today, only.tag.days_to_expiry) == (ExpirySeries.MONTHLY, True, 0)


def test_before_the_first_tagged_date_every_expiry_is_unknown() -> None:
    before_the_floor = _on(2024, 9, 16, "Mon")  # the tags start at 2024-10-01
    third_friday = _on(2024, 9, 20, "Fri")
    day = describe_day(
        FakeChainData(listing=_listing(*_expiry_of(third_friday, [600]), day=before_the_floor)),
        underlying="SPY",
        session=_session(before_the_floor),
    )

    [only] = day.expiries
    assert only.tag.series is ExpirySeries.UNKNOWN and only.tag.days_to_expiry == 4


def test_a_date_with_no_stored_listing_says_so_and_lists_nothing() -> None:
    day = describe_day(FakeChainData(listing=None), underlying="SPY", session=_session(FRI))

    assert day.listing is ListingState.NOT_STORED and day.expiries == ()
    assert (day.rows_left_out_unusable, day.rows_left_out_expired) == (0, 0)


@pytest.mark.parametrize("error", [OptionListingFileError("bad file"), LakeAdmissionError("no receipt")])
def test_a_stored_listing_the_lake_cannot_read_is_unreadable_with_its_reason_not_missing(error: Exception) -> None:
    day = describe_day(FakeChainData(listing=error), underlying="SPY", session=_session(FRI))

    assert day.listing is ListingState.UNREADABLE and day.expiries == ()
    assert day.listing_problem is not None and type(error).__name__ in day.listing_problem


def test_an_unreachable_catalog_is_not_turned_into_a_state() -> None:
    with pytest.raises(CatalogUnavailableError):
        describe_day(FakeChainData(listing=CatalogUnavailableError("down")), underlying="SPY", session=_session(FRI))


# ── One expiry at one instant ─────────────────────────────────────────────


def _the_friday_listing() -> StoredListing:
    calls = [_listed(_call(strike)) for strike in range(598, 607)]
    calls.append(_listed(_call("602.5"), standard=False))
    puts = [_listed(_put(strike)) for strike in (599, 600, 601, 603, 604)]
    # Another expiry, so the trading date and the expiry are not the same thing.
    return _listing(*calls, *puts, _listed(_call(600, FRI_THIRD)), _listed(_call(601, FRI_THIRD)))


def _the_friday_underlying() -> list[MinuteBar]:
    return list(underlying_bars(FRI, {1: "600.20", 10: "602.40", 11: "603.10", 390: "602.60"}))


def _the_friday_options() -> dict[OptionContract, Sequence[MinuteBar] | Exception]:
    return {
        # Traded at minute 5 and again at 100; and once after the session, at 16:01.
        _call(600): option_bars(FRI, {5: ("3.00", "3.10"), 100: ("2.80", "2.90"), 391: ("0.40", "0.50")}),
        # Captured, and traded only at minute 200.
        _call(601): option_bars(FRI, {200: ("1.40", "1.50")}),
        # 602 was never captured: absent.
        _call("602.5"): option_bars(FRI, {7: ("2.00", "2.00")}),
        # Captured, and never traded.
        _call(603): (),
        _call(604): OptionBarsFileError("damaged"),
        # The bar closing at minute 10 is the instant itself; the one closing at minute 11 is a minute after it.
        _put(603): option_bars(FRI, {10: ("4.30", "4.40")}),
        _put(604): option_bars(FRI, {11: ("5.00", "5.00")}),
    }


def _friday(**overrides) -> FakeChainData:
    arguments = {
        "underlying": _the_friday_underlying(),
        "listing": _the_friday_listing(),
        "options": _the_friday_options(),
    }
    return FakeChainData(**{**arguments, **overrides})


def _expiry(
    data: FakeChainData, *, at: int | None = close_ms(FRI, 10), n: int = 2, expiry: date = FRI, day: date = FRI
):
    return describe_expiry(data, underlying="SPY", session=_session(day), expiry=expiry, at_ms=at, strikes_each_side=n)


def _by_strike(side) -> dict[int, object]:
    return {c.contract.strike_thousandths: c for c in side.contracts}


def test_the_underlying_price_is_the_close_of_its_last_bar_at_or_before_the_instant_with_that_bars_time() -> None:
    at_minute_10 = _expiry(_friday(), at=close_ms(FRI, 10)).underlying_quote
    at_minute_9 = _expiry(_friday(), at=close_ms(FRI, 9)).underlying_quote
    at_the_close = _expiry(_friday(), at=None).underlying_quote

    # A bar closing exactly at the instant counts: it holds the trades before it.
    assert (at_minute_10.state, at_minute_10.price, at_minute_10.price_time_ms) == (
        UnderlyingState.PRICED,
        D("602.40"),
        close_ms(FRI, 10),
    )
    # One minute earlier the last bar is minute 1's, and the price is not carried from minute 10.
    assert (at_minute_9.price, at_minute_9.price_time_ms) == (D("600.20"), close_ms(FRI, 1))
    assert (at_the_close.price, at_the_close.price_time_ms) == (D("602.60"), FRI_CLOSE_MS)


def test_the_price_is_the_close_of_the_bar_and_not_its_open_high_or_low() -> None:
    # A minute that opened at 601.00, reached 603.00, fell to 600.50 and closed at 602.40.
    underlying = list(option_bars(FRI, {10: ("601.00", "603.00", "600.50", "602.40")}))
    options = {_call(602): option_bars(FRI, {10: ("1.00", "5.00", "0.50", "2.40")})}

    chain = _expiry(_friday(underlying=underlying, options=options), at=close_ms(FRI, 10), n=0)

    assert chain.underlying_quote.price == D("602.40")
    assert _by_strike(chain.calls)[602000].price == D("2.40")


def test_the_default_instant_is_the_close_and_a_bar_that_closes_after_it_never_counts() -> None:
    underlying = list(underlying_bars(FRI, {1: "600.20", 390: "602.60", 391: "699.00"}))  # a 16:01 print

    chain = _expiry(_friday(underlying=underlying), at=None)

    assert chain.at_ms == FRI_CLOSE_MS and chain.underlying_quote.price == D("602.60")


def test_a_price_before_the_open_still_counts_as_the_last_trade_by_the_instant_and_shows_its_time() -> None:
    data = _friday(options={_put(603): option_bars(FRI, {-3: ("6.00", "6.00")})})

    chain = _expiry(data, at=close_ms(FRI, 10))

    put = _by_strike(chain.puts)[603000]
    assert (put.state, put.price, put.price_time_ms) == (PriceState.TRADED, D("6.00"), close_ms(FRI, -3))
    assert put.price_time_ms < FRI_OPEN_MS


def test_the_money_is_the_nearest_strike_of_each_right_and_the_two_rights_can_differ() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10))

    # 602.40: calls have 602 (0.40 away). Puts have no 602; 603 is 0.60 away and 601 is 1.40 away.
    assert chain.calls.at_the_money_strike_thousandths == 602000
    assert chain.puts.at_the_money_strike_thousandths == 603000


def test_the_money_is_the_pickers_own_for_calls_and_puts_at_every_instant() -> None:
    for minute, price in ((1, "600.20"), (10, "602.40"), (11, "603.10"), (390, "602.60")):
        chain = _expiry(_friday(), at=close_ms(FRI, minute))
        for side, right in ((chain.calls, "call"), (chain.puts, "put")):
            ladder = [
                listed.contract.strike_thousandths
                for listed in _the_friday_listing().contracts
                if listed.contract.right == right and listed.is_pickable and listed.contract.expiry_date == FRI
            ]
            assert side.at_the_money_strike_thousandths == strike_for(
                AtTheMoney(kind="atm"), ladder_thousandths=ladder, underlying_price=D(price)
            ), (minute, right)


def test_the_money_agrees_with_the_contract_a_leg_with_the_at_the_money_rule_picks() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10))
    for right, side in (("call", chain.calls), ("put", chain.puts)):
        leg = LegSetup(
            leg_id="leg",
            right=right,
            contracts=1,
            expiry=SameDayExpiryRule(kind="same_day"),
            strike=AtTheMoney(kind="atm"),
        )

        picked = pick_contract(leg, listing=_the_friday_listing(), trading_date=FRI, underlying_price=D("602.40"))

        assert picked.contract.strike_thousandths == side.at_the_money_strike_thousandths  # type: ignore[union-attr]


def test_a_price_exactly_between_two_strikes_takes_the_lower_as_the_picker_does() -> None:
    underlying = list(underlying_bars(FRI, {10: "600.50"}))

    chain = _expiry(_friday(underlying=underlying), at=close_ms(FRI, 10))

    # Calls 600 and 601 are both 0.50 away: the lower. Puts have 600 and 601 too.
    assert chain.calls.at_the_money_strike_thousandths == 600000
    assert chain.puts.at_the_money_strike_thousandths == 600000


def test_the_window_is_the_strikes_of_the_standard_ladder_each_side_of_the_money_for_each_right() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=2)

    # Calls, money 602, two each side: 600 601 602 603 604, and the non-standard 602.5 listed inside the range.
    assert [c.contract.strike_thousandths for c in chain.calls.contracts] == [
        600000,
        601000,
        602000,
        602500,
        603000,
        604000,
    ]
    # Puts, money 603 on the ladder 599 600 601 603 604: two below are 601 and 600, and only 604 is above.
    assert [c.contract.strike_thousandths for c in chain.puts.contracts] == [600000, 601000, 603000, 604000]


def test_steps_from_the_money_are_counted_on_the_standard_ladder_and_a_non_standard_contract_has_none() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=2)

    assert {c.contract.strike_thousandths: c.steps_from_the_money for c in chain.calls.contracts} == {
        600000: -2,
        601000: -1,
        602000: 0,
        602500: None,
        603000: 1,
        604000: 2,
    }
    # The 602.5 call is not on the ladder, so it does not move 603 to two steps.
    assert {c.contract.strike_thousandths: c.steps_from_the_money for c in chain.puts.contracts} == {
        600000: -2,
        601000: -1,
        603000: 0,
        604000: 1,
    }
    assert {c.contract.strike_thousandths: c.standard for c in chain.calls.contracts}[602500] is False
    assert all(c.standard for c in chain.puts.contracts)


def test_a_window_that_runs_off_the_end_of_the_ladder_is_shorter_not_padded() -> None:
    high = _expiry(_friday(underlying=list(underlying_bars(FRI, {10: "610.00"}))), at=close_ms(FRI, 10), n=2)
    low = _expiry(_friday(underlying=list(underlying_bars(FRI, {10: "590.00"}))), at=close_ms(FRI, 10), n=2)

    # Calls end at 606 (the money, with the non-standard 602.5 outside the range) and start at 598.
    assert [c.contract.strike_thousandths for c in high.calls.contracts] == [604000, 605000, 606000]
    assert [c.contract.strike_thousandths for c in low.calls.contracts] == [598000, 599000, 600000]
    assert high.calls.at_the_money_strike_thousandths == 606000 and low.calls.at_the_money_strike_thousandths == 598000


def test_no_strikes_each_side_is_the_money_alone() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=0)

    assert [c.contract.strike_thousandths for c in chain.calls.contracts] == [602000]
    assert [c.contract.strike_thousandths for c in chain.puts.contracts] == [603000]


def test_the_default_window_is_ten_each_side_and_the_widest_is_fourteen_which_a_download_holds() -> None:
    assert DEFAULT_STRIKES_EACH_SIDE == 10 and MAX_STRIKES_EACH_SIDE == 14
    wide = FakeChainData(
        underlying=list(underlying_bars(FRI, {10: "602.40"})),
        listing=_listing(*[_listed(_call(strike)) for strike in range(570, 640)]),
    )

    default = describe_expiry(wide, underlying="SPY", session=_session(), expiry=FRI, at_ms=close_ms(FRI, 10))
    widest = describe_expiry(
        wide, underlying="SPY", session=_session(), expiry=FRI, at_ms=close_ms(FRI, 10), strikes_each_side=14
    )

    assert len(default.calls.contracts) == 21 and len(widest.calls.contracts) == 29


@pytest.mark.parametrize("n", [-1, 15, 1000])
def test_a_window_outside_zero_to_fourteen_is_refused(n: int) -> None:
    with pytest.raises(ValueError, match="strikes each side"):
        _expiry(_friday(), n=n)


def test_each_listed_contract_carries_its_price_and_the_time_of_the_bar_it_is_the_close_of() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=2)

    calls = _by_strike(chain.calls)
    # 600 traded at minute 5 and its price at minute 10 is that bar's close, with that bar's time.
    assert (calls[600000].state, calls[600000].price, calls[600000].price_time_ms) == (
        PriceState.TRADED,
        D("3.10"),
        close_ms(FRI, 5),
    )
    # 602.5 traded at minute 7.
    assert (calls[602500].state, calls[602500].price, calls[602500].price_time_ms) == (
        PriceState.TRADED,
        D("2.00"),
        close_ms(FRI, 7),
    )
    puts = _by_strike(chain.puts)
    # The put 603 closed a bar at minute 10 exactly: the instant itself.
    assert (puts[603000].state, puts[603000].price, puts[603000].price_time_ms) == (
        PriceState.TRADED,
        D("4.40"),
        close_ms(FRI, 10),
    )


def test_a_contract_that_traded_only_later_did_not_trade_by_the_earlier_instant_and_has_a_price_at_the_later_one() -> (
    None
):
    early = _expiry(_friday(), at=close_ms(FRI, 10), n=2)
    close = _expiry(_friday(), at=None, n=4)

    # Call 601 first traded at minute 200. Put 604 traded at minute 11, one minute after the earlier instant.
    assert _by_strike(early.calls)[601000].state is PriceState.DID_NOT_TRADE
    assert _by_strike(early.calls)[601000].price is None and _by_strike(early.calls)[601000].price_time_ms is None
    assert _by_strike(early.puts)[604000].state is PriceState.DID_NOT_TRADE
    assert (_by_strike(close.calls)[601000].state, _by_strike(close.calls)[601000].price) == (
        PriceState.TRADED,
        D("1.50"),
    )
    assert _by_strike(close.calls)[601000].price_time_ms == close_ms(FRI, 200)
    assert (_by_strike(close.puts)[604000].price, _by_strike(close.puts)[604000].price_time_ms) == (
        D("5.00"),
        close_ms(FRI, 11),
    )


def test_the_price_at_the_close_is_the_last_trade_by_then_and_never_one_from_the_last_quarter_hour() -> None:
    chain = _expiry(_friday(), at=None, n=4)

    # Call 600 traded at minute 5 and 100, and at 16:01 (minute 391), which is after the close.
    call = _by_strike(chain.calls)[600000]
    assert (call.price, call.price_time_ms) == (D("2.90"), close_ms(FRI, 100))


def test_a_listed_contract_whose_bars_were_never_captured_is_not_stored_and_is_not_given_a_price() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=2)

    never_captured = _by_strike(chain.calls)[602000]
    assert never_captured.state is PriceState.NOT_STORED
    assert (never_captured.price, never_captured.price_time_ms, never_captured.problem) == (None, None, None)


def test_a_contract_captured_with_no_trade_did_not_trade_and_is_told_apart_from_one_not_stored() -> None:
    chain = _expiry(_friday(), at=None, n=4)

    assert _by_strike(chain.calls)[603000].state is PriceState.DID_NOT_TRADE
    assert _by_strike(chain.calls)[602000].state is PriceState.NOT_STORED


@pytest.mark.parametrize("error", [OptionBarsFileError("damaged"), LakeAdmissionError("no receipt")])
def test_a_contract_the_lake_cannot_read_is_unreadable_with_the_lakes_words(error: Exception) -> None:
    chain = _expiry(_friday(options={_call(604): error}), at=close_ms(FRI, 10), n=2)

    unreadable = _by_strike(chain.calls)[604000]
    assert unreadable.state is PriceState.UNREADABLE and unreadable.price is None
    assert unreadable.problem is not None and type(error).__name__ in unreadable.problem


def test_one_unreadable_contract_does_not_stop_the_others_being_read() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=2)

    states = {c.contract.strike_thousandths: c.state for c in chain.calls.contracts}
    assert states[604000] is PriceState.UNREADABLE and states[600000] is PriceState.TRADED


def test_an_unreachable_catalog_while_reading_a_price_is_not_turned_into_a_state() -> None:
    with pytest.raises(CatalogUnavailableError):
        _expiry(_friday(options={_call(600): CatalogUnavailableError("down")}), at=close_ms(FRI, 10), n=2)


@pytest.mark.parametrize(
    ("underlying", "state"),
    [
        pytest.param(None, UnderlyingState.NOT_STORED, id="not stored"),
        pytest.param(UnderlyingDayUnreadable("damaged"), UnderlyingState.UNREADABLE, id="damaged"),
        pytest.param(LakeAdmissionError("no receipt"), UnderlyingState.UNREADABLE, id="not admitted"),
    ],
)
def test_without_an_underlying_price_there_is_no_money_and_the_sides_hold_no_contracts_but_say_what_is_listed(
    underlying: object, state: UnderlyingState
) -> None:
    chain = _expiry(_friday(underlying=underlying), at=close_ms(FRI, 10), n=2)

    assert chain.underlying_quote.state is state and chain.underlying_quote.price is None
    assert chain.underlying_quote.price_time_ms is None
    for side in (chain.calls, chain.puts):
        assert side.at_the_money_strike_thousandths is None and side.contracts == ()
    assert (chain.calls.listed, chain.calls.standard_listed) == (10, 9)
    assert (chain.puts.listed, chain.puts.standard_listed) == (5, 5)


def test_before_the_underlyings_first_bar_there_is_no_price_yet() -> None:
    chain = _expiry(_friday(), at=FRI_OPEN_MS, n=2)

    assert chain.underlying_quote.state is UnderlyingState.NO_BAR_YET and chain.underlying_quote.price is None
    assert chain.calls.at_the_money_strike_thousandths is None and chain.calls.contracts == ()


def test_an_expiry_the_listing_does_not_hold_is_not_listed_and_has_no_tag_or_contracts() -> None:
    chain = _expiry(_friday(), expiry=MON_AFTER)

    assert chain.listing is ListingState.STORED and chain.expiry_listed is False and chain.tag is None
    assert chain.calls.listed == 0 and chain.calls.contracts == ()


def test_an_expiry_before_the_trading_date_is_answered_as_not_listed() -> None:
    chain = _expiry(_friday(listing=_listing(*_expiry_of(FRI_BEFORE, [600]))), expiry=FRI_BEFORE)

    assert chain.expiry_listed is False and chain.calls.listed == 0


def test_a_date_with_no_stored_listing_has_no_expiry_to_show_but_still_has_the_underlyings_price() -> None:
    chain = _expiry(_friday(listing=None), at=close_ms(FRI, 10))

    assert chain.listing is ListingState.NOT_STORED and chain.expiry_listed is None and chain.tag is None
    assert chain.underlying_quote.price == D("602.40")
    assert chain.calls.contracts == () and chain.puts.contracts == ()


def test_a_listing_the_lake_cannot_read_leaves_the_expiry_unknown_with_the_reason() -> None:
    chain = _expiry(_friday(listing=OptionListingFileError("damaged")))

    assert chain.listing is ListingState.UNREADABLE and chain.expiry_listed is None
    assert chain.listing_problem is not None and "OptionListingFileError" in chain.listing_problem


def test_the_expiry_carries_its_tag_and_the_trading_date_and_the_expiry_are_two_separate_inputs() -> None:
    same_day = _expiry(_friday(), expiry=FRI)
    next_monthly = _expiry(_friday(), expiry=FRI_THIRD)

    # Friday the 9th expires today and is a weekly; the 16th, a week on, is the third Friday: a monthly, seven days out.
    assert (same_day.tag.series, same_day.tag.expires_today, same_day.tag.days_to_expiry) == (
        ExpirySeries.WEEKLY,
        True,
        0,
    )
    assert (next_monthly.tag.series, next_monthly.tag.expires_today, next_monthly.tag.days_to_expiry) == (
        ExpirySeries.MONTHLY,
        False,
        7,
    )
    # Changing the expiry moved neither the date nor the underlying's price; changing nothing else changed the contracts.
    assert (next_monthly.session, next_monthly.underlying_quote) == (same_day.session, same_day.underlying_quote)
    assert [c.contract.strike_thousandths for c in next_monthly.calls.contracts] == [600000, 601000]
    assert (next_monthly.calls.listed, next_monthly.puts.listed) == (2, 0)


def test_the_same_expiry_on_another_trading_date_is_read_from_that_dates_own_data() -> None:
    thursday = _on(2026, 10, 8, "Thu")
    other = FakeChainData(
        underlying=list(underlying_bars(thursday, {10: "598.30"})),
        listing=_listing(*[_listed(_call(strike)) for strike in range(596, 602)], day=thursday),
    )

    chain = describe_expiry(
        other,
        underlying="SPY",
        session=_session(thursday),
        expiry=FRI,
        at_ms=close_ms(thursday, 10),
        strikes_each_side=1,
    )

    # 598.30 is nearest 598; the expiry (Friday the 9th) is a day after this trading date.
    assert chain.calls.at_the_money_strike_thousandths == 598000
    assert chain.tag.days_to_expiry == 1 and chain.tag.expires_today is False  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "at",
    [FRI_OPEN_MS - 1, FRI_CLOSE_MS + 1, 0, FRI_OPEN_MS + 24 * 3_600_000],
    ids=["a millisecond before the open", "a millisecond after the close", "the epoch", "the next day"],
)
def test_an_instant_outside_the_session_is_refused(at: int) -> None:
    with pytest.raises(InstantOutsideSession) as caught:
        _expiry(_friday(), at=at)

    assert caught.value.code == "OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION"


def test_the_open_and_the_close_themselves_are_inside_the_session() -> None:
    assert _expiry(_friday(), at=FRI_OPEN_MS).at_ms == FRI_OPEN_MS
    assert _expiry(_friday(), at=FRI_CLOSE_MS).at_ms == FRI_CLOSE_MS


def test_a_half_days_default_instant_is_its_own_early_close() -> None:
    half = _session(HALF_DAY)
    data = FakeChainData(underlying=list(underlying_bars(HALF_DAY, {1: "600.00"})), listing=_listing(day=HALF_DAY))

    chain = describe_expiry(data, underlying="SPY", session=half, expiry=HALF_DAY)

    assert chain.at_ms == 1764352800000


def test_nothing_is_read_for_a_contract_outside_the_window() -> None:
    data = _friday()

    _expiry(data, at=close_ms(FRI, 10), n=0)

    asked = [contract.strike_thousandths for kind, contract in data.asked if kind == "option"]
    assert sorted(asked) == [602000, 603000]


def test_a_standard_contract_of_another_root_is_not_on_the_ladder_and_does_not_move_the_money_or_the_steps() -> None:
    other_root = OptionContract.of(root="SPYW", expiry_date=FRI, right="call", strike=602)
    listing = _listing(_listed(_call(600)), _listed(other_root), _listed(_call(604)))
    data = FakeChainData(underlying=list(underlying_bars(FRI, {10: "602.10"})), listing=listing)

    chain = _expiry(data, at=close_ms(FRI, 10), n=1)

    # 602.10 is 2.10 from SPY's 600 and 1.90 from its 604, so the money is 604. The SPYW 602 (0.10 away) is not
    # SPY's: with it on the ladder the money would be 602.
    assert chain.calls.at_the_money_strike_thousandths == 604000
    assert {c.contract.occ_symbol: c.steps_from_the_money for c in chain.calls.contracts} == {
        "SPY261009C00600000": -1,
        "SPYW261009C00602000": None,
        "SPY261009C00604000": 0,
    }


def test_the_money_strike_is_in_dollars_as_the_contract_says_it_and_in_thousandths() -> None:
    chain = _expiry(_friday(), at=close_ms(FRI, 10), n=0)

    # 603 for the puts, and the calls' 602 (602000 thousandths).
    assert (chain.puts.at_the_money_strike, chain.puts.at_the_money_strike_thousandths) == (D(603), 603000)
    assert (chain.calls.at_the_money_strike, chain.calls.at_the_money_strike_thousandths) == (D(602), 602000)


# ── The window never holds more than a download takes ─────────────────────


def _wide_listing(*non_standard: str) -> StoredListing:
    """Standard calls and puts at every dollar from 570 to 639, and non-standard calls at the strikes named."""
    standard = [_listed(_call(strike)) for strike in range(570, 640)]
    standard += [_listed(_put(strike)) for strike in range(570, 640)]
    return _listing(*standard, *[_listed(_call(strike), standard=False) for strike in non_standard])


def _wide(non_standard: tuple[str, ...], n: int) -> tuple[FakeChainData, Callable[[], ChainExpiry]]:
    data = FakeChainData(underlying=list(underlying_bars(FRI, {10: "602.40"})), listing=_wide_listing(*non_standard))
    return data, lambda: _expiry(data, at=close_ms(FRI, 10), n=n)


def test_a_window_of_exactly_the_cap_is_answered_with_its_non_standard_contracts_inside_it() -> None:
    # Money 602 (0.40 from 602.40, against 0.60 for 603) on both ladders: 14 each side is 588 to 616, 29 strikes a
    # right, 58 in all. The non-standard calls at 602.5 and 603.5 are inside that range: 60 contracts, the cap.
    _, ask = _wide(("602.5", "603.5"), 14)

    chain = ask()

    assert (len(chain.calls.contracts), len(chain.puts.contracts)) == (31, 29)
    assert len(chain.calls.contracts) + len(chain.puts.contracts) == MAX_WINDOW_CONTRACTS


def test_a_window_one_over_the_cap_is_refused_before_any_contract_is_read_and_a_narrower_one_is_answered() -> None:
    # The same 58 standard contracts and three non-standard calls (601.5, 602.5, 603.5), all inside 588 to 616: 61.
    data, ask = _wide(("601.5", "602.5", "603.5"), 14)

    with pytest.raises(WindowTooWide) as caught:
        ask()

    assert caught.value.code == "OPTIONS_BACKTEST_WINDOW_TOO_WIDE"
    assert "61" in str(caught.value) and "60" in str(caught.value)
    assert [kind for kind, _ in data.asked if kind == "option"] == []  # refused before one file was opened

    # 13 each side is 589 to 615, 27 strikes a right: 54 standard and the same three inside it, 57.
    narrower = _expiry(data, at=close_ms(FRI, 10), n=13)
    assert len(narrower.calls.contracts) + len(narrower.puts.contracts) == 57


# ── What cannot be read is told without a file ────────────────────────────

_A_PATH = "/srv/lake/raw/option/usa/minute/spy/spy261009c00604000/20261009_trade.zip"


@pytest.mark.parametrize(
    "error",
    [
        OptionBarsFileError(f"{_A_PATH}: not a readable file (BadZipFile: File is not a zip file)"),
        LakeAdmissionError(f"{_A_PATH}: no committed catalog receipt for these bytes; retry capture"),
    ],
    ids=["a damaged file", "bytes the catalog does not admit"],
)
def test_a_contract_that_cannot_be_read_says_which_and_why_in_words_without_the_path(error: Exception) -> None:
    chain = _expiry(_friday(options={_call(604): error}), at=close_ms(FRI, 10), n=2)

    problem = _by_strike(chain.calls)[604000].problem

    assert problem is not None
    assert "SPY261009C00604000" in problem and "2026-10-09" in problem and type(error).__name__ in problem
    assert "/srv" not in problem and ".zip" not in problem


@pytest.mark.parametrize(
    "error",
    [
        OptionListingFileError(f"{_A_PATH}: expected one member"),
        LakeAdmissionError(f"{_A_PATH}: no root identity for committed lake admission"),
    ],
    ids=["a damaged file", "bytes the catalog does not admit"],
)
def test_a_listing_that_cannot_be_read_says_which_and_why_in_words_without_the_path(error: Exception) -> None:
    day = describe_day(FakeChainData(listing=error), underlying="SPY", session=_session(FRI))
    chain = _expiry(_friday(listing=error))

    for problem in (day.listing_problem, chain.listing_problem):
        assert problem is not None
        assert "SPY listing" in problem and "2026-10-09" in problem and type(error).__name__ in problem
        assert "/srv" not in problem and ".zip" not in problem


@pytest.mark.parametrize(
    "error",
    [
        UnderlyingDayUnreadable(
            f"the stored SPY minute file cannot be decoded (OSError: [Errno 21] Is a directory: '{_A_PATH}')"
        ),
        LakeAdmissionError(f"{_A_PATH}: cannot verify committed lake root identity"),
    ],
    ids=["a damaged file", "bytes the catalog does not admit"],
)
def test_an_underlying_that_cannot_be_read_says_which_and_why_in_words_without_the_path(error: Exception) -> None:
    quote = _expiry(_friday(underlying=error)).underlying_quote

    assert quote.state is UnderlyingState.UNREADABLE and quote.problem is not None
    assert (
        "SPY minute bars" in quote.problem and "2026-10-09" in quote.problem and type(error).__name__ in quote.problem
    )
    assert "/srv" not in quote.problem and ".zip" not in quote.problem


def test_the_lakes_own_words_for_what_cannot_be_read_go_to_the_log_where_the_path_is_wanted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = OptionBarsFileError(f"{_A_PATH}: not a readable file")

    with caplog.at_level(logging.WARNING, logger="app.options_backtest.past_chain"):
        _expiry(_friday(options={_call(604): error}), at=close_ms(FRI, 10), n=2)

    [record] = [r for r in caplog.records if r.name == "app.options_backtest.past_chain"]
    assert record.error_type == "OptionBarsFileError" and _A_PATH in record.error  # type: ignore[attr-defined]


# ── An instant is the close of a minute ───────────────────────────────────


def test_an_instant_off_a_whole_minute_is_refused_with_its_own_code_and_a_whole_minute_is_not() -> None:
    # 09:30:30 is the open + 30 000 ms: inside the session and half way through the minute whose bar closes at 09:31:00.
    with pytest.raises(InstantNotOnAMinute) as caught:
        _expiry(_friday(), at=FRI_OPEN_MS + 30_000)

    assert caught.value.code == "OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE"
    assert str(FRI_OPEN_MS + 30_000) in str(caught.value)
    assert _expiry(_friday(), at=FRI_OPEN_MS + 60_000).at_ms == FRI_OPEN_MS + 60_000


def test_a_millisecond_off_the_minute_inside_the_session_is_refused_as_off_a_minute() -> None:
    with pytest.raises(InstantNotOnAMinute):
        _expiry(_friday(), at=close_ms(FRI, 10) + 1)
