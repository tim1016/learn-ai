"""What a run does with data that is not there, a stop request, and a run it cannot start.

"Not captured" (``None``) and "captured, and nothing traded" (an empty sequence) are different answers: the first stops
the run and names what is missing, the second is a fact about the market that the replay handles.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.options_backtest.fees import FeeRateNotPublished
from app.options_backtest.picking import Listing
from app.options_backtest.results import EventKind, SessionStatus, SkipReason
from app.options_backtest.run import RunStopped, SellingNotReplayed, SessionDataMissing, run_sessions
from app.options_backtest.session_market import MalformedBars
from app.options_backtest.setup import FeePolicy
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import (
    FRI,
    THU,
    WED,
    Day,
    FakeRunData,
    changed,
    contract_on,
    fake_data,
    flat,
    listing_on,
    option_bars,
    steady,
    underlying_bars,
)

CALL_500 = ("call", 500)
PUT_500 = ("put", 500)


def _day() -> Day:
    return Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 60), 61: ("2.50", "2.50")}})


def test_a_session_with_no_underlying_captured_stops_the_run_and_says_so() -> None:
    data = fake_data({WED: _day(), THU: _day(), FRI: _day()}, without_underlying=(THU,))

    with pytest.raises(SessionDataMissing) as raised:
        run_sessions(setup(leg()), [WED, THU, FRI], data)

    missing = raised.value
    assert (missing.trading_date, missing.underlying, missing.listing, missing.contracts) == (THU, True, False, ())
    assert "2026-10-08" in str(missing)
    # It stopped there: Friday was never asked about.
    assert [question[1] for question in data.asked if question[0] in ("underlying", "listing")][-1] == THU
    assert not any(question[1] == FRI for question in data.asked)


def test_a_session_with_no_listing_captured_stops_the_run_and_says_so() -> None:
    data = fake_data({WED: _day(), THU: _day()}, without_listing=(THU,))

    with pytest.raises(SessionDataMissing) as raised:
        run_sessions(setup(leg()), [WED, THU], data)

    assert (raised.value.trading_date, raised.value.underlying, raised.value.listing) == (THU, False, True)


def test_a_session_missing_both_names_both() -> None:
    data = fake_data({THU: _day()}, without_underlying=(THU,), without_listing=(THU,))

    with pytest.raises(SessionDataMissing) as raised:
        run_sessions(setup(leg()), [THU], data)

    assert (raised.value.underlying, raised.value.listing, raised.value.contracts) == (True, True, ())


def test_a_needed_contract_that_was_not_captured_stops_the_run_and_is_named() -> None:
    no_options = Day(underlying=steady("500.00"))
    data = fake_data({WED: _day(), THU: no_options})

    with pytest.raises(SessionDataMissing) as raised:
        run_sessions(setup(leg()), [WED, THU], data)

    missing = raised.value
    thursday_call = contract_on(THU, "call", 500)
    assert (missing.trading_date, missing.underlying, missing.listing) == (THU, False, False)
    assert missing.contracts == (thursday_call,)
    assert thursday_call.occ_symbol in str(missing) and "2026-10-08" in str(missing)


def test_every_needed_contract_that_was_not_captured_is_named_in_the_order_of_their_symbols() -> None:
    data = fake_data({THU: Day(underlying=steady("500.00"))})

    with pytest.raises(SessionDataMissing) as raised:
        run_sessions(setup(leg("a", "put"), leg("b", "call")), [THU], data)

    assert raised.value.contracts == (contract_on(THU, "call", 500), contract_on(THU, "put", 500))


def test_a_needed_contract_captured_as_empty_never_traded_so_its_entry_is_dropped_and_the_run_goes_on() -> None:
    day = Day(underlying=steady("500.00"), options={CALL_500: {}})
    data = fake_data({THU: day})

    result = run_sessions(setup(leg()), [THU], data)

    [session] = result.sessions
    assert session.status is SessionStatus.NO_TRADE
    assert [event.kind for event in session.outcome.events if event.kind is EventKind.ENTRY_DROPPED] == [
        EventKind.ENTRY_DROPPED
    ]
    assert session.cash_after_usd == session.cash_before_usd == Decimal("10000")


def test_an_underlying_captured_as_empty_never_traded_so_the_session_is_skipped_and_the_run_goes_on() -> None:
    data = FakeRunData(underlying={THU: ()}, listings={THU: listing_on(THU)})

    result = run_sessions(setup(leg()), [THU], data)

    [session] = result.sessions
    assert (session.status, session.outcome.skip_reason) == (SessionStatus.SKIPPED, SkipReason.UNDERLYING_DID_NOT_TRADE)
    assert session.pins.option_days == ()


def test_underlying_bars_that_repeat_a_minute_are_refused_and_not_averaged_or_overwritten() -> None:
    twice = list(underlying_bars(THU, steady("500.00"))) + list(underlying_bars(THU, {5: "999.00"}))
    twice.sort(key=lambda bar: bar.close_ms)
    data = FakeRunData(underlying={THU: twice}, listings={THU: listing_on(THU)})

    with pytest.raises(MalformedBars, match="twice"):
        run_sessions(setup(leg()), [THU], data)

    # It was refused before any contract was read on the strength of it.
    assert [question[0] for question in data.asked] == ["underlying", "listing"]


def test_a_contract_day_that_repeats_a_minute_is_refused() -> None:
    call = contract_on(THU, "call", 500)
    repeated = option_bars(THU, flat("2.00", 5, 61)) + option_bars(THU, {10: ("9.00", "9.00")})
    data = FakeRunData(
        underlying={THU: underlying_bars(THU, steady("500.00"))},
        listings={THU: listing_on(THU)},
        options={(call, THU): sorted(repeated, key=lambda bar: bar.close_ms)},
    )

    with pytest.raises(MalformedBars, match="twice"):
        run_sessions(setup(leg()), [THU], data)


def test_a_listing_of_another_underlying_is_refused_and_not_read_as_nothing_listed() -> None:
    # A QQQ chain handed to a setup that trades SPY would make every pick a quiet "expiry not listed".
    foreign = Listing(underlying="QQQ", as_of=THU, contracts=(), complete=True)
    data = FakeRunData(underlying={THU: underlying_bars(THU, steady("500.00"))}, listings={THU: foreign})

    with pytest.raises(ValueError, match="QQQ"):
        run_sessions(setup(leg()), [THU], data)


def test_a_stop_request_between_sessions_raises_and_returns_nothing() -> None:
    data = fake_data({WED: _day(), THU: _day(), FRI: _day()})
    asked = iter([False, False, True])

    with pytest.raises(RunStopped) as raised:
        run_sessions(setup(leg()), [WED, THU, FRI], data, should_stop=lambda: next(asked))

    assert raised.value.sessions_done == 2
    # The two sessions before the request were replayed; the third was never read.
    assert {question[1] for question in data.asked} == {WED, THU}


def test_a_stop_request_before_the_first_session_reads_nothing() -> None:
    data = fake_data({WED: _day()})

    with pytest.raises(RunStopped) as raised:
        run_sessions(setup(leg()), [WED], data, should_stop=lambda: True)

    assert raised.value.sessions_done == 0
    assert data.asked == []


def test_progress_is_reported_after_each_session_with_the_count_done_and_the_total() -> None:
    data = fake_data({WED: _day(), THU: _day(), FRI: _day()})
    seen: list[tuple[int, int, date]] = []

    run_sessions(
        setup(leg()), [WED, THU, FRI], data, on_session=lambda done, total, day: seen.append((done, total, day))
    )

    assert seen == [(1, 3, WED), (2, 3, THU), (3, 3, FRI)]


def test_a_run_over_no_sessions_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one session"):
        run_sessions(setup(leg()), [], FakeRunData())


@pytest.mark.parametrize(
    ("sessions", "words"),
    [
        ([THU, WED], "date order"),
        ([WED, WED], "date order"),
        ([date(2026, 10, 10)], "not a NYSE session"),  # a Saturday
        ([WED, date(2026, 11, 26)], "not a NYSE session"),  # Thanksgiving
    ],
)
def test_sessions_out_of_order_repeated_or_closed_are_refused_before_anything_is_read(
    sessions: list[date], words: str
) -> None:
    data = fake_data({WED: _day(), THU: _day()})

    with pytest.raises(ValueError, match=words):
        run_sessions(setup(leg()), sessions, data)

    assert data.asked == []


def test_estimated_fees_on_a_date_without_published_rates_are_refused_before_anything_is_read() -> None:
    # 2026-06-30 is the last day without a CAT rate; 2026-07-01 is the first with one. The check is on every date of the
    # run, not only on one that happens to fill, and it is made before the first session is read.
    june_30, july_1 = date(2026, 6, 30), date(2026, 7, 1)
    data = FakeRunData()

    with pytest.raises(FeeRateNotPublished) as raised:
        run_sessions(changed(setup(leg()), fees=FeePolicy(kind="estimated")), [june_30, july_1], data)

    assert (raised.value.trading_date, raised.value.components) == (june_30, ("cat",))
    assert data.asked == []


def test_a_setup_with_a_sold_leg_is_refused_before_anything_is_read_until_selling_is_replayed() -> None:
    # No path may replay a sold leg as if it were bought (#3106 replays it).
    data = FakeRunData()
    selling = setup(
        leg("long_call"),
        {**leg("short_call"), "side": "sell"},
        leg("long_put", "put"),
        {**leg("short_put", "put"), "side": "sell"},
    )

    with pytest.raises(SellingNotReplayed) as raised:
        run_sessions(selling, [WED], data)

    assert raised.value.leg_ids == ("short_call", "short_put")
    assert data.asked == []
