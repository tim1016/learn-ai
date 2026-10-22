"""A cheap projection of durable receipts, without provider calls or zip scans."""
from __future__ import annotations

from datetime import timedelta

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.planner import (
    FAILED,
    UNDERLYINGS,
    SessionOutcome,
    archive_history_boundary,
    next_daily_run_ms,
    regular_hours_pause,
    snapshot_window_ms,
)
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveStateDoc, JobReceipt, SessionReceipt
from app.schemas.option_archive import (
    ArchiveGap,
    ArchiveJobHealth,
    ArchivePhase,
    ArchiveSeverity,
    OptionArchiveHealth,
    UnderlyingArchiveHealth,
)
from app.utils.session_anchors import et_date_at_ms

_ATTENTION: tuple[ArchiveSeverity, ...] = ("at_risk", "failed", "unavailable")


def _job_health(receipt: JobReceipt, *, now_ms: int, stale_ms: int, name: str, problem: str | None = None) -> ArchiveJobHealth:
    completed = receipt.last_success_ms
    age = now_ms - completed if completed is not None else None
    failure = problem or receipt.failure
    if failure:
        severity, reason = "failed", failure
    elif completed is None:
        severity, reason = "unavailable", f"No checked {name} has been recorded."
    elif age is not None and age > stale_ms:
        severity, reason = "at_risk", f"The last checked {name} is stale; run it and inspect the failure receipt."
    elif receipt.unverified_generations:
        severity, reason = "at_risk", (f"Backup generations {', '.join(receipt.unverified_generations)} failed verification "
                                       "and are kept for diagnosis; follow the restore runbook.")
    else:
        severity, reason = "healthy", f"The latest {name} passed verification."
    return ArchiveJobHealth(severity=severity, reason=reason, last_success_ms=completed,
        generation_id=receipt.generation_id, last_failure=receipt.failure, size_bytes=receipt.size_bytes,
        free_bytes=receipt.free_bytes, manifest_sha256=receipt.manifest_sha256)


def _capture_phase(job: JobReceipt, *, now_ms: int, margin_ms: int, capacity_paused: bool) -> ArchivePhase:
    """Where capture stands now, read from the last pass's receipt."""
    if regular_hours_pause(now_ms, margin_ms):
        return "rth_pause"
    if job.failure is not None:
        return "failed"
    if job.phase == "capture" and job.ended_ms is not None:
        # The pass ended without recording where it stopped: it was cancelled.
        return "interrupted"
    if job.phase == "capacity_pause" and not capacity_paused:
        # A later capture completed the session that waited for staging space.
        return "waiting"
    return job.phase or "waiting"


def project_health(state: ArchiveStateDoc, *, now_ms: int, config: ArchiveSettings,
                   destination_problem: str | None = None) -> OptionArchiveHealth:
    today = et_date_at_ms(now_ms)
    boundary = archive_history_boundary(today)
    first = et_date_at_ms(state.baseline_ms) if state.baseline_ms is not None else boundary
    days = expected_sessions(first, today - timedelta(days=1))
    job = state.job("capture")
    # Staging space is one budget, so a capacity pause holds every underlying.
    capacity_reason = next((row.reason for rows in state.sessions.values() for row in rows.values()
                            if row.outcome is SessionOutcome.CAPACITY_PAUSED), None)
    phase = _capture_phase(job, now_ms=now_ms, margin_ms=config.REQUEST_OPEN_MARGIN_MS, capacity_paused=capacity_reason is not None)
    next_due = next_daily_run_ms(now_ms=now_ms, minute_et=config.CAPTURE_START_MINUTE_ET, completed_day_ms=job.completed_day_ms,
                                 retry_after_ms=job.retry_after_ms, margin_ms=config.REQUEST_OPEN_MARGIN_MS)
    result: list[UnderlyingArchiveHealth] = []
    for symbol in UNDERLYINGS:
        rows = state.sessions.get(symbol, {})
        complete: list[int] = []
        gaps: list[ArchiveGap] = []
        partial = 0
        for day in days:
            session_ms = session_open_ms_utc(day)
            item = rows.get(session_ms) or SessionReceipt()
            if item.complete:
                complete.append(session_ms)
                continue
            permanent = item.outcome is SessionOutcome.LOST or day < boundary
            partial += bool(item.answered)
            gaps.append(ArchiveGap(session_ms=session_ms, permanent=permanent,
                days_left=None if permanent else max(0, (day - boundary).days),
                reason=item.reason or "This session has not been fully archived.",
                recorded_ms=item.recorded_ms if item.recorded_ms is not None else now_ms))
        failure = next((row.reason for _, row in sorted(rows.items(), reverse=True) if row.outcome in FAILED), None)
        provider_block = state.provider_blocks.get(symbol)
        failure = (f"{provider_block.reason}: {provider_block.detail}" if provider_block else failure) or job.failure
        at_risk = any(gap.permanent or (gap.days_left is not None and gap.days_left <= config.AT_RISK_DAYS) for gap in gaps)
        severity = "failed" if failure else "at_risk" if at_risk or capacity_reason else "catching_up" if gaps else "healthy"
        reason = failure or capacity_reason or ("Archive gaps need attention before their vendor deadline." if at_risk else
            "The archive is catching up, oldest sessions first." if gaps else "Every required session has been archived.")
        if not config.ENABLED:
            severity, reason = "waiting", "Archive capture is disabled; enable it after reviewing the preservation runbook."
        first_gap = gaps[0].session_ms if gaps else None
        through = max((value for value in complete if first_gap is None or value < first_gap), default=None)
        observations = state.snapshots.get(symbol, {})
        missed = 0
        if state.snapshot_activation_ms is not None:
            for day in expected_sessions(et_date_at_ms(state.snapshot_activation_ms), today):
                window = session_window_for_date(day)
                _, cutoff = snapshot_window_ms(window.close_ms_utc, delay_ms=config.SNAPSHOT_DELAY_MS,
                                               window_ms=config.SNAPSHOT_WINDOW_MS)
                if now_ms >= cutoff and window.open_ms_utc not in observations:
                    missed += 1
        incomplete = sum(not row.complete for row in observations.values())
        last_observation = max(observations, default=None)
        field_gaps = sum(observations[last_observation].field_gaps.values()) if last_observation is not None else 0
        if config.ENABLED and (missed or incomplete or field_gaps) and severity in ("healthy", "catching_up"):
            severity, reason = "at_risk", "An evening snapshot is missing or incomplete; inspect its observation receipt."
        result.append(UnderlyingArchiveHealth(underlying=symbol, severity=severity, reason=reason,
            earliest_required_session_ms=session_open_ms_utc(days[0]) if days else None,
            complete_through_session_ms=through, last_archived_session_ms=max(complete, default=None),
            pending_sessions=sum(not gap.permanent for gap in gaps), partial_sessions=partial,
            lost_sessions=sum(gap.permanent for gap in gaps), gaps=gaps[:100], phase=phase,
            last_failure=failure, last_success_ms=max((row.ended_ms for row in rows.values()
                                                       if row.complete and row.ended_ms is not None), default=None),
            last_pass_started_ms=job.started_ms, last_pass_ended_ms=job.ended_ms, next_due_ms=next_due,
            snapshot_session_ms=last_observation,
            snapshot_missed_evenings=missed, snapshot_incomplete=incomplete, snapshot_field_gaps=field_gaps))
    backup_receipt = state.job("backup")
    backup = _job_health(backup_receipt, now_ms=now_ms, stale_ms=config.BACKUP_STALE_MS, name="backup", problem=destination_problem)
    cutoff = backup_receipt.captured_through_ms or 0
    unbacked = [row.ended_ms for rows in state.sessions.values() for row in rows.values()
                if row.complete and row.ended_ms is not None and row.ended_ms > cutoff]
    backup.oldest_unbacked_capture_ms = min(unbacked, default=None)
    restore = _job_health(state.job("restore"), now_ms=now_ms, stale_ms=config.RESTORE_STALE_MS, name="test restore")
    attention = any(item.severity in _ATTENTION for item in (*result, backup, restore))
    return OptionArchiveHealth(enabled=config.ENABLED, generated_ms=now_ms, attention=attention,
        underlyings=result, backup=backup, test_restore=restore)
