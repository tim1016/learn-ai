"""``check_availability`` derives its expected sessions from the trading calendar.

It used to walk weekdays and count every exchange holiday as a missing day,
so a window containing one never reported complete — a two-year window has
roughly eighteen, which would have refused essentially every Grid Search.
Confirmed live before the fix: SPY 2025-01-01 → 2025-04-01 reported four
missing days that were all closures (New Year's Day, the Carter national day
of mourning, MLK Day, Presidents' Day). PRD #1926, "Data availability".
"""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

from app.engine.data.availability import MissingSessionsError, check_availability
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


def _daily_row(day: date, *, columns: int = 6) -> str:
    fields = [f"{day.strftime('%Y%m%d')} 00:00", "5000000", "5010000", "4990000", "5005000", "1000"]
    return ",".join(fields[:columns])


def _write_daily_zip(root: Path, members: dict[str, list[str]]) -> None:
    path = root / "equity" / "usa" / "daily" / "spy.zip"
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, rows in members.items():
            zf.writestr(name, "\n".join(rows) + "\n")


def test_a_daily_session_is_available_only_when_the_reader_parses_its_row(tmp_path: Path) -> None:
    """A line that merely begins with the date is not a bar the run will read (#2445 review)."""
    window = (date(2024, 11, 25), date(2024, 11, 29))
    truncated = date(2024, 11, 29)
    sessions = expected_sessions(*window)
    _write_daily_zip(tmp_path, {"spy.csv": [_daily_row(day, columns=4 if day == truncated else 6) for day in sessions]})

    report = check_availability([tmp_path], "SPY", *window, resolution="daily")

    assert report.missing_days == [truncated]


def test_daily_availability_reads_the_member_the_reader_reads(tmp_path: Path) -> None:
    """The reader opens ``{symbol}.csv`` when the zip holds one, whichever member comes first."""
    window = (date(2024, 11, 25), date(2024, 11, 29))
    sessions = expected_sessions(*window)
    _write_daily_zip(
        tmp_path,
        {"notes.csv": [_daily_row(day) for day in sessions], "spy.csv": [_daily_row(day) for day in sessions[:-1]]},
    )

    report = check_availability([tmp_path], "SPY", *window, resolution="daily")

    assert report.missing_days == [sessions[-1]]


def test_missing_sessions_either_side_of_a_closure_are_one_span(tmp_path: Path) -> None:
    """New Year's Day is no session, so the 12-31 and 01-02 holes are one run of sessions."""
    window = (date(2025, 12, 30), date(2026, 1, 2))
    assert date(2026, 1, 1) not in expected_sessions(*window)
    _seed(tmp_path, [date(2025, 12, 30)])

    report = check_availability([tmp_path], "SPY", *window)

    assert report.missing_spans == [(date(2025, 12, 31), date(2026, 1, 2))]


def test_a_year_long_gap_is_named_as_one_range(tmp_path: Path) -> None:
    """The refusal compresses a hole into ranges instead of listing every date (#2445)."""
    year = (date(2025, 1, 1), date(2025, 12, 31))
    sessions = expected_sessions(*year)

    error = MissingSessionsError(check_availability([tmp_path], "QQQ", *year))

    assert str(error) == (
        f"missing data: QQQ has no minute bars for {len(sessions)} of {len(sessions)} trading sessions "
        f"in 2025-01-01..2025-12-31 — missing {sessions[0].isoformat()}..{sessions[-1].isoformat()}"
    )


def test_gaps_past_the_shown_limit_are_counted_not_listed(tmp_path: Path) -> None:
    sessions = expected_sessions(*WINDOW)
    _seed(tmp_path, sessions[1::2])  # every other session held: one gap per missing session

    report = check_availability([tmp_path], "SPY", *WINDOW)
    message = str(MissingSessionsError(report))

    assert len(report.missing_spans) > 10
    assert f"(+{len(report.missing_spans) - 10} more gaps)" in message
    assert report.missing_spans[9][0].isoformat() in message
    assert report.missing_spans[10][0].isoformat() not in message
