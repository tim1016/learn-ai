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
    WindowSummary,
    summarize_window,
)

MEMORIAL_DAY = (date(2026, 5, 25), "holiday", "Memorial Day")
SATURDAY = (date(2026, 5, 23), "weekend", None)
SUNDAY = (date(2026, 5, 24), "weekend", None)


@pytest.mark.parametrize(
    ("start", "end", "included", "excluded"),
    [
        # The window brackets Memorial Day plus a Sat + Sun (2026-05-23 / 2026-05-24).
        # Five trading sessions: Tue 5/19, Wed 5/20, Thu 5/21, Fri 5/22, and
        # Tue 5/26 (the inclusive end day after Memorial Day Monday).
        pytest.param(
            date(2026, 5, 19),
            date(2026, 5, 26),
            (
                date(2026, 5, 19),
                date(2026, 5, 20),
                date(2026, 5, 21),
                date(2026, 5, 22),
                date(2026, 5, 26),
            ),
            (SATURDAY, SUNDAY, MEMORIAL_DAY),
            id="flags-memorial-day-2026",
        ),
        # A Sat→Sun (inclusive) window has zero sessions, two excluded.
        pytest.param(
            date(2026, 5, 23),
            date(2026, 5, 24),
            (),
            (SATURDAY, SUNDAY),
            id="pure-weekend-returns-no-sessions",
        ),
        # Mon→Fri (inclusive) covers all five weekdays with no holidays.
        pytest.param(
            date(2026, 6, 1),
            date(2026, 6, 5),
            (
                date(2026, 6, 1),
                date(2026, 6, 2),
                date(2026, 6, 3),
                date(2026, 6, 4),
                date(2026, 6, 5),
            ),
            (),
            id="all-trading-days-has-no-excluded",
        ),
        # start == end is a valid single-day inclusive window.
        pytest.param(
            date(2026, 5, 26),
            date(2026, 5, 26),
            (date(2026, 5, 26),),
            (),
            id="same-day-trading-day-returns-one-session",
        ),
        # start == end on Memorial Day returns the holiday as excluded.
        pytest.param(
            date(2026, 5, 25),
            date(2026, 5, 25),
            (),
            (MEMORIAL_DAY,),
            id="same-day-holiday-returns-excluded",
        ),
    ],
)
def test_summarize_window_splits_the_window_into_sessions_and_excluded_days(
    start: date,
    end: date,
    included: tuple[date, ...],
    excluded: tuple[tuple[date, str, str | None], ...],
):
    summary = summarize_window(start, end)

    assert isinstance(summary, WindowSummary), "returns a WindowSummary"
    assert summary.requested_start_date == start, "echoes the requested start date"
    assert summary.requested_end_date == end, "echoes the requested end date"
    assert summary.sessions_included == list(included), "trading sessions inside the window"
    assert len(summary.sessions_excluded) == len(excluded), "one excluded entry per non-session day"
    assert {(ex.date, ex.reason, ex.name) for ex in summary.sessions_excluded} == set(excluded), (
        "each excluded day carries its reason and holiday name"
    )


def test_summarize_window_rejects_end_before_start():
    with pytest.raises(ValueError, match="end must be on or after start"):
        summarize_window(date(2026, 5, 26), date(2026, 5, 25))
