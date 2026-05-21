"""Tests for the fixture-backed trading_sessions_for in Slice 1a.

Slice 1c replaces this with a LEAN market-hours-database-driven implementation;
the public function signature is stable.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import NonSessionRecord


def test_weekday_non_holiday_is_a_session():
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 20), date(2024, 5, 20))
    assert sessions == [date(2024, 5, 20)]  # Mon
    assert non_sessions == []


def test_weekend_is_excluded():
    # 2024-05-25 is a Saturday, 2024-05-26 is a Sunday.
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 25), date(2024, 5, 26))
    assert sessions == []
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 25), reason="weekend") in non_sessions
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 26), reason="weekend") in non_sessions


def test_memorial_day_2024_is_a_market_holiday():
    # 2024-05-27 is Memorial Day; market is closed.
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 27), date(2024, 5, 27))
    assert sessions == []
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 27), reason="market_holiday") in non_sessions


def test_week_spanning_a_holiday():
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 24), date(2024, 5, 31))
    # Fri 5/24 trading, Sat 5/25 weekend, Sun 5/26 weekend,
    # Mon 5/27 Memorial Day, Tue 5/28 trading, ..., Fri 5/31 trading.
    expected_sessions = [
        date(2024, 5, 24),
        date(2024, 5, 28),
        date(2024, 5, 29),
        date(2024, 5, 30),
        date(2024, 5, 31),
    ]
    assert sessions == expected_sessions
    holiday_dates = [n.trading_date for n in non_sessions if n.reason == "market_holiday"]
    assert date(2024, 5, 27) in holiday_dates


def test_uses_staged_market_hours_when_provided(tmp_path: Path):
    # Minimal market-hours-database.json with a full closure on 2024-07-04
    # (US Independence Day) and an early close on 2024-07-03.
    mh_db = tmp_path / "market-hours-database.json"
    mh_db.write_text(
        json.dumps(
            {
                "entries": {
                    "Equity-usa-[*]": {
                        "exchange": "nyse",
                        "timezone": "America/New_York",
                        "holidays": ["2024-07-04"],
                        "earlyCloses": {"2024-07-03": "13:00"},
                    }
                }
            }
        )
    )
    sessions, non_sessions = trading_sessions_for(
        "usa",
        date(2024, 7, 3),
        date(2024, 7, 5),
        market_hours_db_path=mh_db,
    )
    assert date(2024, 7, 4) not in sessions
    assert any(n.trading_date == date(2024, 7, 4) and n.reason == "market_holiday" for n in non_sessions)
    # Early-close day is still a session in v1 (full-minute coverage).
    assert date(2024, 7, 3) in sessions


def test_falls_back_to_hardcoded_when_no_path():
    # Memorial Day 2024 is in the hardcoded list; should be a non-session.
    sessions, _ = trading_sessions_for(
        "usa",
        date(2024, 5, 27),
        date(2024, 5, 27),
        market_hours_db_path=None,
    )
    assert sessions == []
