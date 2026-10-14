"""A decision is stale after more than one whole regular session of silence (#3217).

The silence is counted in market time: nights, weekends and holidays hold no
session, and an early close is one session at its own length. The dates are
2025's, whose schedule is settled: Friday 2025-11-21 and Monday 2025-11-24 are
ordinary sessions, Thanksgiving is Thursday 2025-11-27 and the Friday after it
closes at 13:00 ET.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.broker_v2_panel.panel_projection_service import decision_is_stale

_NY = ZoneInfo("America/New_York")


def _et(month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(2025, month, day, hour, minute, tzinfo=_NY).timestamp() * 1000)


def test_a_weekend_is_not_silence() -> None:
    """Friday's last decision, read before Monday's open: 64 wall-clock hours, one minute of market time."""
    assert decision_is_stale(_et(11, 21, 15, 59), _et(11, 24, 8, 0)) is False


def test_the_silence_is_whole_sessions_not_wall_clock_hours() -> None:
    # Friday's last decision, read at Monday 09:31: one minute of Friday and one of Monday.
    assert decision_is_stale(_et(11, 21, 15, 59), _et(11, 24, 9, 31)) is False
    # The whole of Monday's session passed with no decision, and Tuesday's open is not even needed.
    assert decision_is_stale(_et(11, 21, 15, 59), _et(11, 24, 16, 0)) is True


def test_six_hours_of_session_silence_is_not_yet_a_session() -> None:
    """The rule is a whole session of silence: 6 h of a 6.5 h session reads as on time."""
    assert decision_is_stale(_et(11, 24, 9, 35), _et(11, 24, 15, 35)) is False


def test_silence_is_stale_only_past_one_whole_session_of_market_time() -> None:
    last = _et(11, 24, 9, 35)  # 385 minutes of Monday's session remain; Tuesday's open is the 386th
    # 385 + 5 = 390 minutes of market time: exactly one session, not more than one.
    assert decision_is_stale(last, _et(11, 25, 9, 35)) is False
    assert decision_is_stale(last, _et(11, 25, 9, 36)) is True


def test_an_early_close_is_one_session_at_its_own_length() -> None:
    last = _et(11, 26, 15, 59)  # Wednesday, a minute before the close; Thursday is Thanksgiving
    # Friday's half-day session is 09:30-13:00; 150 of its 210 minutes is not a whole session ...
    assert decision_is_stale(last, _et(11, 28, 12, 0)) is False
    # ... and all of it, with the last minute of Wednesday, is more than one.
    assert decision_is_stale(last, _et(11, 28, 13, 0)) is True


def test_a_holiday_holds_no_session() -> None:
    """41 wall-clock hours, but Thursday was closed: a minute of Wednesday and a minute of Friday."""
    assert decision_is_stale(_et(11, 26, 15, 59), _et(11, 28, 9, 31)) is False


def test_a_decision_stamped_after_now_is_not_stale() -> None:
    assert decision_is_stale(_et(11, 24, 12, 0), _et(11, 24, 11, 0)) is False
