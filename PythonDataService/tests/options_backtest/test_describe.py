"""What an owner reads about a setup before pressing Start (#3030).

The clock a description is read against is built from the calendar, and every
expected clock time is worked out from it with datetime arithmetic, so no
session time is written as a literal here. Tests assert what a sentence says
(the distances, the clock times, the amounts, what is left unsaid), not its
whole wording.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.describe import describe_setup
from app.options_backtest.saved_setups import EXAMPLE_SETUPS
from app.options_backtest.session_clock import SessionClock
from app.options_backtest.setup import MS_PER_MINUTE
from app.utils.timestamps import ny_datetime
from tests.options_backtest.setup_fixtures import leg as _leg
from tests.options_backtest.setup_fixtures import setup as _setup

# 2026-10-09 is a full session.
_FULL_DAY = session_window_for_date(date(2026, 10, 9))
_OPENED_ET = ny_datetime(_FULL_DAY.open_ms_utc)
_CLOSED_ET = ny_datetime(_FULL_DAY.close_ms_utc)
_CLOCK = SessionClock(
    open_minute_et=_OPENED_ET.hour * 60 + _OPENED_ET.minute,
    close_minute_et=_CLOSED_ET.hour * 60 + _CLOSED_ET.minute,
)
_SESSION_MINUTES = _CLOCK.close_minute_et - _CLOCK.open_minute_et


def _after_open(minutes: int) -> str:
    return (_OPENED_ET + timedelta(minutes=minutes)).strftime("%H:%M")


def _before_close(minutes: int) -> str:
    return (_CLOSED_ET - timedelta(minutes=minutes)).strftime("%H:%M")


def _after_open_time(minutes: int) -> dict[str, Any]:
    return {"anchor": "open", "offset_ms": minutes * MS_PER_MINUTE}


def _before_close_time(minutes: int) -> dict[str, Any]:
    return {"anchor": "close", "offset_ms": minutes * MS_PER_MINUTE}


def _leg_text(**changes: Any) -> str:
    [leg] = describe_setup(_setup(legs=[_leg(**changes)]), full_session=_CLOCK).legs
    return " ".join(leg.sentences)


def _window(**changes: Any) -> str:
    return " ".join(describe_setup(_setup(**changes), full_session=_CLOCK).window)


def _costs(**changes: Any) -> str:
    return " ".join(describe_setup(_setup(**changes), full_session=_CLOCK).costs)


def test_the_window_gives_each_time_as_a_distance_and_as_the_clock_on_a_full_day() -> None:
    window = _window()

    assert "SPY" in window
    assert re.search(r"entry is read 5 minutes after the open", window) and _after_open(5) in window
    assert re.search(r"exit is read 46 minutes before the close", window) and _before_close(46) in window


def test_the_window_says_the_times_are_when_a_rule_is_read_and_an_order_fills_a_minute_on() -> None:
    window = _window()

    assert "next minute's opening trade" in window
    # The times are not the fills: the window never says the setup "starts" or "buys" at them.
    assert not re.search(r"\b(starts|buys)\b", window, re.IGNORECASE)
    assert re.search(r"at the exit.*sold.*cancelled.*nothing more is bought", window, re.IGNORECASE)


def test_the_window_says_a_buy_is_dropped_and_a_sale_is_tried_again_when_the_contract_does_not_trade() -> None:
    window = _window()

    assert re.search(r"a buy is dropped if its contract does not trade", window, re.IGNORECASE)
    assert re.search(r"a sale is tried each minute until it does", window, re.IGNORECASE)


def test_a_re_entry_cutoff_at_the_open_is_said_as_the_open() -> None:
    window = _window(no_reentry_after=_after_open_time(0))

    assert "later than the open" in window
    assert _after_open(0) in window


def test_a_re_entry_cutoff_says_it_leaves_first_entries_alone_and_no_cutoff_says_nothing_of_it() -> None:
    assert "First entries are not affected" in _window(no_reentry_after=_after_open_time(30))
    assert "First entries" not in _window()


def test_the_window_says_what_an_early_close_does_to_it() -> None:
    window = _window()

    assert re.search(r"closes early", window)
    assert re.search(r"keep their distance from the open and the close", window)
    assert re.search(r"no longer fit is skipped", window)


def test_a_re_entry_on_momentum_says_it_takes_a_new_starting_premium() -> None:
    on_momentum = _leg_text(
        momentum={"kind": "percent_up", "value": "25"},
        stop_loss={"kind": "percent", "value": "25"},
        reentry_on_stop={"kind": "on_momentum", "count": 2},
    )
    at_once = _leg_text(
        stop_loss={"kind": "percent", "value": "25"},
        reentry_on_stop={"kind": "at_once", "count": 2},
    )

    assert "new starting premium" in on_momentum
    assert "new starting premium" not in at_once
    assert "in a session" in on_momentum and "in a session" in at_once


@pytest.mark.parametrize(
    ("field", "time", "said"),
    [
        ("exit", _after_open_time(_SESSION_MINUTES + 1), "after the close"),
        ("entry", _before_close_time(_SESSION_MINUTES + 1), "before the open"),
    ],
)
def test_a_time_outside_a_full_session_is_not_given_a_clock_time_that_would_mislead(
    field: str, time: dict[str, Any], said: str
) -> None:
    window = _window(**{field: time})

    assert said in window
    # The clock face the arithmetic would give, one minute past the close or before the open, is not printed.
    assert _after_open(_SESSION_MINUTES + 1) not in window
    assert _before_close(_SESSION_MINUTES + 1) not in window


# (entry, exit, which parts the summary names), worked out by hand on a session of _SESSION_MINUTES minutes.
_WINDOWS: list[tuple[dict[str, Any], dict[str, Any], set[str]]] = [
    (_after_open_time(5), _before_close_time(46), set()),
    (_after_open_time(1), _before_close_time(1), set()),
    # An entry counted back from the close can land on the open, or before it.
    (_before_close_time(_SESSION_MINUTES), _before_close_time(46), {"entry"}),
    (_before_close_time(_SESSION_MINUTES + 210), _before_close_time(46), {"entry"}),
    # An exit counted from the open can land on the close, or after it.
    (_after_open_time(5), _after_open_time(_SESSION_MINUTES), {"exit"}),
    (_after_open_time(5), _after_open_time(_SESSION_MINUTES + 210), {"exit"}),
    # The two counted from opposite ends can meet or cross in the middle.
    (_after_open_time(_SESSION_MINUTES // 2), _before_close_time(_SESSION_MINUTES // 2), {"order"}),
    (_after_open_time(_SESSION_MINUTES // 2 + 60), _before_close_time(_SESSION_MINUTES // 2 + 60), {"order"}),
    (_before_close_time(_SESSION_MINUTES + 210), _after_open_time(_SESSION_MINUTES + 210), {"entry", "exit"}),
]
_TROUBLES = {
    "entry": r"entry is too close to the open",
    "exit": r"exit is too close to the close",
    "order": r"exit is not after the entry",
}


def _named_troubles(notices: tuple[str, ...]) -> set[str]:
    return {name for name, pattern in _TROUBLES.items() if any(re.search(pattern, notice) for notice in notices)}


@pytest.mark.parametrize(("entry", "exit_", "troubles"), _WINDOWS)
def test_a_window_a_full_day_cannot_trade_is_flagged_with_the_part_that_is_the_trouble(
    entry: dict[str, Any], exit_: dict[str, Any], troubles: set[str]
) -> None:
    notices = describe_setup(_setup(entry=entry, exit=exit_), full_session=_CLOCK).notices

    assert _named_troubles(notices) == troubles
    if troubles:
        assert re.search(r"would not trade", notices[0])
    else:
        assert notices == ()


@pytest.mark.parametrize(("entry", "exit_", "troubles"), _WINDOWS)
def test_the_summary_warns_exactly_when_the_replay_would_skip_the_day(
    entry: dict[str, Any], exit_: dict[str, Any], troubles: set[str]
) -> None:
    setup = _setup(entry=entry, exit=exit_)
    fits = setup.window_fits(
        session_open_ms=_CLOCK.open_minute_et * MS_PER_MINUTE, session_close_ms=_CLOCK.close_minute_et * MS_PER_MINUTE
    )

    assert (describe_setup(setup, full_session=_CLOCK).notices == ()) is fits


def test_a_cutoff_for_re_entry_is_said_with_its_distance_and_clock_and_a_missing_one_is_not_mentioned() -> None:
    with_cutoff = _window(no_reentry_after=_before_close_time(60))
    without = _window()

    assert "60 minutes before the close" in with_cutoff and _before_close(60) in with_cutoff
    assert "re-entry" not in without


def test_every_leg_gets_one_sentence_that_names_it() -> None:
    legs = describe_setup(
        _setup(legs=[_leg(leg_id="call"), _leg(leg_id="put", right="put", contracts=3)]), full_session=_CLOCK
    ).legs

    assert [leg.leg_id for leg in legs] == ["call", "put"]
    assert re.search(r"\b1 at-the-money call\b", " ".join(legs[0].sentences))
    assert re.search(r"\b3 at-the-money puts\b", " ".join(legs[1].sentences))


@pytest.mark.parametrize(
    ("strike", "said"),
    [
        ({"kind": "atm"}, "at-the-money"),
        ({"kind": "atm_offset", "steps": 2}, "2 strikes above the money"),
        ({"kind": "atm_offset", "steps": -1}, "1 strike below the money"),
    ],
)
def test_a_strike_rule_says_where_the_strike_sits(strike: dict[str, Any], said: str) -> None:
    assert said in _leg_text(strike=strike)


@pytest.mark.parametrize(
    ("expiry", "said"),
    [
        ({"kind": "same_day"}, "on the trading date itself"),
        ({"kind": "this_week"}, "last trading day of its week"),
        ({"kind": "min_days", "days": 3}, "at least 3 calendar days"),
        ({"kind": "min_days", "days": 1}, "at least 1 calendar day "),
        ({"kind": "min_days", "days": 0}, "today included"),
    ],
)
def test_an_expiry_rule_says_which_expiry_is_wanted(expiry: dict[str, Any], said: str) -> None:
    assert said in _leg_text(expiry=expiry)


def test_the_this_week_rule_is_said_as_it_works_not_as_an_expiry_in_this_week() -> None:
    text = _leg_text(expiry={"kind": "this_week"})

    # The nearest listed expiry on a week's last trading day: another week for an underlying with none this week,
    # and the same day when today is that day.
    assert "nearest listed expiry" in text and "today included" in text
    assert "this week" not in text


@pytest.mark.parametrize(
    ("momentum", "pattern"),
    [
        ({"kind": "percent_up", "value": "25"}, r"closes at least 25% above where its premium stood"),
        ({"kind": "percent_down", "value": "10.5"}, r"closes at least 10\.5% below where its premium stood"),
        ({"kind": "points_up", "value": "0.5"}, r"closes at least 0\.5 points above where its premium stood"),
        ({"kind": "points_down", "value": "1.5"}, r"closes at least 1\.5 points below where its premium stood"),
    ],
)
def test_momentum_says_which_way_how_far_and_from_where(momentum: dict[str, Any], pattern: str) -> None:
    text = _leg_text(momentum=momentum)

    assert re.search(pattern, text)
    assert "entry time" in text


def test_a_leg_without_momentum_buys_when_the_entry_is_read() -> None:
    text = _leg_text()

    assert "as soon as the entry is read" in text
    assert "closes" not in text


@pytest.mark.parametrize(
    ("stop_loss", "pattern"),
    [
        ({"kind": "percent", "value": "25"}, r"closes at least 25% below what was paid"),
        ({"kind": "points", "value": "0.4"}, r"closes at least 0\.4 points below what was paid"),
    ],
)
def test_a_stop_loss_says_a_minute_must_close_that_far_below_what_was_paid(
    stop_loss: dict[str, Any], pattern: str
) -> None:
    assert re.search(pattern, _leg_text(stop_loss=stop_loss))


def test_a_leg_with_no_rules_of_its_own_mentions_none() -> None:
    text = _leg_text()

    assert "%" not in text
    assert "points" not in text
    assert "below what was paid" not in text
    assert "above" not in text
    assert "again" not in text


@pytest.mark.parametrize(
    ("reentry", "pattern"),
    [
        ({"kind": "at_once", "count": 1}, r"fresh contract.*buys it at once, up to 1 time\b"),
        ({"kind": "at_once", "count": 3}, r"fresh contract.*buys it at once, up to 3 times\b"),
        ({"kind": "on_momentum", "count": 2}, r"fresh contract.*waits for momentum again, up to 2 times\b"),
    ],
)
def test_a_re_entry_picks_a_fresh_contract_and_says_how_it_comes_back_and_how_many_times(
    reentry: dict[str, Any], pattern: str
) -> None:
    text = _leg_text(
        momentum={"kind": "percent_up", "value": "25"},
        stop_loss={"kind": "percent", "value": "25"},
        reentry_on_stop=reentry,
    )

    assert re.search(pattern, text)


def test_square_off_says_what_one_stop_closes() -> None:
    partial = " ".join(describe_setup(_setup(square_off="partial"), full_session=_CLOCK).square_off)
    complete = " ".join(describe_setup(_setup(square_off="complete"), full_session=_CLOCK).square_off)

    assert "only" in partial and "every" not in partial
    assert "every" in complete and "only" not in complete


def test_costs_say_the_slippage_the_price_step_the_cash_and_the_fee_policy() -> None:
    costs = _costs(slippage_percent="0.3000", price_step_usd="0.05")

    assert "0.3%" in costs
    assert "0.05" in costs
    assert "10,000.00" in costs
    assert "not charged" in costs


@pytest.mark.parametrize(
    ("fees", "said"),
    [
        ({"kind": "per_contract", "per_contract_usd": "0.65"}, "0.65"),
        ({"kind": "estimated"}, "estimated"),
        ({"kind": "not_charged"}, "not charged"),
    ],
)
def test_each_fee_policy_is_named(fees: dict[str, Any], said: str) -> None:
    assert said in _costs(fees=fees)


def test_estimated_fees_say_they_need_published_rates_and_which_sessions_cannot_run_without_them() -> None:
    estimated = _costs(fees={"kind": "estimated"})
    not_charged = _costs()

    assert "published option fee rates" in estimated and "before those rates exist" in estimated
    assert "cannot be run" in estimated
    # No date is written: this module does not know the fee model's first day.
    assert not re.search(r"\b(19|20)\d\d\b", estimated)
    assert "published" not in not_charged and "cannot" not in not_charged


def test_no_slippage_is_said_as_no_slippage_not_as_a_zero_percent_move() -> None:
    costs = _costs(slippage_percent="0")

    assert "0%" not in costs
    assert "no slippage" in costs.lower()


def test_the_example_named_no_re_entry_says_nothing_of_re_entry_and_the_other_one_says_it() -> None:
    no_re_entry, one_re_entry = (describe_setup(example.setup, full_session=_CLOCK) for example in EXAMPLE_SETUPS)

    assert "re-entry" not in " ".join(no_re_entry.window)
    assert all("After a stop" not in " ".join(leg.sentences) for leg in no_re_entry.legs)
    assert "earns no re-entry" in " ".join(one_re_entry.window) and _before_close(60) in " ".join(one_re_entry.window)
    assert all(re.search(r"buys it at once, up to 1 time\b", " ".join(leg.sentences)) for leg in one_re_entry.legs)


def test_both_examples_fit_a_full_day() -> None:
    for example in EXAMPLE_SETUPS:
        assert describe_setup(example.setup, full_session=_CLOCK).notices == ()


def _every_sentence(setup: Any) -> list[str]:
    described = describe_setup(setup, full_session=_CLOCK)
    return [
        *described.window,
        *(sentence for leg in described.legs for sentence in leg.sentences),
        *described.square_off,
        *described.costs,
        *described.notices,
    ]


def test_each_block_is_a_list_of_whole_sentences_one_to_an_item() -> None:
    busy = _setup(
        no_reentry_after=_before_close_time(60),
        fees={"kind": "estimated"},
        legs=[
            _leg(
                momentum={"kind": "percent_up", "value": "25"},
                stop_loss={"kind": "percent", "value": "25"},
                reentry_on_stop={"kind": "on_momentum", "count": 2},
            )
        ],
    )
    unsound = _setup(entry=_after_open_time(_SESSION_MINUTES + 1))

    for written in (busy, unsound):
        sentences = _every_sentence(written)
        assert sentences
        for sentence in sentences:
            assert sentence.endswith(".") and sentence == sentence.strip(), sentence
            # A full stop followed by a space would be a second sentence in the item.
            assert ". " not in sentence, sentence


def test_the_blocks_have_the_sentences_the_setup_gives_rise_to_and_no_more() -> None:
    plain = describe_setup(_setup(), full_session=_CLOCK)
    cutoff = describe_setup(_setup(no_reentry_after=_before_close_time(60)), full_session=_CLOCK)
    estimated = describe_setup(_setup(fees={"kind": "estimated"}), full_session=_CLOCK)

    # The window: trades, entry, exit, early close, fills, dropped buy, the exit; the cutoff adds two more.
    assert len(plain.window) == 7 and len(cutoff.window) == 9
    # A plain leg: what it buys, and when it buys. Square off: one. Costs: fills, fees, cash; estimated adds one.
    assert [len(leg.sentences) for leg in plain.legs] == [2]
    assert len(plain.square_off) == 1
    assert len(plain.costs) == 3 and len(estimated.costs) == 4


# ── A leg that sells (#3105) ─────────────────────────────────────────────

_COVER = _leg(leg_id="long_call")


def _sold_leg_sentences(**changes: Any) -> tuple[str, ...]:
    """The sentences of a sold call, set up beside the bought call that covers it."""
    legs = [_COVER, _leg(leg_id="short_call", side="sell", **changes)]
    return describe_setup(_setup(legs=legs), full_session=_CLOCK).legs[1].sentences


def test_a_sold_leg_says_it_sells_to_open_and_is_bought_back_and_its_bought_cover_is_said_as_it_always_was() -> None:
    described = describe_setup(_setup(legs=[_COVER, _leg(leg_id="short_call", side="sell")]), full_session=_CLOCK).legs

    assert [leg.leg_id for leg in described] == ["long_call", "short_call"]
    assert described[0].sentences == (
        "Leg long_call: buys 1 at-the-money call expiring on the nearest listed expiry, today included, that falls "
        "on the last trading day of its week.",
        "The buy comes as soon as the entry is read.",
    )
    assert described[1].sentences == (
        "Leg short_call: sells to open 1 at-the-money call expiring on the nearest listed expiry, today included, "
        "that falls on the last trading day of its week, and is bought back to close.",
        "The sale comes as soon as the entry is read.",
        "The sale needs one bought call of the same expiry for each contract sold, held already or filling in the "
        "same minute; with too few, it is refused and the leg is done for the day.",
    )


@pytest.mark.parametrize(
    ("stop_loss", "said"),
    [
        (
            {"kind": "percent", "value": "25"},
            "Bought back once a minute closes at or above its stop level, 25% above what it was sold for.",
        ),
        (
            {"kind": "percent", "value": "150"},
            "Bought back once a minute closes at or above its stop level, 150% above what it was sold for.",
        ),
        (
            {"kind": "points", "value": "0.4"},
            "Bought back once a minute closes at or above its stop level, 0.4 points above what it was sold for.",
        ),
    ],
)
def test_a_sold_legs_stop_is_a_rise_above_what_it_was_sold_for_and_is_not_a_sale(
    stop_loss: dict[str, Any], said: str
) -> None:
    sentences = _sold_leg_sentences(stop_loss=stop_loss)

    assert said in sentences
    assert not any("below what was paid" in sentence or sentence.startswith("Sells ") for sentence in sentences)


def test_a_sold_legs_momentum_gates_the_sale() -> None:
    sentences = _sold_leg_sentences(momentum={"kind": "percent_down", "value": "10"})

    assert sentences[1] == (
        "The sale waits until a minute closes at least 10% below where its premium stood at the entry time."
    )
    assert not any("The buy" in sentence for sentence in sentences)


def test_a_sold_legs_re_entry_sells_the_fresh_contract_and_on_momentum_is_said_as_for_a_bought_leg() -> None:
    at_once = _sold_leg_sentences(
        stop_loss={"kind": "percent", "value": "25"}, reentry_on_stop={"kind": "at_once", "count": 2}
    )
    on_momentum = _sold_leg_sentences(
        stop_loss={"kind": "percent", "value": "25"},
        momentum={"kind": "percent_up", "value": "10"},
        reentry_on_stop={"kind": "on_momentum", "count": 1},
    )

    assert at_once[-1] == (
        "After a stop, picks a fresh contract by the same rules and sells it at once, up to 2 times in a session."
    )
    assert on_momentum[-1] == (
        "After a stop, picks a fresh contract by the same rules, takes a new starting premium from it and waits for "
        "momentum again, up to 1 time in a session."
    )


def test_a_bought_legs_sentences_are_what_they_were_with_every_rule_on() -> None:
    [leg] = describe_setup(
        _setup(
            legs=[
                _leg(
                    leg_id="put",
                    right="put",
                    contracts=2,
                    strike={"kind": "atm_offset", "steps": -1},
                    stop_loss={"kind": "points", "value": "0.4"},
                    momentum={"kind": "percent_up", "value": "25"},
                    reentry_on_stop={"kind": "at_once", "count": 1},
                )
            ]
        ),
        full_session=_CLOCK,
    ).legs

    assert leg.sentences == (
        "Leg put: buys 2 puts 1 strike below the money expiring on the nearest listed expiry, today included, that "
        "falls on the last trading day of its week.",
        "The buy waits until a minute closes at least 25% above where its premium stood at the entry time.",
        "Sells once a minute closes at least 0.4 points below what was paid.",
        "After a stop, picks a fresh contract by the same rules and buys it at once, up to 1 time in a session.",
    )


def test_a_setup_that_sells_says_which_orders_open_and_which_close_and_a_setup_that_buys_is_said_as_it_was() -> None:
    buys = describe_setup(_setup(), full_session=_CLOCK).window
    sells = describe_setup(_setup(legs=[_COVER, _leg(leg_id="short_call", side="sell")]), full_session=_CLOCK).window

    assert re.search(r"A buy is dropped.*a sale is tried each minute until it does", " ".join(buys))
    assert "At the exit, whatever is open is sold, legs still waiting are cancelled and nothing more is bought." in buys
    assert len(sells) == len(buys)
    assert re.search(r"order to open is dropped.*order to close is tried each minute", " ".join(sells))
    assert re.search(r"a bought leg still open is sold, a sold leg still open is bought back", " ".join(sells))
    assert not any("A buy is dropped" in sentence or "whatever is open is sold" in sentence for sentence in sells)
    assert sells[:5] == buys[:5]


def test_the_sentences_of_a_setup_that_sells_are_whole_sentences_one_to_an_item() -> None:
    selling = _setup(
        no_reentry_after=_before_close_time(60),
        legs=[
            _leg(leg_id="long_call", stop_loss={"kind": "percent", "value": "25"}),
            _leg(
                leg_id="short_call",
                side="sell",
                momentum={"kind": "percent_up", "value": "25"},
                stop_loss={"kind": "percent", "value": "150"},
                reentry_on_stop={"kind": "on_momentum", "count": 2},
            ),
        ],
    )

    sentences = _every_sentence(selling)

    assert sentences
    for sentence in sentences:
        assert sentence.endswith(".") and sentence == sentence.strip(), sentence
        assert ". " not in sentence, sentence


def test_a_partial_square_off_of_a_setup_that_sells_says_a_stopped_bought_leg_takes_the_sold_legs_it_protects() -> None:
    # A stop on a bought leg that covers a sold one closes the sold leg too (#3107), so "only the leg that was stopped"
    # would be untrue of a setup that sells. A setup that buys only is said as it always was, and so is a complete one.
    selling = _setup(square_off="partial", legs=[_COVER, _leg(leg_id="short_call", side="sell")])
    buying = _setup(square_off="partial", legs=[_COVER])
    complete = _setup(square_off="complete", legs=[_COVER, _leg(leg_id="short_call", side="sell")])

    assert describe_setup(selling, full_session=_CLOCK).square_off == (
        "Square off is partial: a stop closes only the leg that was stopped, and a stopped bought leg takes with it "
        "any sold leg it protects, bought back in the same minute.",
    )
    assert describe_setup(buying, full_session=_CLOCK).square_off == (
        "Square off is partial: a stop closes only the leg that was stopped.",
    )
    assert "every open leg" in " ".join(describe_setup(complete, full_session=_CLOCK).square_off)
