"""The per-date expiry bracket (#3528): ``pick_bracket_expiries`` over a month's one wide expiry lookup.

``find_bracket_contracts`` lists the expiries once per month and picks the pair around 30 days for each session from
that list. These tests pin the pick itself: 30 days is inside the pair on every date, the edges where an expiry
enters or leaves the usable range, and the typed reason when no pair exists. The month-long carry of a pair and the
re-list are pinned end to end in ``test_iv_builder_bracket_window.py``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.research.options.contract_finder import (
    EXPIRY_MAX_DTE,
    MIN_DTE_DAYS,
    TARGET_DTE,
    BracketGap,
    pick_bracket_expiries,
)

FRIDAY = 4


def fridays(first: date, last: date) -> list[str]:
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return [day.isoformat() for day in days if day.weekday() == FRIDAY]


def dte(expiry: str, day: date) -> int:
    return (date.fromisoformat(expiry) - day).days


MAY = [date(2024, 5, 1) + timedelta(days=n) for n in range(31)]
WEEKLY = fridays(date(2024, 4, 1), date(2024, 9, 1))


@pytest.mark.parametrize("day", MAY, ids=lambda day: day.isoformat())
def test_30_days_is_inside_the_pair_on_every_date_of_a_month(day: date):
    pair = pick_bracket_expiries(WEEKLY, day.isoformat())

    assert not isinstance(pair, BracketGap)
    low, high = pair
    assert MIN_DTE_DAYS <= dte(low, day) <= TARGET_DTE < dte(high, day) <= EXPIRY_MAX_DTE
    assert [e for e in WEEKLY if low < e < high] == [], "the pair is adjacent: no listed expiry sits between the two"
    assert [e for e in WEEKLY if low < e <= (day + timedelta(days=30)).isoformat()] == [], "low is the closest below"


def test_an_expiry_exactly_30_days_out_is_the_low_end_and_the_day_before_it_is_the_high_end():
    listed = ["2024-06-21", "2024-06-28", "2024-07-12"]

    assert pick_bracket_expiries(listed, "2024-05-28") == ("2024-06-21", "2024-06-28"), "Jun 28 is 31 days out"
    assert pick_bracket_expiries(listed, "2024-05-29") == ("2024-06-28", "2024-07-12"), "Jun 28 is 30 days out"
    assert pick_bracket_expiries(listed, "2024-05-30") == ("2024-06-28", "2024-07-12"), "Jun 28 is 29 days out"


def test_an_expiry_exactly_30_days_out_with_nothing_beyond_it_is_the_high_end():
    """The old pair (Mar 29, Apr 15) still answered on Mar 16, when Apr 15 was 30 days out; so does the pair now."""
    listed = ["2024-03-29", "2024-04-15"]

    assert pick_bracket_expiries(listed, "2024-03-15") == ("2024-03-29", "2024-04-15"), "Apr 15 is 31 days out"
    assert pick_bracket_expiries(listed, "2024-03-16") == ("2024-03-29", "2024-04-15"), "Apr 15 is 30 days out"
    assert pick_bracket_expiries(listed, "2024-03-17") == BracketGap.NO_FAR_EXPIRY, "Apr 15 is 29 days out: nothing beyond"
    assert pick_bracket_expiries(["2024-04-15"], "2024-03-16") == BracketGap.NO_FAR_EXPIRY, "nothing below to bracket it"


def test_the_pair_goes_when_the_far_expiry_is_out_of_range_and_when_the_near_one_is_too_close():
    """Three weeks between expiries: Jul 5 is usable from 45 days out, and Jun 14 until it is 6 days out."""
    listed = ["2024-06-14", "2024-07-05"]

    assert pick_bracket_expiries(listed, "2024-05-14") == BracketGap.NO_NEAR_EXPIRY, "Jun 14 is 31 days out"
    assert pick_bracket_expiries(listed, "2024-05-20") == BracketGap.NO_FAR_EXPIRY, "Jul 5 is 46 days out"
    assert pick_bracket_expiries(listed, "2024-05-21") == ("2024-06-14", "2024-07-05"), "Jul 5 enters at 45 days"
    assert pick_bracket_expiries(listed, "2024-06-04") == ("2024-06-14", "2024-07-05"), "Jul 5 is 31 days out"
    assert pick_bracket_expiries(listed, "2024-06-05") == ("2024-06-14", "2024-07-05"), "Jul 5 is 30 days out: high end"
    assert pick_bracket_expiries(listed, "2024-06-06") == BracketGap.NO_FAR_EXPIRY, "Jul 5 is 29 days out: nothing beyond"

    near = ["2024-06-10", "2024-07-05"]
    assert pick_bracket_expiries(near, "2024-06-03") == ("2024-06-10", "2024-07-05"), "Jun 10 is 7 days out: last day"
    assert pick_bracket_expiries(near, "2024-06-04") == BracketGap.NO_NEAR_EXPIRY, "Jun 10 is 6 days out: rolled off"


def test_a_date_no_listed_pair_brackets_says_why():
    assert pick_bracket_expiries([], "2024-05-01") == BracketGap.NO_EXPIRIES
    assert pick_bracket_expiries(["2024-05-24", "2024-05-31"], "2024-05-02") == BracketGap.NO_FAR_EXPIRY
    assert pick_bracket_expiries(["2024-06-07", "2024-06-14"], "2024-05-01") == BracketGap.NO_NEAR_EXPIRY
    assert BracketGap.NO_FAR_EXPIRY.value == "no_far_expiry", "the value is what the frame carries"
