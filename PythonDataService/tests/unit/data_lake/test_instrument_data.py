"""Which sessions count, which are unfinished, and which a Fill can close (#3503, T06).

``build_instrument_data`` reads one coverage answer against the canonical NYSE
calendar, the provider's history floor and the clock. These cases pin the
calendar semantics (a holiday is no session, an early close is one, the
spring-forward day moves the open's UTC instant) and the floor (history the
provider no longer serves stays readable when held and refuses explicitly when
not), with the window, the clock and the floor all given, so none of them
depends on today's date.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.data_lake.types import CoverageDay, CoverageResponse, trading_date_to_calendar_anchor_ms
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_windows_ms_utc
from app.schemas.instrument_data import DailyRollup
from app.services.instrument_data_service import build_instrument_data

_ET = ZoneInfo("America/New_York")
_NO_ROLLUP = DailyRollup(state="not_built")
# Far below every window here unless a test says otherwise.
_OLD_FLOOR = date(2000, 1, 3)


def _et_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET).timestamp() * 1000)


def _coverage(start: date, end: date, status_by_date: dict[date, CoverageDay] | None = None, default: str = "complete") -> CoverageResponse:
    """What ``/coverage`` answers: one day per scheduled session, ``default`` unless overridden."""
    held = status_by_date or {}
    return CoverageResponse(
        market="usa",
        symbol="SYN",
        data_type="trade",
        resolution="minute",
        provider="polygon",
        price_adjustment_mode="raw",
        days=[
            held.get(window.session_date)
            or CoverageDay(trading_date_ms=window.open_ms_utc, status=default, artifact_id=window.session_date.toordinal())
            for window in session_windows_ms_utc(start, end)
        ],
    )


def _build(start: date, end: date, now_ms: int, coverage: CoverageResponse | None = None, floor: date = _OLD_FLOOR):
    return build_instrument_data(
        coverage or _coverage(start, end, default="missing"),
        start=start,
        end=end,
        now_ms=now_ms,
        floor=floor,
        rollup=_NO_ROLLUP,
    )


def _day(session_date: date, status: str, **fields) -> CoverageDay:
    return CoverageDay(trading_date_ms=session_open_ms_utc(session_date), status=status, artifact_id=1, **fields)


def test_a_holiday_and_a_weekend_are_not_sessions_and_an_early_close_is_one() -> None:
    # Thanksgiving is Thursday 2025-11-27; the next day is a half day; then a weekend.
    start, end = date(2025, 11, 24), date(2025, 12, 1)
    report = _build(start, end, now_ms=_et_ms(date(2026, 1, 5), 12))

    scheduled = expected_sessions(start, end)
    assert report.summary.calendar_day_count == 8
    assert report.summary.session_count == len(scheduled) == 5
    assert date(2025, 11, 27) not in scheduled
    assert date(2025, 11, 28) in scheduled
    assert [session.trading_date_ms for session in report.sessions] == [session_open_ms_utc(d) for d in scheduled]
    # Every one of them is a gap a Fill asks for: nothing counts the holiday or the weekend as missing.
    assert report.summary.missing == 5
    assert report.summary.fetchable == 5


def test_the_spring_forward_day_moves_the_open_and_still_counts_once() -> None:
    # US clocks moved on Sunday 2026-03-08: Friday's open is 14:30 UTC, Monday's is 13:30 UTC.
    start, end = date(2026, 3, 6), date(2026, 3, 9)
    report = _build(start, end, now_ms=_et_ms(date(2026, 4, 1), 12))

    opens = [session.trading_date_ms for session in report.sessions]
    assert report.summary.session_count == 2
    assert opens == [session_open_ms_utc(date(2026, 3, 6)), session_open_ms_utc(date(2026, 3, 9))]
    assert (opens[1] - opens[0]) % 86_400_000 == 82_800_000, "the open moved an hour in UTC across the change"


@pytest.mark.parametrize(
    ("label", "now_ms", "expected_state"),
    [
        # An early close ends the regular tape at 13:00 ET, but the lake stores the whole day, so the
        # date stays open until it rolls: the file would be frozen while after-hours trades still print.
        ("on an early-close day, after its 13:00 close", _et_ms(date(2025, 11, 28), 14), "forming"),
        ("the evening of that day", _et_ms(date(2025, 11, 28), 21), "forming"),
        ("the next morning, the date having rolled", _et_ms(date(2025, 12, 1), 8), "missing"),
        ("the evening before it opens", _et_ms(date(2025, 11, 26), 20), "upcoming"),
    ],
)
def test_the_forming_session_follows_the_calendar_not_the_regular_close(label: str, now_ms: int, expected_state: str) -> None:
    day = date(2025, 11, 28)
    report = _build(day, day, now_ms=now_ms)

    assert [session.state for session in report.sessions] == [expected_state], label


def test_a_forming_session_is_never_missing_and_a_fill_never_asks_for_it() -> None:
    start, end = date(2026, 1, 5), date(2026, 1, 9)
    now_ms = _et_ms(date(2026, 1, 8), 11)  # Thursday, mid-morning
    held = {d: _day(d, "complete") for d in expected_sessions(start, date(2026, 1, 6))}
    report = _build(start, end, now_ms, _coverage(start, end, held, default="missing"))

    assert [session.state for session in report.sessions] == ["ready", "ready", "missing", "forming", "upcoming"]
    assert report.summary.forming == 1
    assert report.summary.upcoming == 1
    assert report.summary.missing == 1
    # The one finished gap is the whole Fill window; Thursday and Friday are not in it.
    wednesday = date(2026, 1, 7)
    assert report.fill_window is not None
    assert report.fill_window.start_trading_date_ms == trading_date_to_calendar_anchor_ms(wednesday)
    assert report.fill_window.end_trading_date_ms == trading_date_to_calendar_anchor_ms(wednesday)


def test_history_older_than_the_provider_serves_stays_readable_and_a_missing_old_day_refuses() -> None:
    start, end = date(2024, 5, 20), date(2024, 5, 24)
    held = {d: _day(d, "complete") for d in expected_sessions(start, date(2024, 5, 22))}
    # The provider's window now begins after the whole span.
    floor = date(2025, 1, 6)
    report = _build(start, end, _et_ms(date(2026, 2, 2), 12), _coverage(start, end, held, default="missing"), floor=floor)

    assert [session.state for session in report.sessions] == ["ready", "ready", "ready", "missing", "missing"]
    refused = [session for session in report.sessions if session.state == "missing"]
    assert [(session.fetchable, session.blocked_by) for session in refused] == [(False, "before_provider_history")] * 2
    assert report.summary.ready == 3
    assert report.summary.before_provider_history == 2
    assert report.summary.fetchable == 0
    assert report.fill_window is None, "nothing a Fill could fetch, so nothing is offered"
    assert report.summary.run_readable is False
    assert report.provider_history_start_ms == trading_date_to_calendar_anchor_ms(floor)


def test_a_session_exactly_on_the_floor_is_fetchable() -> None:
    day = date(2025, 1, 6)
    report = _build(day, day, _et_ms(date(2026, 2, 2), 12), floor=day)

    assert [(session.state, session.fetchable, session.blocked_by) for session in report.sessions] == [("missing", True, None)]


def test_a_failure_the_provider_answered_with_no_bars_is_not_a_gap_to_fill() -> None:
    start, end = date(2024, 5, 20), date(2024, 5, 22)
    held = {
        date(2024, 5, 20): _day(date(2024, 5, 20), "failed", failure_reason="provider_no_data"),
        date(2024, 5, 21): _day(date(2024, 5, 21), "failed", failure_reason="provider_api_error"),
        date(2024, 5, 22): _day(date(2024, 5, 22), "complete"),
    }
    report = _build(start, end, _et_ms(date(2026, 2, 2), 12), _coverage(start, end, held))

    no_bars, retriable, ready = report.sessions
    assert (no_bars.state, no_bars.reason, no_bars.fetchable, no_bars.blocked_by) == ("failed", "provider_no_data", False, "provider_no_data")
    assert (retriable.state, retriable.reason, retriable.fetchable) == ("failed", "provider_api_error", True)
    assert ready.state == "ready"
    assert report.summary.provider_no_data == 1
    assert report.summary.fetchable == 1


def test_a_complete_row_a_run_refuses_is_a_rebuild_with_the_evidence_it_lacks() -> None:
    start, end = date(2024, 5, 20), date(2024, 5, 21)
    held = {
        date(2024, 5, 20): _day(date(2024, 5, 20), "needs_rebuild", rebuild_reason="adjustment_record_rejected"),
        date(2024, 5, 21): _day(date(2024, 5, 21), "fetching"),
    }
    report = _build(start, end, _et_ms(date(2026, 2, 2), 12), _coverage(start, end, held))

    rebuild, building = report.sessions
    assert (rebuild.state, rebuild.reason, rebuild.fetchable) == ("needs_rebuild", "adjustment_record_rejected", True)
    assert (building.state, building.fetchable) == ("building", False)
    assert report.summary.ready == 0
    assert report.summary.run_readable is False


def test_a_window_with_no_finished_session_is_not_run_readable() -> None:
    day = date(2026, 1, 8)
    report = _build(day, day, _et_ms(day, 11), _coverage(day, day))

    assert [session.state for session in report.sessions] == ["forming"]
    assert report.summary.run_readable is False
