"""Unit tests for ``app.research.runs.window``.

The motivating bug: a "last 7 calendar days" SPY backtest silently
became a 5-trading-day backtest because Memorial Day + the weekend
fell out without surfacing. ``WindowSummary`` makes the calendar
breakdown explicit so the API can return it and persist it to the
ledger.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.research.runs.window import (
    ExcludedDay,
    WindowSummary,
    summarize_window,
)


def test_summarize_window_flags_memorial_day_2026():
    summary = summarize_window(date(2026, 5, 19), date(2026, 5, 26))

    assert isinstance(summary, WindowSummary)
    assert summary.requested_start_date == date(2026, 5, 19)
    assert summary.requested_end_date == date(2026, 5, 26)

    excluded_by_date = {ex.date: ex for ex in summary.sessions_excluded}
    memorial_day = date(2026, 5, 25)
    assert memorial_day in excluded_by_date
    ex = excluded_by_date[memorial_day]
    assert ex.reason == "holiday"
    assert ex.name == "Memorial Day"

    # The window also brackets a Sat + Sun (2026-05-23 / 2026-05-24).
    for weekend_date in (date(2026, 5, 23), date(2026, 5, 24)):
        assert weekend_date in excluded_by_date
        we = excluded_by_date[weekend_date]
        assert we.reason == "weekend"
        assert we.name is None

    # And four trading sessions slipped through: Tue 5/19, Wed 5/20,
    # Thu 5/21, Fri 5/22. Memorial Day is Monday 5/25; end is exclusive.
    assert summary.sessions_included == [
        date(2026, 5, 19),
        date(2026, 5, 20),
        date(2026, 5, 21),
        date(2026, 5, 22),
    ]


def test_summarize_window_pure_weekend_returns_no_sessions():
    """A Sat→Mon (exclusive) window has zero sessions, two excluded."""
    summary = summarize_window(date(2026, 5, 23), date(2026, 5, 25))

    assert summary.sessions_included == []
    assert {ex.date for ex in summary.sessions_excluded} == {
        date(2026, 5, 23),
        date(2026, 5, 24),
    }
    assert all(ex.reason == "weekend" for ex in summary.sessions_excluded)
    assert all(ex.name is None for ex in summary.sessions_excluded)


def test_summarize_window_all_trading_days_has_no_excluded():
    """Mon→Sat (exclusive) covers Mon-Fri with no holidays."""
    summary = summarize_window(date(2026, 6, 1), date(2026, 6, 6))

    assert summary.sessions_excluded == []
    assert len(summary.sessions_included) == 5
    assert summary.sessions_included[0] == date(2026, 6, 1)
    assert summary.sessions_included[-1] == date(2026, 6, 5)


def test_summarize_window_rejects_end_le_start():
    with pytest.raises(ValueError, match="end must be strictly after start"):
        summarize_window(date(2026, 5, 26), date(2026, 5, 26))
    with pytest.raises(ValueError, match="end must be strictly after start"):
        summarize_window(date(2026, 5, 26), date(2026, 5, 25))


def test_excluded_day_serializes_date_as_iso_string():
    ex = ExcludedDay(date=date(2026, 5, 25), reason="holiday", name="Memorial Day")
    dumped = ex.model_dump(mode="json")
    assert dumped == {
        "date": "2026-05-25",
        "reason": "holiday",
        "name": "Memorial Day",
    }


def test_window_summary_serializes_dates_as_iso_strings():
    summary = summarize_window(date(2026, 5, 19), date(2026, 5, 26))
    dumped = summary.model_dump(mode="json")

    assert dumped["requested_start_date"] == "2026-05-19"
    assert dumped["requested_end_date"] == "2026-05-26"
    assert all(isinstance(d, str) for d in dumped["sessions_included"])
    assert all(isinstance(ex["date"], str) for ex in dumped["sessions_excluded"])
