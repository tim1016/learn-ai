"""The owner's words for an instant in ET (#2665).

``et_when_words`` is the one author of backend copy that names a day and
minute in ET: a bot's end, the Start window's next open, the Deploy exit
steps, the end-sale alert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.utils.et_words import et_when_words
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return to_ms_utc(datetime(year, month, day, hour, minute, tzinfo=_ET))


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return to_ms_utc(datetime(year, month, day, hour, minute, tzinfo=UTC))


@pytest.mark.parametrize(
    ("instant_ms", "now_ms", "words"),
    [
        pytest.param(_et(2026, 9, 30, 15, 59), _et(2026, 9, 30, 10), "Wed Sep 30, 15:59 ET", id="this-year"),
        pytest.param(_et(2026, 10, 1, 4), _et(2026, 9, 30, 10), "Thu Oct 1, 04:00 ET", id="day-unpadded"),
        pytest.param(_et(2027, 1, 5, 15, 59), _et(2026, 9, 30, 10), "Tue Jan 5 2027, 15:59 ET", id="next-year"),
        pytest.param(_et(2026, 12, 30, 15, 59), _et(2027, 1, 4, 10), "Wed Dec 30 2026, 15:59 ET", id="last-year"),
        # 20:00 ET on Dec 31 is already Jan 1 in UTC: the year is ET's, on both sides.
        pytest.param(_et(2026, 12, 31, 21), _et(2026, 12, 31, 20), "Thu Dec 31, 21:00 ET", id="et-year-not-utc"),
        pytest.param(_et(2027, 1, 4, 4), _et(2026, 12, 31, 20), "Mon Jan 4 2027, 04:00 ET", id="et-new-year"),
    ],
)
def test_et_when_words_names_the_day_and_minute_with_the_year_only_when_it_differs(
    instant_ms: int, now_ms: int, words: str,
) -> None:
    """Never "today": the owner may not be in ET, where today can be their tomorrow."""
    assert et_when_words(instant_ms, now_ms=now_ms) == words


def test_et_when_words_follows_the_new_york_zone_across_dst() -> None:
    """The clock comes from the NY zone, never a fixed offset: 2026-03-08 springs 01:59 EST to 03:00 EDT."""
    now = _et(2026, 3, 1, 10)

    assert et_when_words(_utc(2026, 3, 8, 6, 59), now_ms=now) == "Sun Mar 8, 01:59 ET"
    assert et_when_words(_utc(2026, 3, 8, 7, 0), now_ms=now) == "Sun Mar 8, 03:00 ET"
    # The same UTC minute reads an hour later once daylight time has begun.
    assert et_when_words(_utc(2026, 3, 6, 14, 30), now_ms=now) == "Fri Mar 6, 09:30 ET"
    assert et_when_words(_utc(2026, 3, 9, 14, 30), now_ms=now) == "Mon Mar 9, 10:30 ET"
