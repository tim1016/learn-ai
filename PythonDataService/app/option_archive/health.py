"""A cheap projection of durable receipts, without provider calls or zip scans."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.planner import UNDERLYINGS, archive_history_boundary, regular_hours_pause, wall_clock_ms
from app.option_archive.settings import ArchiveSettings
from app.schemas.option_archive import ArchiveGap, ArchiveJobHealth, OptionArchiveHealth, UnderlyingArchiveHealth
from app.utils.session_anchors import et_date_at_ms


def _job_health(receipt: dict[str, Any], *, now_ms: int, stale_ms: int, name: str) -> ArchiveJobHealth:
    completed = receipt.get("last_success_ms")
    age = now_ms - completed if completed is not None else None
    failure = receipt.get("failure")
    if failure:
        severity, reason = "failed", failure
    elif completed is None:
        severity, reason = "unavailable", f"No checked {name} has been recorded."
    elif age is not None and age > stale_ms:
        severity, reason = "at_risk", f"The last checked {name} is stale; run it and inspect the failure receipt."
    else:
        severity, reason = "healthy", f"The latest {name} passed verification."
    return ArchiveJobHealth(severity=severity, reason=reason, last_success_ms=completed, age_ms=age,
        generation_id=receipt.get("generation_id"), last_failure=failure, size_bytes=receipt.get("size_bytes"),
        free_bytes=receipt.get("free_bytes"), manifest_sha256=receipt.get("manifest_sha256"),
        oldest_unbacked_capture_ms=receipt.get("oldest_unbacked_capture_ms"))


def project_health(state: dict[str, Any], *, now_ms: int, config: ArchiveSettings) -> OptionArchiveHealth:
    today = et_date_at_ms(now_ms)
    boundary = archive_history_boundary(today)
    baseline = state.get("baseline_ms")
    first = et_date_at_ms(baseline) if baseline is not None else boundary
    days = expected_sessions(first, today - timedelta(days=1))
    sessions = state.get("sessions", {})
    job = state.get("jobs", {}).get("capture", {})
    phase = "rth_pause" if regular_hours_pause(now_ms, config.REQUEST_OPEN_MARGIN_MS) else job.get("phase", "waiting")
    result: list[UnderlyingArchiveHealth] = []
    for symbol in UNDERLYINGS:
        rows = sessions.get(symbol, {})
        complete: list[int] = []
        gaps: list[ArchiveGap] = []
        partial = 0
        for day in days:
            session_ms = session_open_ms_utc(day)
            item = rows.get(str(session_ms), {})
            if item.get("complete"):
                complete.append(session_ms)
                continue
            permanent = item.get("lost", False) or day < boundary
            partial += bool(item.get("answered", 0))
            gaps.append(ArchiveGap(session_ms=session_ms, permanent=permanent,
                days_left=None if permanent else max(0, (day - boundary).days),
                reason=item.get("reason", "This session has not been fully archived."), recorded_ms=item.get("recorded_ms", now_ms)))
        failure = next((row.get("reason") for row in reversed(list(rows.values())) if row.get("failed")), None)
        at_risk = any(gap.permanent or (gap.days_left is not None and gap.days_left <= config.AT_RISK_DAYS) for gap in gaps)
        severity = "failed" if failure else "at_risk" if at_risk else "catching_up" if gaps else "healthy"
        reason = failure or ("Archive gaps need attention before their vendor deadline." if at_risk else
            "The archive is catching up, oldest sessions first." if gaps else "Every required session has been archived.")
        if not config.ENABLED:
            severity, reason = "waiting", "Archive capture is disabled; enable it after reviewing the preservation runbook."
        first_gap = gaps[0].session_ms if gaps else None
        through = max((value for value in complete if first_gap is None or value < first_gap), default=None)
        observations = state.get("snapshots", {}).get(symbol, {})
        snapshot_base = state.get("snapshot_activation_ms")
        missed = 0
        if snapshot_base is not None:
            for day in expected_sessions(et_date_at_ms(snapshot_base), today):
                window = session_window_for_date(day)
                if now_ms > window.close_ms_utc + config.SNAPSHOT_DELAY_MS + config.SNAPSHOT_WINDOW_MS and str(window.open_ms_utc) not in observations:
                    missed += 1
        incomplete = sum(not row.get("complete", False) for row in observations.values())
        last_observation = max(observations, key=int, default=None)
        field_gaps = sum(observations[last_observation].get("field_gaps", {}).values()) if last_observation else 0
        if config.ENABLED and (missed or incomplete or field_gaps) and severity == "healthy":
            severity, reason = "at_risk", "An evening snapshot is missing or incomplete; inspect its observation receipt."
        result.append(UnderlyingArchiveHealth(underlying=symbol, severity=severity, reason=reason,
            earliest_required_session_ms=session_open_ms_utc(days[0]) if days else None,
            complete_through_session_ms=through, last_archived_session_ms=max(complete, default=None),
            pending_sessions=sum(not gap.permanent for gap in gaps), partial_sessions=partial,
            lost_sessions=sum(gap.permanent for gap in gaps), gaps=gaps[:100], phase=phase,
            last_failure=failure, last_success_ms=max((row.get("ended_ms", 0) for row in rows.values() if row.get("complete")), default=None),
            last_pass_started_ms=job.get("started_ms"), last_pass_ended_ms=job.get("ended_ms"),
            next_due_ms=wall_clock_ms(today + timedelta(days=1), config.CAPTURE_START_MINUTE_ET),
            snapshot_session_ms=int(last_observation) if last_observation else None,
            snapshot_missed_evenings=missed, snapshot_incomplete=incomplete, snapshot_field_gaps=field_gaps))
    jobs = state.get("jobs", {})
    backup = _job_health(jobs.get("backup", {}), now_ms=now_ms, stale_ms=config.BACKUP_STALE_MS, name="backup")
    cutoff = jobs.get("backup", {}).get("captured_through_ms", 0)
    unbacked = [row["ended_ms"] for entries in sessions.values() for row in entries.values()
                if row.get("complete") and row.get("ended_ms", 0) > cutoff]
    backup.oldest_unbacked_capture_ms = min(unbacked, default=None)
    restore = _job_health(jobs.get("restore", {}), now_ms=now_ms, stale_ms=config.RESTORE_STALE_MS, name="test restore")
    attention = any(row.severity in ("at_risk", "failed", "unavailable") for row in result) or any(
        row.severity in ("at_risk", "failed", "unavailable") for row in (backup, restore))
    return OptionArchiveHealth(enabled=config.ENABLED, generated_ms=now_ms, attention=attention,
        underlyings=result, backup=backup, test_restore=restore)
