"""Two saved runs compared (#3030): the settings that differ, and the sessions they share.

Every expectation is worked out by hand in a comment. The base setup is ``setup_fixtures.setup()``: it enters 5 minutes
after the open and leaves 46 minutes before the close, squares off completely, 0.3 percent slippage, a price step of
0.01, no fees and 10,000 to start, with one leg ``call`` (a call at the money expiring on the last trading day of its
week) and no rule on it.
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
from app.options_backtest.results import SessionStatus
from app.options_backtest.saved_runs import RunHeader, RunRecord, SessionLine
from app.options_backtest.setup import MS_PER_MINUTE
from tests.options_backtest.setup_fixtures import leg, setup

D = Decimal
CALL_WORDS = "call, 1 contract, expiry the last trading day of the week, strike at the money"


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
        ({"square_off": "partial"}, (Setting.SQUARE_OFF, None, "square off", "complete", "partial")),
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
            (Setting.LEG_CONTRACTS, "call", "leg call: contracts", "1", "3"),
        ),
        (
            {"legs": [leg(expiry={"kind": "same_day"})]},
            (Setting.LEG_EXPIRY, "call", "leg call: expiry", "the last trading day of the week", "same day"),
        ),
        (
            {"legs": [leg(expiry={"kind": "min_days", "days": 7})]},
            (
                Setting.LEG_EXPIRY,
                "call",
                "leg call: expiry",
                "the last trading day of the week",
                "at least 7 calendar days out",
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
            (Setting.LEG_STOP_LOSS, "call", "leg call: stop loss", NOT_SET, "20% below the price paid"),
        ),
        (
            {"legs": [leg(momentum={"kind": "percent_down", "value": "10"})]},
            (
                Setting.LEG_MOMENTUM,
                "call",
                "leg call: momentum",
                NOT_SET,
                "10% down from where it stood at the entry time",
            ),
        ),
        (
            {"legs": [leg(reentry_on_stop={"kind": "at_once", "count": 2})]},
            (Setting.LEG_REENTRY, "call", "leg call: re-entry on stop", NOT_SET, "at once, up to 2 times"),
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
            "25% below the price paid",
            "1 point below the price paid",
        ),
    ]


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

    described = (
        "put, 2 contracts, expiry same day, strike 1 strike below the money; stop loss 0.4 points below the price paid; "
        "momentum 25% up from where it stood at the entry time; re-entry on stop on momentum, up to 1 time"
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


def _header(run_id: int, first: int, last: int, requested: int, **changes: Any) -> RunHeader:
    record = RunRecord(
        id=run_id,
        created_at_ms=1_791_800_000_000,
        finished_at_ms=1_791_800_100_000,
        job_id=f"job-{run_id}",
        status="complete",
        setup_name=None,
        underlying="SPY",
        first_session_open_ms=first,
        last_session_open_ms=last,
        sessions_requested=requested,
        fee_policy_text="Fees are not charged, so results are before fees.",
        calendar_identity="NYSE",
        calendar_package_version="5.1.0",
        failure=None,
        headline=None,
    )
    return RunHeader(record=record, setup=setup(**changes))


def test_a_comparison_carries_the_underlying_the_differences_and_whether_the_ranges_differ() -> None:
    same_range = compare(_header(1, 10, 30, 3), [], _header(2, 10, 30, 3, slippage_percent="0.5"), [])
    other_range = compare(_header(1, 10, 30, 3), [], _header(2, 10, 20, 2), [])

    assert same_range.underlying == "SPY"
    assert [difference.setting for difference in same_range.settings] == [Setting.SLIPPAGE]
    assert not same_range.ranges_differ
    assert other_range.settings == () and other_range.ranges_differ
    assert (other_range.range_a.sessions_requested, other_range.range_b.last_session_open_ms) == (3, 20)
