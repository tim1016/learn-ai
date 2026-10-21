from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data_lake.option_lake import option_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.health import project_health
from app.option_archive.planner import archive_history_boundary, plan_archive, regular_hours_pause, wall_clock_ms
from app.option_archive.settings import ArchiveSettings


@pytest.mark.parametrize("today", [date(2026, 10, 12), date(2026, 10, 21), date(2024, 2, 29), date(2026, 3, 9), date(2026, 11, 2)])
def test_archive_boundary_preserves_vendor_edge_without_moving_interactive_floor(today: date) -> None:
    boundary = archive_history_boundary(today)
    floor = option_history_floor(today)
    try:
        anniversary = today.replace(year=today.year - 2)
    except ValueError:
        anniversary = today.replace(year=today.year - 2, day=28)
    assert anniversary <= boundary <= floor
    assert expected_sessions(boundary, floor + timedelta(days=7))[0] >= anniversary


def test_planner_oldest_first_fairness_and_holidays() -> None:
    now = wall_clock_ms(date(2026, 10, 13), 60)
    baseline = session_open_ms_utc(date(2024, 10, 14))
    plan = plan_archive(now_ms=now, baseline_ms=baseline, complete=frozenset(), lost=frozenset())
    assert [work.session_ms for work in plan.work] == sorted(work.session_ms for work in plan.work)
    assert {work.underlying for work in plan.work[:3]} == {"SPY", "QQQ", "IWM"}
    assert plan.work[0].underlying != plan.work[3].underlying
    completed = frozenset((item.underlying, item.session_ms) for item in plan.work[:2])
    resumed = plan_archive(now_ms=now, baseline_ms=baseline, complete=completed, lost=frozenset())
    assert (resumed.work[0].underlying, resumed.work[0].session_ms) not in completed
    assert resumed.work[0] == plan.work[2]


def test_late_wake_catches_up_immediately_and_half_day_pause_follows_calendar() -> None:
    day = date(2026, 11, 27)
    window = session_window_for_date(day)
    assert regular_hours_pause(window.open_ms_utc - 10_000)
    assert regular_hours_pause(window.close_ms_utc - 1)
    assert not regular_hours_pause(window.close_ms_utc + 1)
    plan = plan_archive(now_ms=wall_clock_ms(day, 180), baseline_ms=session_open_ms_utc(date(2024, 11, 29)),
                        complete=frozenset(), lost=frozenset())
    assert plan.phase == "capture" and len(plan.work) > 1000
    paused = plan_archive(now_ms=window.open_ms_utc, baseline_ms=session_open_ms_utc(date(2024, 11, 29)),
                          complete=frozenset(), lost=frozenset())
    assert paused.phase == "rth_pause" and not paused.work


def test_health_recent_success_never_hides_older_hole_and_lost_gap_survives_rollover() -> None:
    baseline = session_open_ms_utc(date(2024, 10, 18))
    recent = session_open_ms_utc(date(2026, 10, 16))
    state = {"baseline_ms": baseline, "sessions": {"SPY": {str(recent): {"complete": True, "ended_ms": recent}}}}
    config = ArchiveSettings(ENABLED=True)
    health = project_health(state, now_ms=wall_clock_ms(date(2026, 10, 21), 60), config=config)
    spy = health.underlyings[0]
    assert spy.severity == "at_risk" and spy.last_archived_session_ms == recent
    assert spy.complete_through_session_ms is None and spy.lost_sessions > 0
    assert spy.gaps[0].session_ms == baseline and spy.gaps[0].permanent


@pytest.mark.parametrize("job,receipt,expected", [
    ("backup", {}, "unavailable"),
    ("backup", {"last_success_ms": 1000}, "at_risk"),
    ("backup", {"failure": "Destination is missing"}, "failed"),
    ("restore", {"last_success_ms": 1000}, "at_risk"),
    ("restore", {"failure": "Hash verification failed"}, "failed"),
])
def test_backup_restore_health_is_independent_and_actionable(job: str, receipt: dict, expected: str) -> None:
    health = project_health({"jobs": {job: receipt}}, now_ms=wall_clock_ms(date(2026, 10, 21), 60), config=ArchiveSettings(ENABLED=True))
    item = health.backup if job == "backup" else health.test_restore
    assert item.severity == expected and item.reason and health.attention


def test_snapshot_health_keeps_missed_evening_and_field_gaps() -> None:
    day = date(2026, 10, 19)
    session = session_open_ms_utc(day)
    state = {"baseline_ms": session, "snapshot_activation_ms": session,
             "snapshots": {"SPY": {str(session): {"complete": False, "field_gaps": {"iv:missing": 3}}}}}
    health = project_health(state, now_ms=wall_clock_ms(date(2026, 10, 21), 60), config=ArchiveSettings(ENABLED=True))
    spy = health.underlyings[0]
    assert spy.snapshot_missed_evenings == 1 and spy.snapshot_incomplete == 1 and spy.snapshot_field_gaps == 3


def test_account_probe_pins_inclusive_weekday_anniversary() -> None:
    # #3418: read-only observed_at_ms=1792557343508; 270 bars served for
    # SPY241025C00580000 on session1729517400000, earlier Oct17/18 refused.
    assert archive_history_boundary(date(2026, 10, 21)) == date(2024, 10, 21)
    assert session_open_ms_utc(archive_history_boundary(date(2026, 10, 21))) == 1729517400000
