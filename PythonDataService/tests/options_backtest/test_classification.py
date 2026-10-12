"""The tag an expiry carries in the past-chain browser, worked out by hand from the calendar.

Every date is checked against the canonical calendar when the module loads, so a
wrong date fails here, loudly. The third Friday of a month is checked for what it
is: a Friday between the 15th and the 21st.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import is_trading_day
from app.options_backtest.classification import (
    ExpirySeries,
    ExpiryTag,
    last_session_of_month,
    standard_monthly_date,
    tag_expiry,
)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _on(year: int, month: int, day: int, weekday: str, *, session: bool = True) -> date:
    """A date that is the weekday named and is, or is not, a session on the calendar."""
    found = date(year, month, day)
    assert _WEEKDAYS[found.weekday()] == weekday, f"{found} is a {_WEEKDAYS[found.weekday()]}"
    assert is_trading_day(found) is session, f"{found} {'is not' if session else 'is'} a session on the calendar"
    return found


def _third_friday(year: int, month: int, day: int) -> date:
    found = _on(year, month, day, "Fri")
    assert 15 <= found.day <= 21, f"{found} is not a third Friday"
    return found


def _holiday_third_friday(year: int, month: int, day: int) -> date:
    found = _on(year, month, day, "Fri", session=False)
    assert 15 <= found.day <= 21, f"{found} is not a third Friday"
    return found


# A third Friday, on a plain month.
_MON_BEFORE = _on(2025, 10, 13, "Mon")
_WED = _on(2025, 10, 15, "Wed")
_FRI_THIRD = _third_friday(2025, 10, 17)
_MON_AFTER = _on(2025, 10, 20, "Mon")
_FRI_NEXT = _on(2025, 10, 24, "Fri")
_FRI_LAST_OF_OCTOBER = _on(2025, 10, 31, "Fri")
_SAT = _on(2025, 10, 18, "Sat", session=False)
# A third Friday that is a market holiday: the monthly moves to the Thursday.
_GOOD_FRIDAY_2025 = _holiday_third_friday(2025, 4, 18)
_THU_BEFORE_GOOD_FRIDAY = _on(2025, 4, 17, "Thu")
_MON_OF_GOOD_FRIDAY_WEEK = _on(2025, 4, 14, "Mon")
_JUNETEENTH_2026 = _holiday_third_friday(2026, 6, 19)
_THU_BEFORE_JUNETEENTH = _on(2026, 6, 18, "Thu")
_MON_OF_JUNETEENTH_WEEK = _on(2026, 6, 15, "Mon")
# A week whose Friday is a holiday, in a month where it is not the third.
_JULY_4_2025 = _on(2025, 7, 4, "Fri", session=False)
_TUE_OF_JULY_4_WEEK = _on(2025, 7, 1, "Tue")
_WED_OF_JULY_4_WEEK = _on(2025, 7, 2, "Wed")
_THU_OF_JULY_4_WEEK = _on(2025, 7, 3, "Thu")
# A weekday the exchange closed on one day only, with the Friday after it open.
_CLOSED_THURSDAY = _on(2025, 1, 9, "Thu", session=False)
_MON_OF_CLOSED_THURSDAY_WEEK = _on(2025, 1, 6, "Mon")
_FRI_AFTER_CLOSED_THURSDAY = _on(2025, 1, 10, "Fri")
# The third Fridays of quarter months, which are monthlies and not quarterlies.
_MON_BEFORE_MARCH_THIRD_FRIDAY = _on(2025, 3, 17, "Mon")
_MARCH_2025_THIRD_FRIDAY = _third_friday(2025, 3, 21)
_MON_BEFORE_DECEMBER_THIRD_FRIDAY = _on(2025, 12, 15, "Mon")
_DECEMBER_2025_THIRD_FRIDAY = _third_friday(2025, 12, 19)
# Quarter ends.
_MON_BEFORE_Q1_2025_END = _on(2025, 3, 24, "Mon")
_Q1_2025_END = _on(2025, 3, 31, "Mon")
_Q4_2025_END = _on(2025, 12, 31, "Wed")
_MON_OF_Q4_2027_END_WEEK = _on(2027, 12, 27, "Mon")
_Q4_2027_END_A_FRIDAY = _on(2027, 12, 31, "Fri")
_MON_OF_Q3_2028_END_WEEK = _on(2028, 9, 25, "Mon")
_Q3_2028_END_NOT_THE_30TH = _on(2028, 9, 29, "Fri")
# A fourth Friday that is a half day.
_MON_OF_THANKSGIVING_WEEK = _on(2025, 11, 24, "Mon")
_FRI_AFTER_THANKSGIVING = _on(2025, 11, 28, "Fri")
# Around the floor.
_DAY_BEFORE_THE_FLOOR = _on(2024, 9, 30, "Mon")
_FLOOR = _on(2024, 10, 1, "Tue")
_FRI_AFTER_THE_FLOOR = _on(2024, 10, 4, "Fri")
_MON_BEFORE_THE_FLOOR = _on(2024, 9, 16, "Mon")
_THIRD_FRIDAY_BEFORE_THE_FLOOR = _third_friday(2024, 9, 20)


def _tag(expiry: date, trading_date: date) -> ExpiryTag:
    return tag_expiry(expiry, trading_date=trading_date, listed=[expiry])


@pytest.mark.parametrize(
    ("trading_date", "expiry", "series"),
    [
        pytest.param(_MON_BEFORE, _FRI_THIRD, ExpirySeries.MONTHLY, id="a third Friday is the monthly"),
        pytest.param(
            _MON_OF_GOOD_FRIDAY_WEEK,
            _THU_BEFORE_GOOD_FRIDAY,
            ExpirySeries.MONTHLY,
            id="a third Friday that is Good Friday moves the monthly to the Thursday",
        ),
        pytest.param(
            _MON_OF_JUNETEENTH_WEEK,
            _THU_BEFORE_JUNETEENTH,
            ExpirySeries.MONTHLY,
            id="a third Friday that is Juneteenth moves the monthly to the Thursday",
        ),
        pytest.param(_MON_BEFORE, _FRI_NEXT, ExpirySeries.WEEKLY, id="another Friday is a weekly"),
        pytest.param(
            _MON_BEFORE,
            _FRI_LAST_OF_OCTOBER,
            ExpirySeries.WEEKLY,
            id="the last Friday of a month that is no quarter end",
        ),
        pytest.param(
            _MON_OF_THANKSGIVING_WEEK,
            _FRI_AFTER_THANKSGIVING,
            ExpirySeries.WEEKLY,
            id="a half-day Friday after Thanksgiving is a weekly",
        ),
        pytest.param(
            _TUE_OF_JULY_4_WEEK,
            _THU_OF_JULY_4_WEEK,
            ExpirySeries.WEEKLY,
            id="in a week whose Friday is a holiday the Thursday is the weekly",
        ),
        pytest.param(
            _MON_OF_CLOSED_THURSDAY_WEEK,
            _FRI_AFTER_CLOSED_THURSDAY,
            ExpirySeries.WEEKLY,
            id="a Friday after a one-off Thursday closure is still a weekly",
        ),
        pytest.param(
            _TUE_OF_JULY_4_WEEK, _WED_OF_JULY_4_WEEK, ExpirySeries.DAILY, id="the day before that Thursday is a daily"
        ),
        pytest.param(_MON_BEFORE, _WED, ExpirySeries.DAILY, id="a Wednesday is a daily"),
        pytest.param(_FRI_THIRD, _MON_AFTER, ExpirySeries.DAILY, id="a Monday is a daily"),
        pytest.param(
            _MON_BEFORE_MARCH_THIRD_FRIDAY,
            _MARCH_2025_THIRD_FRIDAY,
            ExpirySeries.MONTHLY,
            id="the third Friday of a quarter month is its monthly and not its quarterly",
        ),
        pytest.param(
            _MON_BEFORE_DECEMBER_THIRD_FRIDAY,
            _DECEMBER_2025_THIRD_FRIDAY,
            ExpirySeries.MONTHLY,
            id="the third Friday of December is its monthly too",
        ),
        pytest.param(
            _MON_BEFORE_Q1_2025_END, _Q1_2025_END, ExpirySeries.QUARTERLY, id="the last session of March is a quarterly"
        ),
        pytest.param(
            _MON_BEFORE_Q1_2025_END,
            _on(2025, 3, 28, "Fri"),
            ExpirySeries.WEEKLY,
            id="the Friday before that quarter end is only a weekly",
        ),
        pytest.param(
            _MON_OF_Q4_2027_END_WEEK,
            _Q4_2027_END_A_FRIDAY,
            ExpirySeries.QUARTERLY,
            id="a quarter end that is also a Friday is a quarterly and not a weekly",
        ),
        pytest.param(
            _MON_OF_Q3_2028_END_WEEK,
            _Q3_2028_END_NOT_THE_30TH,
            ExpirySeries.QUARTERLY,
            id="a quarter end is the last session, not the last day: September 30th 2028 is a Saturday",
        ),
        pytest.param(
            _MON_BEFORE,
            _on(2025, 10, 30, "Thu"),
            ExpirySeries.DAILY,
            id="the last Thursday of a month that is no quarter end is a daily",
        ),
    ],
)
def test_an_expiry_is_tagged_by_where_its_date_sits_on_the_calendar(
    trading_date: date, expiry: date, series: ExpirySeries
) -> None:
    assert _tag(expiry, trading_date).series is series


@pytest.mark.parametrize(
    ("expiry", "trading_date"),
    [
        pytest.param(_SAT, _MON_BEFORE, id="a Saturday"),
        pytest.param(_CLOSED_THURSDAY, _MON_OF_CLOSED_THURSDAY_WEEK, id="a weekday the exchange closed on"),
        pytest.param(_GOOD_FRIDAY_2025, _MON_OF_GOOD_FRIDAY_WEEK, id="a third Friday that is a holiday"),
        pytest.param(_JULY_4_2025, _TUE_OF_JULY_4_WEEK, id="a holiday Friday"),
    ],
)
def test_a_date_that_is_not_a_session_is_unknown(expiry: date, trading_date: date) -> None:
    tag = _tag(expiry, trading_date)

    assert tag.series is ExpirySeries.UNKNOWN
    assert tag.evidence.strip()


@pytest.mark.parametrize(
    ("trading_date", "expiry"),
    [
        pytest.param(_DAY_BEFORE_THE_FLOOR, _DAY_BEFORE_THE_FLOOR, id="expiring that day"),
        pytest.param(_DAY_BEFORE_THE_FLOOR, _FLOOR, id="expiring on the floor day, a Tuesday"),
        pytest.param(_DAY_BEFORE_THE_FLOOR, _FRI_AFTER_THE_FLOOR, id="a Friday that would be a weekly"),
        pytest.param(
            _MON_BEFORE_THE_FLOOR, _THIRD_FRIDAY_BEFORE_THE_FLOOR, id="a third Friday that would be a monthly"
        ),
    ],
)
def test_a_trading_date_before_the_floor_leaves_every_expiry_unknown_and_says_why(
    trading_date: date, expiry: date
) -> None:
    tag = _tag(expiry, trading_date)

    assert tag.series is ExpirySeries.UNKNOWN
    assert _FLOOR.isoformat() in tag.evidence


def test_the_floor_day_itself_is_tagged() -> None:
    assert _tag(_FRI_AFTER_THE_FLOOR, _FLOOR).series is ExpirySeries.WEEKLY


# ---- What the owner reads beside a tag ------------------------------------------

_THE_FLOOR_DAY = _FLOOR.isoformat()


@pytest.mark.parametrize(
    ("trading_date", "expiry", "series", "says", "never_says"),
    [
        pytest.param(
            _MON_BEFORE,
            _FRI_THIRD,
            ExpirySeries.MONTHLY,
            ("third Friday",),
            ("closed", "quarter", "week", "not a session", _THE_FLOOR_DAY),
            id="monthly on its Friday",
        ),
        pytest.param(
            _MON_OF_GOOD_FRIDAY_WEEK,
            _THU_BEFORE_GOOD_FRIDAY,
            ExpirySeries.MONTHLY,
            ("third Friday", "session before", "closed"),
            ("quarter", "week", "not a session", _THE_FLOOR_DAY),
            id="monthly moved to the session before a closed Friday",
        ),
        pytest.param(
            _MON_BEFORE_Q1_2025_END,
            _Q1_2025_END,
            ExpirySeries.QUARTERLY,
            ("quarter",),
            ("Friday", "closed", "week", "not a session", _THE_FLOOR_DAY),
            id="quarterly",
        ),
        pytest.param(
            _MON_BEFORE,
            _FRI_NEXT,
            ExpirySeries.WEEKLY,
            ("last session", "week"),
            ("Friday", "closed", "quarter", "not", _THE_FLOOR_DAY),
            id="weekly",
        ),
        pytest.param(
            _MON_BEFORE,
            _WED,
            ExpirySeries.DAILY,
            ("not", "last", "week"),
            ("Friday", "closed", "quarter", "not a session", _THE_FLOOR_DAY),
            id="daily",
        ),
        pytest.param(
            _MON_BEFORE,
            _SAT,
            ExpirySeries.UNKNOWN,
            ("not a session",),
            ("Friday", "closed", "quarter", "week", _THE_FLOOR_DAY),
            id="not a session",
        ),
        pytest.param(
            _DAY_BEFORE_THE_FLOOR,
            _FRI_AFTER_THE_FLOOR,
            ExpirySeries.UNKNOWN,
            (_THE_FLOOR_DAY,),
            ("Friday", "closed", "quarter", "week", "not a session"),
            id="before the floor",
        ),
    ],
)
def test_the_words_beside_a_tag_name_the_rule_that_decided_it_and_no_other(
    trading_date: date, expiry: date, series: ExpirySeries, says: tuple[str, ...], never_says: tuple[str, ...]
) -> None:
    tag = _tag(expiry, trading_date)

    assert tag.series is series
    for word in says:
        assert word in tag.evidence, f"{tag.evidence!r} does not say {word!r}"
    for word in never_says:
        assert word not in tag.evidence, f"{tag.evidence!r} says {word!r}, which is another rule's"


# ---- Expiring today ------------------------------------------------------------


def test_a_monthly_on_its_last_day_is_a_monthly_that_expires_today() -> None:
    tag = _tag(_FRI_THIRD, _FRI_THIRD)

    assert (tag.series, tag.expires_today, tag.days_to_expiry) == (ExpirySeries.MONTHLY, True, 0)


def test_a_quarterly_on_its_last_day_is_a_quarterly_that_expires_today() -> None:
    tag = _tag(_Q4_2025_END, _Q4_2025_END)

    assert (tag.series, tag.expires_today) == (ExpirySeries.QUARTERLY, True)


def test_a_daily_expiring_today_is_a_daily_that_expires_today() -> None:
    tag = _tag(_WED, _WED)

    assert (tag.series, tag.expires_today) == (ExpirySeries.DAILY, True)


def test_a_weekly_expiring_today_is_a_weekly_that_expires_today() -> None:
    tag = _tag(_FRI_NEXT, _FRI_NEXT)

    assert (tag.series, tag.expires_today) == (ExpirySeries.WEEKLY, True)


def test_an_expiry_on_the_trading_date_is_flagged_even_when_its_series_is_not_claimed() -> None:
    """A trading date before the floor: the series is not claimed, the date arithmetic still holds."""
    tag = _tag(_DAY_BEFORE_THE_FLOOR, _DAY_BEFORE_THE_FLOOR)

    assert (tag.series, tag.expires_today, tag.days_to_expiry) == (ExpirySeries.UNKNOWN, True, 0)


def test_an_expiry_after_the_trading_date_does_not_expire_today_and_counts_calendar_days() -> None:
    tag = _tag(_FRI_NEXT, _MON_BEFORE)

    assert (tag.expires_today, tag.days_to_expiry) == (False, 11)


# ---- What the listing is for ---------------------------------------------------


def test_a_tag_is_a_fact_of_the_date_and_does_not_change_with_what_else_is_listed() -> None:
    alone = tag_expiry(_FRI_THIRD, trading_date=_MON_BEFORE, listed=[_FRI_THIRD])
    among_others = tag_expiry(_FRI_THIRD, trading_date=_MON_BEFORE, listed=(_WED, _FRI_THIRD, _FRI_NEXT, _FRI_NEXT))

    assert alone == among_others


def test_an_expiry_the_listing_does_not_hold_is_a_callers_mistake() -> None:
    with pytest.raises(ValueError, match=str(_FRI_THIRD)):
        tag_expiry(_FRI_THIRD, trading_date=_MON_BEFORE, listed=[_WED, _FRI_NEXT])


def test_an_expiry_before_the_trading_date_is_a_callers_mistake() -> None:
    with pytest.raises(ValueError, match=str(_WED)):
        tag_expiry(_WED, trading_date=_FRI_THIRD, listed=[_WED, _FRI_NEXT])


# ---- The dates the tags are built on --------------------------------------------


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        pytest.param(2025, 10, _FRI_THIRD, id="October 2025: Fridays are the 3rd, 10th, 17th"),
        pytest.param(2025, 4, _THU_BEFORE_GOOD_FRIDAY, id="April 2025: the 18th is Good Friday"),
        pytest.param(2026, 6, _THU_BEFORE_JUNETEENTH, id="June 2026: the 19th is Juneteenth"),
        pytest.param(2026, 2, _on(2026, 2, 20, "Fri"), id="February 2026: the 20th, after Presidents Day"),
        pytest.param(2025, 11, _on(2025, 11, 21, "Fri"), id="November 2025: the month starts on a Saturday"),
    ],
)
def test_the_standard_monthly_date_is_the_third_friday_or_the_session_before_it(
    year: int, month: int, expected: date
) -> None:
    assert standard_monthly_date(year, month) == expected


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        pytest.param(2025, 3, _Q1_2025_END, id="a month ending on a Monday"),
        pytest.param(2028, 9, _Q3_2028_END_NOT_THE_30TH, id="a month ending on a Saturday"),
        pytest.param(2024, 3, _on(2024, 3, 28, "Thu"), id="a month ending on a Sunday, with Good Friday before it"),
        pytest.param(2025, 10, _FRI_LAST_OF_OCTOBER, id="a month that is no quarter end"),
    ],
)
def test_the_last_session_of_a_month_is_the_last_day_the_calendar_has_a_session_on(
    year: int, month: int, expected: date
) -> None:
    assert last_session_of_month(year, month) == expected


def test_a_quarter_end_is_never_the_standard_monthly_date() -> None:
    """The monthly falls between the 14th and the 21st; a quarter's last session is in its month's last days.

    So "a quarterly, when it is not the monthly" never has to choose, and no date of a real calendar can
    be tested for the choice.
    """
    for year in range(2024, 2036):
        for month in (3, 6, 9, 12):
            monthly = standard_monthly_date(year, month)
            quarter_end = last_session_of_month(year, month)

            assert 14 <= monthly.day <= 21
            assert quarter_end.day >= 26
            assert monthly != quarter_end
