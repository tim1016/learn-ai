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
from decimal import InvalidOperation
from pathlib import Path

import pytest

from app.engine.data.availability import MissingSessionsError, check_availability
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import expected_sessions
from tests._helpers.lean_store import seed_pre_market_day, seed_store_day

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


def test_an_undecodable_daily_history_is_refused_as_unreadable_naming_the_file(tmp_path: Path) -> None:
    """The reader aborts on it, so its sessions are unreadable, not missing: a backfill would not repair it (#2489)."""
    window = (date(2024, 11, 25), date(2024, 11, 29))
    path = tmp_path / "equity" / "usa" / "daily" / "spy.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a zip")

    report = check_availability([tmp_path], "SPY", *window, resolution="daily")

    assert not report.is_complete
    assert report.missing_days == []
    assert report.unreadable_days == expected_sessions(*window)
    assert str(MissingSessionsError(report)) == (
        "unreadable data: SPY daily data for 4 of 4 trading sessions in 2024-11-25..2024-11-29 "
        f"is on disk but cannot be read — {path} (BadZipFile: File is not a zip file)"
    )


def test_a_later_daily_root_does_not_cover_for_an_unreadable_earlier_one(tmp_path: Path) -> None:
    """The reader merges every root's history and aborts on the first it cannot decode (#2475 review, Codex P2).

    Attributing the sessions to the later root admitted a window whose read
    was then guaranteed to fail.
    """
    window = (date(2024, 11, 25), date(2024, 11, 29))
    damaged, complete = tmp_path / "damaged", tmp_path / "complete"
    damaged_zip = damaged / "equity" / "usa" / "daily" / "spy.zip"
    damaged_zip.parent.mkdir(parents=True)
    damaged_zip.write_bytes(b"not a zip")
    complete.mkdir()
    _write_daily_zip(complete, {"spy.csv": [_daily_row(day) for day in expected_sessions(*window)]})

    report = check_availability([damaged, complete], "SPY", *window, resolution="daily")

    assert not report.is_complete
    assert report.available_days == 0
    assert report.sources[str(complete)] == []
    assert [file.path for file in report.unreadable_files] == [str(damaged_zip)]
    with pytest.raises(zipfile.BadZipFile):
        list(LeanDailyDataReader([damaged, complete]).iter_bars("SPY", *window))


# A minute window whose Tuesday is the session under test.
MINUTE_WINDOW = (date(2024, 12, 2), date(2024, 12, 6))
PROBED = date(2024, 12, 3)


def _minute_zip(root: Path, day: date) -> Path:
    return root / "equity" / "usa" / "minute" / "spy" / f"{day.strftime('%Y%m%d')}_trade.zip"


def _write_minute_csv(root: Path, day: date, rows: list[str]) -> None:
    with zipfile.ZipFile(_minute_zip(root, day), "w") as zf:
        zf.writestr(f"{day.strftime('%Y%m%d')}_spy_minute_trade.csv", "\n".join(rows) + "\n")


def _no_regular_hours_bar(root: Path, kind: str) -> None:
    """Replace the probed session's zip with one the regular-session reader reads no bar from."""
    if kind == "pre-market only":
        seed_pre_market_day(root, "SPY", PROBED)
    elif kind == "empty csv":
        _write_minute_csv(root, PROBED, [])
    else:  # "truncated rows": every row lacks its volume, so the reader drops each one
        rows = [f"{34_200_000 + i * 60_000},5000000,5000000,5000000,5000000" for i in range(390)]
        _write_minute_csv(root, PROBED, rows)


def _undecodable(root: Path, kind: str) -> None:
    """Replace the probed session's zip with one the reader raises on."""
    path = _minute_zip(root, PROBED)
    if kind == "not a zip":
        path.write_bytes(b"not a zip")
    elif kind == "empty archive":
        zipfile.ZipFile(path, "w").close()
    else:  # "garbled row"
        _write_minute_csv(root, PROBED, ["34200000,five,5000000,5000000,5000000,100"])


NO_REGULAR_HOURS_BAR = ["pre-market only", "empty csv", "truncated rows"]
# Each undecodable zip, and what the reader raises on it.
UNDECODABLE = {"not a zip": zipfile.BadZipFile, "empty archive": IndexError, "garbled row": InvalidOperation}


@pytest.mark.parametrize("kind", NO_REGULAR_HOURS_BAR)
def test_a_minute_session_whose_zip_yields_no_regular_hours_bar_is_missing(tmp_path: Path, kind: str) -> None:
    """A zip on disk is not a session the reader reads (#2475 review, Codex P1)."""
    _seed(tmp_path, expected_sessions(*MINUTE_WINDOW))
    _no_regular_hours_bar(tmp_path, kind)

    report = check_availability([tmp_path], "SPY", *MINUTE_WINDOW)

    assert report.missing_days == [PROBED]
    assert report.unreadable_files == []
    assert LeanMinuteDataReader([tmp_path]).read_day("SPY", PROBED) == []


@pytest.mark.parametrize("kind", UNDECODABLE)
def test_a_minute_zip_the_reader_cannot_decode_is_unreadable_not_missing(tmp_path: Path, kind: str) -> None:
    _seed(tmp_path, expected_sessions(*MINUTE_WINDOW))
    _undecodable(tmp_path, kind)

    report = check_availability([tmp_path], "SPY", *MINUTE_WINDOW)
    message = str(MissingSessionsError(report))

    assert not report.is_complete
    assert report.missing_days == []
    assert report.unreadable_days == [PROBED]
    assert [file.path for file in report.unreadable_files] == [str(_minute_zip(tmp_path, PROBED))]
    assert message.startswith("unreadable data: SPY minute data for 1 of 5 trading sessions")
    assert str(_minute_zip(tmp_path, PROBED)) in message
    assert "missing" not in message
    assert report.unreadable_files[0].reason.startswith(UNDECODABLE[kind].__name__)
    with pytest.raises(UNDECODABLE[kind]):
        LeanMinuteDataReader([tmp_path]).read_day("SPY", PROBED)


@pytest.mark.parametrize("kind", [*NO_REGULAR_HOURS_BAR, *UNDECODABLE])
def test_a_first_root_zip_shadows_a_later_roots_copy_of_the_session(tmp_path: Path, kind: str) -> None:
    """The reader reads the first root's zip and never a later one's, so neither does availability (#2475 review).

    The later root's valid copy of the probed session must not count: the
    run would read the first root's unusable zip.
    """
    first, later = tmp_path / "first", tmp_path / "later"
    _seed(first, expected_sessions(*MINUTE_WINDOW))
    seed_store_day(later, "SPY", PROBED)
    if kind in UNDECODABLE:
        _undecodable(first, kind)
    else:
        _no_regular_hours_bar(first, kind)

    report = check_availability([first, later], "SPY", *MINUTE_WINDOW)

    assert report.sources[str(later)] == []
    assert not report.is_complete
    reader = LeanMinuteDataReader([first, later])
    if kind in UNDECODABLE:
        assert report.unreadable_days == [PROBED]
        with pytest.raises(UNDECODABLE[kind]):
            reader.read_day("SPY", PROBED)
    else:
        assert report.missing_days == [PROBED]
        assert reader.read_day("SPY", PROBED) == []


def test_a_rewritten_minute_zip_is_judged_afresh(tmp_path: Path) -> None:
    """The per-file verdict is cached; a zip replaced after one check must not keep its old verdict."""
    _seed(tmp_path, expected_sessions(*MINUTE_WINDOW))
    assert check_availability([tmp_path], "SPY", *MINUTE_WINDOW).is_complete

    seed_pre_market_day(tmp_path, "SPY", PROBED)

    assert check_availability([tmp_path], "SPY", *MINUTE_WINDOW).missing_days == [PROBED]


def test_an_extended_session_check_admits_pre_market_bars(tmp_path: Path) -> None:
    """The session filter is the reader's: pre-market bars are bars to an extended-session read."""
    _seed(tmp_path, expected_sessions(*MINUTE_WINDOW))
    seed_pre_market_day(tmp_path, "SPY", PROBED)

    assert check_availability([tmp_path], "SPY", *MINUTE_WINDOW, session="extended").is_complete


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
