"""``check_availability`` derives its expected sessions from the trading calendar.

It used to walk weekdays and count every exchange holiday as a missing day,
so a window containing one never reported complete — a two-year window has
roughly eighteen, which would have refused essentially every Grid Search.
Confirmed live before the fix: SPY 2025-01-01 → 2025-04-01 reported four
missing days that were all closures (New Year's Day, the Carter national day
of mourning, MLK Day, Presidents' Day). PRD #1926, "Data availability".
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.engine.data.availability import check_availability
from app.lean_sidecar.trading_calendar import expected_sessions
from tests._helpers.lean_store import seed_store_day

WINDOW = (date(2025, 1, 1), date(2025, 4, 1))
CLOSURES = (date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17))


def _seed(root: Path, days: list[date]) -> None:
    for day in days:
        seed_store_day(root, "SPY", day)


def test_a_fully_backfilled_window_with_closures_is_complete(tmp_path: Path) -> None:
    sessions = expected_sessions(*WINDOW)
    assert all(closure not in sessions for closure in CLOSURES)
    _seed(tmp_path, sessions)

    report = check_availability([tmp_path], "SPY", *WINDOW)

    assert report.is_complete
    assert report.expected_days == len(sessions)
    assert report.missing_days == []


def test_a_genuinely_missing_session_is_named_and_a_closure_is_not(tmp_path: Path) -> None:
    sessions = expected_sessions(*WINDOW)
    absent = date(2025, 3, 3)
    _seed(tmp_path, [day for day in sessions if day != absent])

    report = check_availability([tmp_path], "SPY", *WINDOW)

    assert not report.is_complete
    assert report.missing_days == [absent]


def test_an_early_close_is_an_expected_session(tmp_path: Path) -> None:
    window = (date(2024, 11, 25), date(2024, 11, 29))  # Thanksgiving week; Black Friday closes at 13:00 ET
    sessions = expected_sessions(*window)
    assert date(2024, 11, 29) in sessions
    assert date(2024, 11, 28) not in sessions
    _seed(tmp_path, sessions)

    report = check_availability([tmp_path], "SPY", *window)

    assert report.expected_days == 4
    assert report.is_complete


def test_daily_resolution_uses_the_same_calendar(tmp_path: Path) -> None:
    report = check_availability([tmp_path], "SPY", *WINDOW, resolution="daily")

    assert report.expected_days == len(expected_sessions(*WINDOW))
    assert report.available_days == 0
