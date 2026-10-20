"""The lake's session adapter, and its parity with the canonical calendar.

``trading_sessions_for`` is a thin adapter over
``app.lean_sidecar.trading_calendar`` -- the canonical NYSE calendar and the
only ``mcal`` construction in the repo. The parity test at the bottom is what
makes that claim checkable rather than aspirational (ADR 0022 (d)).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import NonSessionRecord
from app.lean_sidecar.trading_calendar import expected_sessions


@pytest.mark.parametrize(
    ("start", "end", "expected_sessions", "expected_non_sessions"),
    [
        pytest.param(
            date(2024, 5, 20),
            date(2024, 5, 20),
            (date(2024, 5, 20),),  # Mon
            (),
            id="weekday_non_holiday_is_a_session",
        ),
        pytest.param(
            date(2024, 5, 27),
            date(2024, 5, 27),
            (),
            ((date(2024, 5, 27), "market_holiday"),),  # Memorial Day; market is closed.
            id="memorial_day_2024_is_a_market_holiday",
        ),
        # Fri 5/24 trading, Sat 5/25 weekend, Sun 5/26 weekend,
        # Mon 5/27 Memorial Day, Tue 5/28 trading, ..., Fri 5/31 trading.
        pytest.param(
            date(2024, 5, 24),
            date(2024, 5, 31),
            (date(2024, 5, 24), date(2024, 5, 28), date(2024, 5, 29), date(2024, 5, 30), date(2024, 5, 31)),
            ((date(2024, 5, 25), "weekend"), (date(2024, 5, 26), "weekend"), (date(2024, 5, 27), "market_holiday")),
            id="week_spanning_a_holiday",
        ),
        # The regression the consolidation closes, pinned to a date. The hand-maintained
        # holiday list this module used to carry covered only 2024-2026, so every market
        # holiday before 2024 read as a trading session: the lake required artifacts for
        # Christmas Day and recorded phantom sessions in its catalog. Any backfill reaching
        # back a few years -- well inside the coverage endpoint's five-year cap -- hit it.
        pytest.param(
            date(2020, 12, 25),
            date(2020, 12, 25),
            (),
            ((date(2020, 12, 25), "market_holiday"),),
            id="a_pre_2024_holiday_is_no_longer_treated_as_a_session",
        ),
    ],
)
def test_a_window_splits_into_sessions_and_reasoned_non_sessions(start, end, expected_sessions, expected_non_sessions):
    sessions, non_sessions = trading_sessions_for("usa", start, end)

    assert sessions == list(expected_sessions), "the sessions are exactly the trading days in the window"
    assert non_sessions == [
        NonSessionRecord(market="usa", trading_date=day, reason=reason) for day, reason in expected_non_sessions
    ], "every other day is a non-session carrying its reason"


def test_weekend_is_excluded():
    # 2024-05-25 is a Saturday, 2024-05-26 is a Sunday.
    sessions, non_sessions = trading_sessions_for("usa", date(2024, 5, 25), date(2024, 5, 26))
    assert sessions == []
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 25), reason="weekend") in non_sessions
    assert NonSessionRecord(market="usa", trading_date=date(2024, 5, 26), reason="weekend") in non_sessions


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2020, 1, 1), date(2021, 12, 31)),
        (date(2022, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2026, 12, 31)),
    ],
)
def test_sessions_are_the_canonical_calendar_verbatim(start: date, end: date) -> None:
    """The adapter adds a vocabulary, never a calendar opinion.

    Required by ADR 0022 (d): a duplicate that exists for a real
    reason (here, layer-locality -- the lake's catalog wants a reason per
    skipped day) carries a parity test naming the canonical file. That file is
    ``app/lean_sidecar/trading_calendar.py``.
    """
    sessions, _ = trading_sessions_for("usa", start, end)

    assert sessions == expected_sessions(start, end)


def test_every_day_in_the_window_is_either_a_session_or_a_reasoned_non_session() -> None:
    """No day falls between the two lists, and none is in both."""
    start, end = date(2024, 12, 20), date(2025, 1, 5)
    sessions, non_sessions = trading_sessions_for("usa", start, end)

    accounted = [*sessions, *(n.trading_date for n in non_sessions)]
    assert sorted(accounted) == sorted(set(accounted))
    assert len(accounted) == (end - start).days + 1


def test_a_market_this_calendar_does_not_cover_is_refused() -> None:
    with pytest.raises(ValueError, match="canonical calendar is NYSE"):
        trading_sessions_for("eurex", date(2024, 5, 20), date(2024, 5, 20))
