"""Two saved runs compared (#3030): the settings that differ, and the sessions they share.

Every expectation is worked out by hand in a comment. The base setup is ``setup_fixtures.setup()``: it enters 5 minutes
after the open and leaves 46 minutes before the close, squares off completely, 0.3 percent slippage, a price step of
0.01, no fees and 10,000 to start, with one leg ``call`` (a call at the money expiring on the last trading day of its
week) and no rule on it.

A rule reads here as it reads in a setup's description: the words come from ``phrases.py`` for both, and
``test_a_rule_reads_in_the_comparison_as_it_reads_in_the_description`` holds the two together.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.options_backtest.comparison import (
    NOT_IN_THIS_RUN,
    NOT_SET,
    Setting,
    compare,
    overlap,
    setting_differences,
)
from app.options_backtest.describe import leg_sentences
from app.options_backtest.results import SessionStatus
from app.options_backtest.saved_runs import RunSetupCannotBeRead, SessionLine
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup
from tests.options_backtest._headers import THE_REASON, a_header, an_unreadable_header
from tests.options_backtest.setup_fixtures import leg, setup

D = Decimal


def _differences(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[Setting, str | None, str, str, str]]:
    return [
        (each.setting, each.leg_id, each.label, each.a, each.b) for each in setting_differences(setup(**a), setup(**b))
    ]


# ── Settings that are the same, however they are written ─────────────────


def test_two_runs_of_one_setup_differ_in_nothing() -> None:
    assert setting_differences(setup(), setup()) == ()


def test_a_number_is_the_same_number_however_many_zeros_it_is_written_with() -> None:
    # 0.3 and 0.3000 are one slippage; 10000 and 10000.00 one amount of cash; and so on down every figure of a setup.
    plain = {
        "slippage_percent": "0.3",
        "price_step_usd": "0.01",
        "starting_cash_usd": "10000",
        "fees": {"kind": "per_contract", "per_contract_usd": "0.65"},
        "legs": [
            leg(
                stop_loss={"kind": "percent", "value": "25"},
                momentum={"kind": "percent_up", "value": "10"},
            )
        ],
    }
    padded = {
        "slippage_percent": "0.3000",
        "price_step_usd": "0.0100",
        "starting_cash_usd": "10000.00",
        "fees": {"kind": "per_contract", "per_contract_usd": "0.6500"},
        "legs": [
            leg(
                stop_loss={"kind": "percent", "value": "25.0000"},
                momentum={"kind": "percent_up", "value": "10.0000"},
            )
        ],
    }

    assert _differences(plain, padded) == []
    assert _differences(padded, plain) == []


def test_the_same_legs_in_another_order_are_the_same_setup_because_legs_are_matched_by_their_id() -> None:
    call, put = leg(leg_id="call"), leg(leg_id="put", right="put")

    assert _differences({"legs": [call, put]}, {"legs": [put, call]}) == []


# ── Each kind of setting, on its own ─────────────────────────────────────

BASE_ENTRY = {"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        # The window.
        (
            {"entry": {"anchor": "open", "offset_ms": 10 * MS_PER_MINUTE}},
            (Setting.ENTRY_TIME, None, "entry time", "5 minutes after the open", "10 minutes after the open"),
        ),
        (
            {"exit": {"anchor": "close", "offset_ms": 1 * MS_PER_MINUTE}},
            (Setting.EXIT_TIME, None, "exit time", "46 minutes before the close", "1 minute before the close"),
        ),
        (
            {"no_reentry_after": {"anchor": "close", "offset_ms": 0}},
            (Setting.NO_REENTRY_AFTER, None, "no re-entry after", NOT_SET, "at the close"),
        ),
        (
            {"no_reentry_after": {"anchor": "open", "offset_ms": 90 * MS_PER_MINUTE}},
            (Setting.NO_REENTRY_AFTER, None, "no re-entry after", NOT_SET, "90 minutes after the open"),
        ),
        (
            {"square_off": "partial"},
            (
                Setting.SQUARE_OFF,
                None,
                "square off",
                "Square off is complete: a stop closes every open leg and cancels legs still waiting for momentum, "
                "which stay cancelled for the day.",
                "Square off is partial: a stop closes only the leg that was stopped.",
            ),
        ),
        # The costs.
        (
            {"slippage_percent": "0.5"},
            (Setting.SLIPPAGE, None, "slippage", "0.3%", "0.5%"),
        ),
        (
            {"price_step_usd": "0.05"},
            (Setting.PRICE_STEP, None, "price step", "$0.01", "$0.05"),
        ),
        (
            {"fees": {"kind": "per_contract", "per_contract_usd": "0.65"}},
            (
                Setting.FEES,
                None,
                "fees",
                "Fees are not charged, so results are before fees.",
                "Fees are $0.65 per contract traded.",
            ),
        ),
        (
            {"fees": {"kind": "estimated"}},
            (
                Setting.FEES,
                None,
                "fees",
                "Fees are not charged, so results are before fees.",
                "Fees are estimated from published option fee rates.",
            ),
        ),
        (
            {"starting_cash_usd": "25000.5"},
            (Setting.STARTING_CASH, None, "starting cash", "$10,000.00", "$25,000.50"),
        ),
        # A leg.
        (
            {"legs": [leg(right="put")]},
            (Setting.LEG_RIGHT, "call", "leg call: call or put", "call", "put"),
        ),
        (
            {"legs": [leg(contracts=3)]},
            (Setting.LEG_CONTRACTS, "call", "leg call: contracts", "1 contract", "3 contracts"),
        ),
        (
            {"legs": [leg(expiry={"kind": "same_day"})]},
            (
                Setting.LEG_EXPIRY,
                "call",
                "leg call: expiry",
                "expiring on the nearest listed expiry, today included, that falls on the last trading day of its week",
                "expiring on the trading date itself",
            ),
        ),
        (
            {"legs": [leg(expiry={"kind": "min_days", "days": 7})]},
            (
                Setting.LEG_EXPIRY,
                "call",
                "leg call: expiry",
                "expiring on the nearest listed expiry, today included, that falls on the last trading day of its week",
                "expiring on the nearest listed expiry at least 7 calendar days after the trading day",
            ),
        ),
        (
            {"legs": [leg(strike={"kind": "atm_offset", "steps": 2})]},
            (Setting.LEG_STRIKE, "call", "leg call: strike", "at the money", "2 strikes above the money"),
        ),
        (
            {"legs": [leg(strike={"kind": "atm_offset", "steps": -1})]},
            (Setting.LEG_STRIKE, "call", "leg call: strike", "at the money", "1 strike below the money"),
        ),
        (
            {"legs": [leg(stop_loss={"kind": "percent", "value": "20"})]},
            (Setting.LEG_STOP_LOSS, "call", "leg call: stop loss", NOT_SET, "at least 20% below what was paid"),
        ),
        (
            {"legs": [leg(momentum={"kind": "percent_down", "value": "10"})]},
            (
                Setting.LEG_MOMENTUM,
                "call",
                "leg call: momentum",
                NOT_SET,
                "at least 10% below where its premium stood at the entry time",
            ),
        ),
    ],
)
def test_each_setting_that_differs_is_one_difference_named_as_an_owner_would_with_both_values_in_words(
    changes: dict[str, Any], expected: tuple[Setting, str | None, str, str, str]
) -> None:
    assert _differences({}, changes) == [expected]
    # And the other way round: the same setting, the two values swapped.
    swapped = (expected[0], expected[1], expected[2], expected[4], expected[3])
    assert _differences(changes, {}) == [swapped]


def test_one_rule_replaced_by_another_of_the_same_leg_is_one_difference_with_each_in_words() -> None:
    a = {"legs": [leg(stop_loss={"kind": "percent", "value": "25"}, momentum={"kind": "points_up", "value": "0.40"})]}
    b = {"legs": [leg(stop_loss={"kind": "points", "value": "1"}, momentum={"kind": "points_up", "value": "0.4"})]}

    # 25 percent against 1 point; the momentum is 0.40 and 0.4 points, which is one value, so it is not a difference.
    assert _differences(a, b) == [
        (
            Setting.LEG_STOP_LOSS,
            "call",
            "leg call: stop loss",
            "at least 25% below what was paid",
            "at least 1 point below what was paid",
        ),
    ]


def test_a_re_entry_rule_is_a_difference_in_the_leg_that_gains_it_and_in_how_many_times_it_may_come_back() -> None:
    # A re-entry needs a stop that can earn it, so every leg here carries the same 20 percent stop.
    stop = {"kind": "percent", "value": "20"}
    none = {"legs": [leg(stop_loss=stop)]}
    once = {"legs": [leg(stop_loss=stop, reentry_on_stop={"kind": "at_once", "count": 1})]}
    on_momentum = leg(
        stop_loss=stop,
        momentum={"kind": "percent_up", "value": "10"},
        reentry_on_stop={"kind": "on_momentum", "count": 2},
    )

    at_once = "After a stop, picks a fresh contract by the same rules and buys it at once, up to 1 time in a session."
    # On momentum, twice: the starting premium is taken afresh and the leg waits for momentum again.
    again = (
        "After a stop, picks a fresh contract by the same rules, takes a new starting premium from it and waits for "
        "momentum again, up to 2 times in a session."
    )
    assert _differences(none, once) == [(Setting.LEG_REENTRY, "call", "leg call: re-entry on stop", NOT_SET, at_once)]
    assert [(d.setting, d.a, d.b) for d in setting_differences(setup(**once), setup(legs=[on_momentum]))] == [
        (Setting.LEG_MOMENTUM, NOT_SET, "at least 10% above where its premium stood at the entry time"),
        (Setting.LEG_REENTRY, at_once, again),
    ]


def test_a_price_step_is_said_with_every_digit_it_has_so_two_steps_never_read_alike() -> None:
    # 0.0125 is a quarter of a cent more than 0.01. Rounded to cents both would read $0.01, a difference that looks like
    # none; every digit is kept: $0.01 and $0.0125.
    assert _differences({}, {"price_step_usd": "0.0125"}) == [
        (Setting.PRICE_STEP, None, "price step", "$0.01", "$0.0125")
    ]


def test_a_rule_reads_in_the_comparison_as_it_reads_in_the_description() -> None:
    # One place says how a rule reads (phrases.py). So each value the comparison gives for a rule is a phrase of the
    # sentences the description makes of the leg that has it: nothing is re-worded here.
    rules = [
        ("expiry", {"expiry": {"kind": "min_days", "days": 7}}, Setting.LEG_EXPIRY),
        ("strike", {"strike": {"kind": "atm_offset", "steps": -2}}, Setting.LEG_STRIKE),
        ("stop loss", {"stop_loss": {"kind": "points", "value": "1.5"}}, Setting.LEG_STOP_LOSS),
        ("momentum", {"momentum": {"kind": "percent_down", "value": "15"}}, Setting.LEG_MOMENTUM),
    ]
    for name, changes, setting in rules:
        written = leg(**changes)
        [difference] = [d for d in setting_differences(setup(), setup(legs=[written])) if d.setting is setting]
        described = " ".join(leg_sentences(LegSetup.model_validate(written)))
        assert difference.b in described, (name, difference.b, described)

    stopped = leg(stop_loss={"kind": "percent", "value": "20"}, reentry_on_stop={"kind": "at_once", "count": 3})
    [reentry] = [d for d in setting_differences(setup(), setup(legs=[stopped])) if d.setting is Setting.LEG_REENTRY]
    assert reentry.b in leg_sentences(LegSetup.model_validate(stopped))


# ── Legs added and removed, and several differences at once ──────────────


def test_a_leg_only_one_run_has_is_one_difference_describing_it_and_saying_the_other_run_lacks_it() -> None:
    call = leg(leg_id="call")
    put = leg(
        leg_id="put",
        right="put",
        contracts=2,
        expiry={"kind": "same_day"},
        strike={"kind": "atm_offset", "steps": -1},
        stop_loss={"kind": "points", "value": "0.4"},
        momentum={"kind": "percent_up", "value": "25"},
        reentry_on_stop={"kind": "on_momentum", "count": 1},
    )

    added = _differences({"legs": [call]}, {"legs": [call, put]})
    removed = _differences({"legs": [call, put]}, {"legs": [call]})

    # The leg is said with the sentences the description makes of it, one after the other.
    described = (
        "Leg put: buys 2 puts 1 strike below the money expiring on the trading date itself. "
        "The buy waits until a minute closes at least 25% above where its premium stood at the entry time. "
        "Sells once a minute closes at least 0.4 points below what was paid. "
        "After a stop, picks a fresh contract by the same rules, takes a new starting premium from it and waits for "
        "momentum again, up to 1 time in a session."
    )
    assert added == [(Setting.LEG, "put", "leg put", NOT_IN_THIS_RUN, described)]
    assert removed == [(Setting.LEG, "put", "leg put", described, NOT_IN_THIS_RUN)]


def test_several_differences_come_in_a_fixed_order_the_window_then_the_legs_then_the_costs() -> None:
    a = {"legs": [leg(leg_id="call"), leg(leg_id="old", right="put")]}
    b = {
        # Written in the opposite order to how they are reported.
        "slippage_percent": "1",
        "legs": [
            leg(leg_id="new", right="put"),
            leg(leg_id="call", stop_loss={"kind": "percent", "value": "20"}),
        ],
        "exit": {"anchor": "close", "offset_ms": 30 * MS_PER_MINUTE},
        "entry": {"anchor": "open", "offset_ms": 7 * MS_PER_MINUTE},
    }

    assert [(setting, leg_id) for setting, leg_id, _, _, _ in _differences(a, b)] == [
        (Setting.ENTRY_TIME, None),
        (Setting.EXIT_TIME, None),
        # a's legs in a's order: call differs in its stop, old is only in a; then the legs only b has.
        (Setting.LEG_STOP_LOSS, "call"),
        (Setting.LEG, "old"),
        (Setting.LEG, "new"),
        (Setting.SLIPPAGE, None),
    ]


# ── A leg's side (#3105) ─────────────────────────────────────────────────

COVER = leg(leg_id="long_call")
BOUGHT_PAIR = {"legs": [COVER, leg(leg_id="short_call")]}
SOLD_PAIR = {"legs": [COVER, leg(leg_id="short_call", side="sell")]}


def test_two_setups_that_differ_only_in_one_legs_side_differ_in_that_setting_and_in_nothing_else() -> None:
    assert _differences(BOUGHT_PAIR, SOLD_PAIR) == [
        (Setting.LEG_SIDE, "short_call", "leg short_call: bought or sold", "bought", "sold")
    ]
    assert _differences(SOLD_PAIR, BOUGHT_PAIR) == [
        (Setting.LEG_SIDE, "short_call", "leg short_call: bought or sold", "sold", "bought")
    ]


def test_a_stop_that_is_the_same_rule_on_a_leg_whose_side_changed_is_one_difference_the_side() -> None:
    stop = {"stop_loss": {"kind": "percent", "value": "40"}}
    a = {"legs": [COVER, leg(leg_id="short_call", **stop)]}
    b = {"legs": [COVER, leg(leg_id="short_call", side="sell", **stop)]}

    assert [(setting, leg_id) for setting, leg_id, _, _, _ in _differences(a, b)] == [(Setting.LEG_SIDE, "short_call")]


def test_a_stop_that_differs_between_a_bought_and_a_sold_leg_is_said_as_a_fall_and_as_a_rise() -> None:
    a = {"legs": [COVER, leg(leg_id="short_call", stop_loss={"kind": "percent", "value": "25"})]}
    b = {"legs": [COVER, leg(leg_id="short_call", side="sell", stop_loss={"kind": "percent", "value": "150"})]}

    assert _differences(a, b) == [
        (Setting.LEG_SIDE, "short_call", "leg short_call: bought or sold", "bought", "sold"),
        (
            Setting.LEG_STOP_LOSS,
            "short_call",
            "leg short_call: stop loss",
            "at least 25% below what was paid",
            "at or above its stop level, 150% above what it was sold for",
        ),
    ]


def test_a_re_entry_that_differs_between_a_bought_and_a_sold_leg_says_which_side_it_enters() -> None:
    stop = {"stop_loss": {"kind": "percent", "value": "25"}}
    a = {"legs": [COVER, leg(leg_id="short_call", **stop)]}
    re_enters = {"reentry_on_stop": {"kind": "at_once", "count": 1}}
    b = {"legs": [COVER, leg(leg_id="short_call", side="sell", **re_enters, **stop)]}

    [side, reentry] = _differences(a, b)

    assert side[0] is Setting.LEG_SIDE
    assert reentry[0] is Setting.LEG_REENTRY
    assert reentry[3:] == (
        NOT_SET,
        "After a stop closes it (its own, or one that takes it along), picks a fresh contract by the same rules and "
        "sells it at once, up to 1 time in a session.",
    )


def test_a_sold_leg_only_one_run_has_is_said_with_the_sentences_the_description_makes_of_it() -> None:
    sold = leg(leg_id="short_call", side="sell", stop_loss={"kind": "points", "value": "0.4"})
    described = (
        "Leg short_call: sells to open 1 at-the-money call expiring on the nearest listed expiry, today included, "
        "that falls on the last trading day of its week, and is bought back to close. "
        "The sale comes as soon as the entry is read. "
        "The sale needs one bought call of the same expiry for each contract sold, held already or filling in the "
        "same minute; with too few, it is refused and the leg is done for the day. "
        "Bought back once a minute closes at or above its stop level, 0.4 points above what it was sold for."
    )

    added = _differences({"legs": [COVER]}, {"legs": [COVER, sold]})

    assert added == [(Setting.LEG, "short_call", "leg short_call", NOT_IN_THIS_RUN, described)]
    assert described == " ".join(leg_sentences(LegSetup.model_validate(sold)))


def test_a_partial_square_off_is_said_as_it_is_of_the_setup_that_has_it_and_a_setup_that_sells_takes_the_sold_legs() -> None:
    # Bought legs only, a partial square-off closes the leg that was stopped. With a sold leg a stopped bought leg takes
    # the sold legs it protects with it (#3107), and the comparison says it as the description does.
    complete_buying = {"square_off": "complete", **BOUGHT_PAIR}
    partial_selling = {"square_off": "partial", **SOLD_PAIR}
    partial_buying = {"square_off": "partial", **BOUGHT_PAIR}

    [_, _, selling_label, _, selling_words] = _differences(complete_buying, partial_selling)[0]
    [(_, _, _, _, buying_words), *_] = _differences(complete_buying, partial_buying)

    assert selling_label == "square off"
    assert selling_words == (
        "Square off is partial: a stop closes only the leg that was stopped, and a stopped bought leg takes with it "
        "the sold legs it protects, closed in the same minute and exit group, last listed first, which can close more "
        "legs than strictly needed."
    )
    assert buying_words == "Square off is partial: a stop closes only the leg that was stopped."


# ── The sessions two runs share ──────────────────────────────────────────


def _line(at: int, status: SessionStatus, profit: str, trades: int) -> SessionLine:
    return SessionLine(session_open_ms=at, status=status, profit_usd=D(profit), trades=trades)


T, N, S = SessionStatus.TRADED, SessionStatus.NO_TRADE, SessionStatus.SKIPPED


def test_the_sessions_both_runs_tested_are_shared_each_runs_extras_are_counted_and_totals_are_over_the_shared_only() -> (
    None
):
    # Sessions are named by their opens: 1, 2, 3, 4, 5 stand for five opens.
    a = [_line(1, T, "10.00", 2), _line(2, T, "-5.50", 1), _line(3, S, "0", 0), _line(4, T, "3.25", 3)]
    b = [_line(2, T, "2.00", 1), _line(3, N, "0", 0), _line(4, T, "-1.25", 2), _line(5, T, "7.00", 4)]

    found = overlap(a, b)

    # Shared: 2, 3 and 4. Run a has 1 and run b has 5 besides, and neither is in a total.
    assert [shared.session_open_ms for shared in found.shared] == [2, 3, 4]
    assert (found.only_in_a, found.only_in_b) == (1, 1)
    # a: -5.50 + 0 + 3.25 = -2.25 over 1 + 0 + 3 = 4 trades; one session (4) above zero.
    assert (found.a.net_profit_usd, found.a.trades, found.a.winning_sessions) == (D("-2.25"), 4, 1)
    # b: 2.00 + 0 - 1.25 = 0.75 over 1 + 0 + 2 = 3 trades; one session (2) above zero.
    assert (found.b.net_profit_usd, found.b.trades, found.b.winning_sessions) == (D("0.75"), 3, 1)
    # The pair of each shared session, with both statuses, in order.
    assert [(s.session_open_ms, s.a.status, s.b.status, s.a.profit_usd, s.b.profit_usd) for s in found.shared] == [
        (2, T, T, D("-5.50"), D("2.00")),
        (3, S, N, D("0"), D("0")),
        (4, T, T, D("3.25"), D("-1.25")),
    ]


def test_a_session_that_broke_even_is_not_a_winning_session_and_a_profit_of_a_cent_is() -> None:
    a = [_line(1, T, "0.00", 1), _line(2, T, "0.01", 1), _line(3, T, "-0.01", 1)]

    found = overlap(a, a)

    assert (found.a.winning_sessions, found.b.winning_sessions) == (1, 1)
    assert found.a.net_profit_usd == D("0.00")


def test_totals_are_exact_whatever_their_length() -> None:
    # 30 digits do not fit the default 28-digit context, which would round this sum. The exact sum is
    # 1234567890123456789012345678.90 + 0.01 + 0.10 = 1234567890123456789012345679.01.
    a = [
        _line(1, T, "1234567890123456789012345678.90", 1),
        _line(2, T, "0.01", 1),
        _line(3, T, "0.10", 1),
    ]

    found = overlap(a, a)

    assert found.a.net_profit_usd == D("1234567890123456789012345679.01")
    assert type(found.a.net_profit_usd) is Decimal


def test_runs_with_no_session_in_common_share_nothing_and_total_nothing() -> None:
    found = overlap([_line(1, T, "9.99", 1)], [_line(2, T, "8.88", 1)])

    assert (found.shared, found.only_in_a, found.only_in_b) == ((), 1, 1)
    assert (found.a.net_profit_usd, found.a.trades, found.a.winning_sessions) == (D("0"), 0, 0)
    assert (found.b.net_profit_usd, found.b.trades, found.b.winning_sessions) == (D("0"), 0, 0)


# ── The whole comparison ─────────────────────────────────────────────────


def test_a_comparison_carries_the_underlying_the_differences_and_whether_the_ranges_differ() -> None:
    same_range = compare(
        a_header(1, 10, 30, 3), [], a_header(2, 10, 30, 3, setup_changes={"slippage_percent": "0.5"}), []
    )
    other_range = compare(a_header(1, 10, 30, 3), [], a_header(2, 10, 20, 2), [])

    assert same_range.underlying == "SPY"
    assert [difference.setting for difference in same_range.settings] == [Setting.SLIPPAGE]
    assert not same_range.ranges_differ
    assert other_range.settings == () and other_range.ranges_differ
    assert (other_range.range_a.sessions_requested, other_range.range_b.last_session_open_ms) == (3, 20)


def test_each_runs_extra_sessions_are_counted_for_that_run_when_the_two_runs_are_of_different_lengths() -> None:
    a = [_line(1, T, "1.00", 1), _line(2, T, "2.00", 1), _line(3, T, "3.00", 1)]
    b = [_line(3, T, "4.00", 1), _line(4, T, "5.00", 1)]

    found = overlap(a, b)

    # Only session 3 is in both. Run a has 1 and 2 besides: two. Run b has 4 besides: one.
    assert [shared.session_open_ms for shared in found.shared] == [3]
    assert (found.only_in_a, found.only_in_b) == (2, 1)
    assert (found.a.net_profit_usd, found.b.net_profit_usd) == (D("3.00"), D("4.00"))


def test_a_run_whose_stored_setup_can_no_longer_be_read_cannot_have_its_settings_compared() -> None:
    readable, unreadable = a_header(1), an_unreadable_header(2)

    for first, second in ((readable, unreadable), (unreadable, readable), (unreadable, unreadable)):
        with pytest.raises(RunSetupCannotBeRead) as caught:
            compare(first, [], second, [])
        # The run named is the one whose setup is lost (run 2 in each pair), with its reason beside it.
        assert str(caught.value) == f"the setup stored with run 2 cannot be read: {THE_REASON}"
        assert caught.value.reason == THE_REASON
