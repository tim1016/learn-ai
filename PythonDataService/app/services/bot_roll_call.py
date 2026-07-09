"""Roll-call offer and fleet-summary policy for daily bot lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.engine.live.bot_lifecycle_state import (
    BotDisplayStatus,
    BotLifecyclePhase,
    BotLifecycleStateRecord,
    BotRollCallOfferRecord,
    BotRollCallOfferRepo,
    stable_bot_roll_call_offers_path,
)
from app.schemas.live_runs import (
    BotAttendanceCell,
    BotEveningReport,
    BotEveningReportRow,
    BotRollCallOffer,
    BotRollCallSummary,
    LiveInstanceStatus,
)

_NY_TZ = ZoneInfo("America/New_York")


def bot_roll_call_offer_repo(root: Path, sid: str) -> BotRollCallOfferRepo:
    artifacts_root = root.parent
    path = stable_bot_roll_call_offers_path(artifacts_root, sid)
    return BotRollCallOfferRepo(path)


def active_roll_call_offer(
    root: Path, sid: str, *, now_ms: int
) -> BotRollCallOfferRecord | None:
    return bot_roll_call_offer_repo(root, sid).active_offer(now_ms=now_ms)


def status_is_roll_call_eligible(status_view: LiveInstanceStatus) -> bool:
    lifecycle = status_view.daily_lifecycle
    start = status_view.operator_surface.host_process.start_capability
    return (
        lifecycle.phase == BotLifecyclePhase.OFF_DUTY
        and lifecycle.on_roster
        and lifecycle.attention_badge != BotDisplayStatus.SICK_BAY
        and start.enabled
        and start.run_id is not None
        and start.request is not None
    )


def ensure_roll_call_offer(
    root: Path,
    *,
    sid: str,
    run_id: str,
    session_date: str,
    issued_at_ms: int,
    expires_at_ms: int,
    evidence_snapshot: dict[str, object],
) -> BotRollCallOfferRecord:
    repo = bot_roll_call_offer_repo(root, sid)
    active = repo.active_offer(now_ms=issued_at_ms, session_date=session_date)
    if active is not None and active.run_id == run_id and active.expires_at_ms == expires_at_ms:
        return active
    return repo.append(
        BotRollCallOfferRecord(
            offer_id=f"{session_date}-{uuid4().hex}",
            strategy_instance_id=sid,
            run_id=run_id,
            session_date=session_date,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            evidence_snapshot=evidence_snapshot,
        )
    )


def roll_call_offer_schema(offer: BotRollCallOfferRecord) -> BotRollCallOffer:
    return BotRollCallOffer(
        offer_id=offer.offer_id,
        strategy_instance_id=offer.strategy_instance_id,
        run_id=offer.run_id,
        session_date=offer.session_date,
        issued_at_ms=offer.issued_at_ms,
        expires_at_ms=offer.expires_at_ms,
    )


def attendance_for_instance(
    *,
    runs: list[dict],
    lifecycle_state: BotLifecycleStateRecord | None,
    read_sidecar: Callable[[Path], object | None],
) -> list[BotAttendanceCell]:
    cells: list[BotAttendanceCell] = []
    for run in sorted(runs, key=lambda item: item.get("created_at_ms") or 0)[-7:]:
        run_dir = Path(str(run.get("run_dir") or ""))
        run_id = str(run.get("run_id") or run_dir.name)
        sidecar = read_sidecar(run_dir)
        created_at_ms = _positive_int(run.get("created_at_ms"))
        ended_at_ms = _positive_int(getattr(sidecar, "ended_at_ms", None))
        started_at_ms = _positive_int(getattr(sidecar, "started_at_ms", None))
        timestamp_ms = ended_at_ms or started_at_ms or created_at_ms
        if timestamp_ms is None:
            continue
        session_date = (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
            .astimezone(_NY_TZ)
            .date()
            .isoformat()
        )
        exit_reason = getattr(sidecar, "exit_reason", None)
        clean = sidecar is not None and (
            getattr(sidecar, "exit_code", None) == 0
            or (exit_reason is not None and str(exit_reason) in {"normal", "force_flat_complete"})
        )
        cells.append(
            BotAttendanceCell(
                session_date=session_date,
                status="clean" if clean else "sick",
                label="Clean day" if clean else BotDisplayStatus.SICK_BAY.value,
                receipt_ref=f"{run_id}/run_status.json",
            )
        )
    if lifecycle_state is not None and lifecycle_state.phase == BotLifecyclePhase.RETIRED:
        today = today_ny_iso()
        if not cells or cells[-1].session_date != today or cells[-1].status != "retired":
            cells.append(
                BotAttendanceCell(
                    session_date=today,
                    status="retired",
                    label=BotDisplayStatus.RETIRED.value,
                    receipt_ref=None,
                )
            )
    if not cells and lifecycle_state is not None and not lifecycle_state.on_roster:
        cells.append(
            BotAttendanceCell(
                session_date=today_ny_iso(),
                status="rested",
                label="Rested",
                receipt_ref=None,
            )
        )
    return cells


def roll_call_summary_from_rows(rows: list, *, now_ms: int) -> BotRollCallSummary:
    counts = {status: 0 for status in BotDisplayStatus}
    session_date: str | None = None
    effective_stop_ms: int | None = None
    for row in rows:
        status_label = BotDisplayStatus(row.daily_lifecycle.display_status)
        counts[status_label] = counts.get(status_label, 0) + 1
        action = row.daily_lifecycle.primary_action
        if action is not None and action.id == "confirm_start":
            session_date = session_date or today_ny_iso()
            if action.expires_at_ms is not None:
                effective_stop_ms = (
                    action.expires_at_ms
                    if effective_stop_ms is None
                    else min(effective_stop_ms, action.expires_at_ms)
                )
    return BotRollCallSummary(
        ready=counts[BotDisplayStatus.READY],
        off_roster=counts[BotDisplayStatus.OFF_ROSTER],
        sick_bay=counts[BotDisplayStatus.SICK_BAY],
        on_duty=counts[BotDisplayStatus.ON_DUTY] + counts[BotDisplayStatus.CLOCKING_OUT],
        off_duty=counts[BotDisplayStatus.OFF_DUTY],
        retired=counts[BotDisplayStatus.RETIRED],
        generated_at_ms=now_ms,
        session_date=session_date,
        effective_stop_ms=effective_stop_ms,
    )


def evening_report_from_rows(rows: list, *, now_ms: int) -> BotEveningReport:
    report_date = (
        datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(_NY_TZ).date().isoformat()
    )
    report_rows: list[BotEveningReportRow] = []
    for row in rows:
        latest = row.attendance[-1] if row.attendance else None
        if latest is None:
            status_value: Literal["clean", "rested", "sick", "retired"] = (
                "retired" if row.daily_lifecycle.phase == BotLifecyclePhase.RETIRED else "rested"
            )
            label = BotDisplayStatus.RETIRED.value if status_value == "retired" else "Rested"
            receipt_ref = None
        else:
            status_value = latest.status
            label = latest.label
            receipt_ref = latest.receipt_ref
        report_rows.append(
            BotEveningReportRow(
                strategy_instance_id=row.strategy_instance_id,
                label=label,
                status=status_value,
                receipt_ref=receipt_ref,
            )
        )
    clean = sum(1 for row in report_rows if row.status == "clean")
    rested = sum(1 for row in report_rows if row.status == "rested")
    sick = sum(1 for row in report_rows if row.status == "sick")
    retired = sum(1 for row in report_rows if row.status == "retired")
    return BotEveningReport(
        session_date=report_date,
        generated_at_ms=now_ms,
        clean_exits=clean,
        rested=rested,
        sick=sick,
        retired=retired,
        summary=f"{clean} clean exits - {sick} sick bay - {rested} rested - {retired} retired",
        rows=report_rows,
    )


def today_ny_iso() -> str:
    return datetime.now(tz=UTC).astimezone(_NY_TZ).date().isoformat()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) and value >= 0 else None


__all__ = [
    "active_roll_call_offer",
    "attendance_for_instance",
    "bot_roll_call_offer_repo",
    "ensure_roll_call_offer",
    "evening_report_from_rows",
    "roll_call_offer_schema",
    "roll_call_summary_from_rows",
    "status_is_roll_call_eligible",
    "today_ny_iso",
]
