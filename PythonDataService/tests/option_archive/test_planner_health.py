from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.data_lake.option_lake import option_history_floor
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.health import project_health
from app.option_archive.planner import SessionOutcome, archive_history_boundary, plan_archive, regular_hours_pause
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveStateDoc, JobReceipt, SessionReceipt, SnapshotReceipt
from app.services.session_authority import et_minute_of_day_ms as wall_clock_ms
from app.utils.session_anchors import et_midnight_ms


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
    state = ArchiveStateDoc(version=1, baseline_ms=baseline,
                            sessions={"SPY": {recent: SessionReceipt(outcome=SessionOutcome.COMPLETE, ended_ms=recent)}})
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
    state = ArchiveStateDoc(version=1, jobs={job: JobReceipt(**receipt)})
    health = project_health(state, now_ms=wall_clock_ms(date(2026, 10, 21), 60), config=ArchiveSettings(ENABLED=True))
    item = health.backup if job == "backup" else health.test_restore
    assert item.severity == expected and item.reason


def test_backup_health_names_unverified_generations_and_keeps_the_recorded_failure_beside_a_missing_destination() -> None:
    now = wall_clock_ms(date(2026, 10, 21), 60)
    kept = ArchiveStateDoc(version=1, jobs={"backup": JobReceipt(last_success_ms=now - 1000, unverified_generations=["damaged-a"])})
    backup = project_health(kept, now_ms=now, config=ArchiveSettings(ENABLED=True)).backup
    assert backup.severity == "at_risk" and "damaged-a" in backup.reason
    failed = ArchiveStateDoc(version=1, jobs={"backup": JobReceipt(failure="Prune refused a retention pin")})
    backup = project_health(failed, now_ms=now, config=ArchiveSettings(ENABLED=True), destination_problem="The destination is gone").backup
    assert backup.reason == "The destination is gone" and backup.last_failure == "Prune refused a retention pin"


def test_next_capture_follows_the_receipt_and_the_calendar() -> None:
    day = date(2026, 10, 21)
    def next_due(now: int, **receipt: int) -> int | None:
        state = ArchiveStateDoc(version=1, jobs={"capture": JobReceipt(**receipt)})
        return project_health(state, now_ms=now, config=ArchiveSettings(ENABLED=True)).underlyings[0].next_due_ms
    assert next_due(wall_clock_ms(day, 30)) == wall_clock_ms(day, 60)
    retrying = wall_clock_ms(day, 120)
    assert next_due(retrying, retry_after_ms=retrying + 300_000) == retrying + 300_000
    assert next_due(retrying, completed_day_ms=et_midnight_ms(day)) == wall_clock_ms(day + timedelta(days=1), 60)
    window = session_window_for_date(day)
    assert next_due(window.open_ms_utc) == window.close_ms_utc


def test_snapshot_health_keeps_missed_evening_and_field_gaps() -> None:
    day = date(2026, 10, 19)
    session = session_open_ms_utc(day)
    state = ArchiveStateDoc(version=1, baseline_ms=session, snapshot_activation_ms=session, snapshots={"SPY": {session: SnapshotReceipt(
        complete=False, field_gaps={"iv:missing": 3}, started_ms=session, ended_ms=session)}})
    health = project_health(state, now_ms=wall_clock_ms(date(2026, 10, 21), 60), config=ArchiveSettings(ENABLED=True))
    spy = health.underlyings[0]
    assert spy.snapshot_missed_evenings == 1 and spy.snapshot_incomplete == 1 and spy.snapshot_field_gaps == 3


@pytest.mark.parametrize("gap", ["missed", "incomplete", "fields"])
def test_snapshot_gap_needs_attention_while_capture_is_catching_up(gap: str) -> None:
    sessions = [session_open_ms_utc(date(2026, 10, day)) for day in (19, 20)]
    now = wall_clock_ms(date(2026, 10, 21), 60)
    state = ArchiveStateDoc(version=1, baseline_ms=sessions[0], snapshot_activation_ms=sessions[0],
        jobs={name: JobReceipt(last_success_ms=now) for name in ("backup", "restore")},
        snapshots={symbol: {session: SnapshotReceipt(complete=True, started_ms=session, ended_ms=session)
                            for session in sessions} for symbol in ("SPY", "QQQ", "IWM")})
    observations = state.snapshots["SPY"]
    if gap == "missed":
        del observations[sessions[0]]
    elif gap == "incomplete":
        observations[sessions[0]].complete = False
    else:
        observations[sessions[-1]].field_gaps = {"iv:missing": 1}
    health = project_health(state, now_ms=now, config=ArchiveSettings(ENABLED=True))
    spy, qqq, iwm = health.underlyings
    assert qqq.severity == iwm.severity == "catching_up"
    assert health.backup.severity == health.test_restore.severity == "healthy"
    assert spy.severity == "at_risk" and spy.reason


def test_account_probe_pins_inclusive_weekday_anniversary() -> None:
    # #3418: read-only observed_at_ms=1792557343508; 270 bars served for
    # SPY241025C00580000 on session1729517400000, earlier Oct17/18 refused.
    assert archive_history_boundary(date(2026, 10, 21)) == date(2024, 10, 21)
    assert session_open_ms_utc(archive_history_boundary(date(2026, 10, 21))) == 1729517400000


def test_the_owner_opt_in_keeps_capture_planned_and_due_through_regular_hours() -> None:
    day = date(2026, 10, 21)
    window = session_window_for_date(day)
    baseline = session_open_ms_utc(date(2026, 10, 14))
    through = plan_archive(now_ms=window.open_ms_utc, baseline_ms=baseline, complete=frozenset(), lost=frozenset(),
                           pause_regular_hours=False)
    assert through.phase == "capture" and through.work
    state = ArchiveStateDoc(version=1, jobs={"capture": JobReceipt()})
    opted_in = project_health(state, now_ms=window.open_ms_utc,
                              config=ArchiveSettings(ENABLED=True, CAPTURE_DURING_REGULAR_HOURS=True)).underlyings[0]
    assert opted_in.next_due_ms == window.open_ms_utc
