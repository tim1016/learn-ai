"""Bars a scheduled session holds, counted from the calendar (#3525).

The counts annualize the 15-minute Realized vs IV series, so they are checked against three things that do not share the
code under test: a pandas count of the same grid in New York wall-clock time, the HF estimator's own session masks (the
module that already says 26 and 64), and plain arithmetic on the hours the exchange and the vendor publish.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.engine.edge.features_realtime.hf_realized_vol import _et_session_mask
from app.engine.edge.session_bars import (
    DAY_BAR_MINUTES,
    PRE_MARKET_HEAD_MS,
    bars_in_session,
    full_session_bars,
)

ET = ZoneInfo("America/New_York")

ORDINARY = date(2024, 3, 12)
SPRING_FORWARD_MONDAY = date(2024, 3, 11)  # the day after the clocks went forward
FALL_BACK_MONDAY = date(2024, 11, 4)
EARLY_CLOSE = date(2024, 7, 3)  # closes 13:00 ET
HOLIDAY = date(2024, 7, 4)
SATURDAY = date(2024, 3, 9)


def grid(day: date, start: time, end: time, minutes: int) -> pd.DatetimeIndex:
    """Bar starts of ``minutes`` between two New York wall-clock times of ``day``, end exclusive."""
    first = datetime.combine(day, start, tzinfo=ET)
    last = datetime.combine(day, end, tzinfo=ET)
    return pd.date_range(first, last, freq=f"{minutes}min", inclusive="left")


@pytest.mark.parametrize(
    ("day", "regular", "extended"),
    [
        pytest.param(ORDINARY, 26, 64, id="ordinary"),
        pytest.param(SPRING_FORWARD_MONDAY, 26, 64, id="after spring forward"),
        pytest.param(FALL_BACK_MONDAY, 26, 64, id="after fall back"),
        pytest.param(EARLY_CLOSE, 14, 52, id="early close: 13:00 ends the tape at 17:00"),
    ],
)
def test_fifteen_minute_bars_in_a_session_match_a_pandas_count_of_the_wall_clock_grid(day, regular, extended):
    close = time(13, 0) if day == EARLY_CLOSE else time(16, 0)
    after_hours_end = time(17, 0) if day == EARLY_CLOSE else time(20, 0)

    assert bars_in_session(day, 15, "regular") == len(grid(day, time(9, 30), close, 15)) == regular
    assert bars_in_session(day, 15, "extended") == len(grid(day, time(4, 0), after_hours_end, 15)) == extended


def test_the_extended_count_is_the_hf_estimators_eth_session_and_the_regular_count_its_rth_session():
    """``hf_realized_vol`` states 26 for RTH and 64 for ETH, and masks the bars that way."""
    starts = grid(ORDINARY, time(0, 0), time(23, 59), 15).tz_convert("UTC")
    frame = pd.DataFrame({"close": 1.0}, index=starts)

    assert int(_et_session_mask(frame.index, "ETH").sum()) == bars_in_session(ORDINARY, 15, "extended")
    assert int(_et_session_mask(frame.index, "RTH").sum()) == bars_in_session(ORDINARY, 15, "regular")


def test_the_pre_market_head_is_half_past_nine_less_four_oclock():
    assert (datetime(2024, 3, 12, 9, 30) - datetime(2024, 3, 12, 4, 0)) // timedelta(milliseconds=1) == PRE_MARKET_HEAD_MS


@pytest.mark.parametrize("session", ["regular", "extended"])
@pytest.mark.parametrize("day", [ORDINARY, EARLY_CLOSE])
def test_a_daily_bar_is_one_a_session_whatever_the_session(day, session):
    assert bars_in_session(day, DAY_BAR_MINUTES, session) == 1


@pytest.mark.parametrize("day", [HOLIDAY, SATURDAY])
def test_a_day_that_is_not_a_session_has_no_count(day):
    with pytest.raises(LookupError):
        bars_in_session(day, 15, "regular")
    with pytest.raises(LookupError):
        bars_in_session(day, DAY_BAR_MINUTES, "regular")


@pytest.mark.parametrize("minutes", [0, -15, DAY_BAR_MINUTES + 1])
def test_a_bar_length_outside_a_minute_to_a_day_is_refused(minutes):
    with pytest.raises(ValueError, match="bar_minutes must be from 1"):
        bars_in_session(ORDINARY, minutes, "regular")


def test_a_bar_that_does_not_divide_the_session_counts_the_whole_bars():
    assert bars_in_session(ORDINARY, 60, "regular") == 6, "390 minutes hold six whole hours"
    assert bars_in_session(ORDINARY, 60, "extended") == 16


def test_a_full_session_is_the_longest_of_the_window_so_an_early_close_does_not_shrink_a_year():
    assert full_session_bars([EARLY_CLOSE, ORDINARY], 15, "regular") == 26
    assert full_session_bars([ORDINARY, EARLY_CLOSE], 15, "extended") == 64
    assert full_session_bars([EARLY_CLOSE], 15, "regular") == 14, "a window of only early closes is counted as it is"
    assert full_session_bars([ORDINARY, EARLY_CLOSE], DAY_BAR_MINUTES, "regular") == 1


def test_counting_no_sessions_is_refused_instead_of_guessed():
    with pytest.raises(ValueError, match="no sessions"):
        full_session_bars([], 15, "regular")
