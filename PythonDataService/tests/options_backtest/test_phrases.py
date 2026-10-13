"""The small phrases a setup's rules are put into words with (#3030).

The sentences made of them are pinned by ``test_describe.py``, and the comparison's values by ``test_comparison.py``. These
hold the phrases themselves, where a wrong digit would pass for a right one: money keeps every digit it has, a number
loses only its trailing zeros, and a distance names the open or the close. Each expectation is worked by hand.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.options_backtest import phrases
from app.options_backtest.setup import MS_PER_MINUTE, ReentryOnStop, SessionTime, StopLoss

D = Decimal


@pytest.mark.parametrize(
    ("value", "reads"),
    [
        # Grouped by thousands, and at least cents.
        ("10000", "$10,000.00"),
        ("25000.5", "$25,000.50"),
        ("0.5", "$0.50"),
        ("0.65", "$0.65"),
        # Trailing zeros and an exponent say nothing: 100000.00 is 1E+5 once normalised, and reads in plain digits.
        ("100000.00", "$100,000.00"),
        ("1E+5", "$100,000.00"),
        # Every digit it has: a quarter of a cent is not rounded away into a cent.
        ("0.0125", "$0.0125"),
        ("1234567.891", "$1,234,567.891"),
    ],
)
def test_money_keeps_every_digit_it_has_and_at_least_the_cents(value: str, reads: str) -> None:
    assert phrases.dollars(D(value)) == reads


@pytest.mark.parametrize(
    ("value", "reads"), [("25.0000", "25"), ("0.3000", "0.3"), ("1E+2", "100"), ("0", "0"), ("0.0100", "0.01")]
)
def test_a_number_is_written_without_its_trailing_zeros_or_an_exponent(value: str, reads: str) -> None:
    assert phrases.plain(D(value)) == reads


def test_an_amount_is_a_percent_or_a_number_of_points_and_one_point_is_singular() -> None:
    assert phrases.amount("percent", D("25.0")) == "25%"
    assert phrases.amount("points", D("1.0")) == "1 point"
    assert phrases.amount("points", D("0.40")) == "0.4 points"
    assert phrases.amount("points", D("2")) == "2 points"


def test_a_time_is_its_distance_from_the_open_or_the_close_and_names_the_open_or_the_close_itself() -> None:
    def time(anchor: str, minutes: int) -> SessionTime:
        return SessionTime.model_validate({"anchor": anchor, "offset_ms": minutes * MS_PER_MINUTE})

    assert phrases.distance(time("open", 0)) == "at the open"
    assert phrases.distance(time("open", 0), at="") == "the open"
    assert phrases.distance(time("close", 0)) == "at the close"
    assert phrases.distance(time("open", 1)) == "1 minute after the open"
    assert phrases.distance(time("open", 5)) == "5 minutes after the open"
    assert phrases.distance(time("close", 46)) == "46 minutes before the close"
    # "at" leads only the open or the close itself, not a distance from it.
    assert phrases.distance(time("close", 46), at="") == "46 minutes before the close"


def test_a_strike_is_counted_in_strikes_above_or_below_the_money_and_one_strike_is_singular() -> None:
    assert phrases.strikes_from_the_money(2) == "2 strikes above the money"
    assert phrases.strikes_from_the_money(1) == "1 strike above the money"
    assert phrases.strikes_from_the_money(-1) == "1 strike below the money"
    assert phrases.strikes_from_the_money(-3) == "3 strikes below the money"


def test_a_side_is_said_as_bought_or_sold() -> None:
    assert phrases.bought_or_sold("buy") == "bought"
    assert phrases.bought_or_sold("sell") == "sold"


def test_a_bought_legs_stop_is_a_fall_from_what_was_paid_and_a_sold_legs_a_rise_from_what_it_was_sold_for() -> None:
    percent = StopLoss(kind="percent", value=D("25.0"))
    points = StopLoss(kind="points", value=D("1"))
    over_a_hundred = StopLoss(kind="percent", value=D("250"))

    # A bought leg's words are the words they were, whether the side is said or left out.
    assert phrases.stop_loss(percent) == "at least 25% below what was paid"
    assert phrases.stop_loss(percent, "buy") == "at least 25% below what was paid"
    assert phrases.stop_loss(points, "buy") == "at least 1 point below what was paid"
    assert phrases.stop_loss(percent, "sell") == "at or above its stop level, 25% above what it was sold for"
    assert phrases.stop_loss(points, "sell") == "at or above its stop level, 1 point above what it was sold for"
    assert phrases.stop_loss(over_a_hundred, "sell") == "at or above its stop level, 250% above what it was sold for"


def test_a_re_entry_buys_the_fresh_contract_of_a_bought_leg_and_sells_that_of_a_sold_one() -> None:
    at_once = ReentryOnStop(kind="at_once", count=1)
    on_momentum = ReentryOnStop(kind="on_momentum", count=2)

    assert phrases.reentry(at_once) == (
        "After a stop, picks a fresh contract by the same rules and buys it at once, up to 1 time in a session."
    )
    assert phrases.reentry(at_once, "sell") == (
        "After a stop, picks a fresh contract by the same rules and sells it at once, up to 1 time in a session."
    )
    # Waiting for momentum names no verb, so it reads alike on both sides.
    assert phrases.reentry(on_momentum, "sell") == phrases.reentry(on_momentum, "buy")


def test_a_partial_square_off_says_that_a_stopped_bought_leg_takes_the_sold_legs_it_protects_only_when_the_setup_sells() -> (
    None
):
    only_the_stopped_leg = "Square off is partial: a stop closes only the leg that was stopped."

    assert phrases.square_off("partial") == only_the_stopped_leg
    assert phrases.square_off("partial", sells=False) == only_the_stopped_leg
    assert phrases.square_off("partial", sells=True) == (
        "Square off is partial: a stop closes only the leg that was stopped, and a stopped bought leg takes with it "
        "any sold leg it protects, bought back in the same minute."
    )
    # A complete square-off closes every open leg already, the sold ones with the rest.
    assert phrases.square_off("complete", sells=True) == phrases.square_off("complete")
