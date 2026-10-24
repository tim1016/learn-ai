"""When a minute capture of a session is final (#3521).

The closing time comes from the canonical calendar; the vendor's after-hours tail and delivery delay are the two
constants under test. Nothing here names a clock time except to pin what the calendar says about the days chosen.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.data_lake.capture_finality import (
    AFTER_HOURS_TAIL_MS,
    VENDOR_DELAY_MS,
    capture_final_ms,
    captured_early,
)
from app.lean_sidecar.trading_calendar import session_close_ms_utc

ET = ZoneInfo("America/New_York")
WINTER = date(2024, 3, 8)  # EST
SUMMER = date(2024, 3, 11)  # EDT, the session after the spring-forward Sunday
EARLY_CLOSE = date(2024, 7, 3)


def _wall_clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, ET).strftime("%H:%M")


def test_a_regular_capture_is_final_a_vendor_delay_after_the_close():
    assert capture_final_ms(WINTER, "regular") == session_close_ms_utc(WINTER) + VENDOR_DELAY_MS
    assert _wall_clock(capture_final_ms(WINTER, "regular")) == "16:15"


def test_an_extended_capture_waits_for_the_after_hours_tape_as_well():
    assert capture_final_ms(WINTER, "extended") - capture_final_ms(WINTER, "regular") == AFTER_HOURS_TAIL_MS
    assert _wall_clock(capture_final_ms(WINTER, "extended")) == "20:15"


@pytest.mark.parametrize("day", [WINTER, SUMMER], ids=["est", "edt"])
def test_the_wall_clock_instant_is_the_same_on_both_sides_of_a_dst_change(day):
    """The close is the calendar's, so the offset from UTC moves with DST and the New York wall clock does not."""
    assert _wall_clock(capture_final_ms(day, "extended")) == "20:15"


def test_an_early_close_ends_its_tape_early():
    assert _wall_clock(session_close_ms_utc(EARLY_CLOSE)) == "13:00", "the calendar, not this test, sets the close"
    assert _wall_clock(capture_final_ms(EARLY_CLOSE, "regular")) == "13:15"
    assert _wall_clock(capture_final_ms(EARLY_CLOSE, "extended")) == "17:15"


def test_a_capture_is_early_up_to_the_final_instant_and_final_from_it():
    final = capture_final_ms(WINTER, "extended")

    assert captured_early(final - 1, WINTER, "extended") is True
    assert captured_early(final, WINTER, "extended") is False
    assert captured_early(final + 1, WINTER, "extended") is False


def test_the_same_capture_can_be_final_for_a_regular_reader_and_early_for_an_extended_one():
    between = capture_final_ms(WINTER, "regular") + 1

    assert captured_early(between, WINTER, "regular") is False
    assert captured_early(between, WINTER, "extended") is True


def test_a_capture_with_no_recorded_time_is_never_called_early():
    assert captured_early(None, WINTER, "extended") is False
