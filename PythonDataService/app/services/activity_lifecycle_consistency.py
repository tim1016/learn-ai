"""Activity/lifecycle consistency policy for the Activity tab."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.schemas.live_runs import ActivityReconciliationWarning

NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ActiveRunWindow:
    run: Mapping[str, Any]
    run_dir: Path
    run_id: str
    started_at_ms: int
    ended_at_ms: int | None
    sidecar_started: bool


def ny_session_bounds_ms(day: date) -> tuple[int, int]:
    """Return America/New_York calendar-day bounds as UTC ms."""

    start = datetime.combine(day, datetime.min.time(), tzinfo=NY_TZ)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=NY_TZ)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def runs_active_in_window(
    runs: Sequence[Mapping[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    live_run_id: str | None,
    force_include_live_run: bool,
    read_sidecar: Callable[[Path], object | None],
) -> list[ActiveRunWindow]:
    """Subset runs whose sidecar or fallback creation window overlaps a session."""

    active: list[ActiveRunWindow] = []
    for run in runs:
        run_dir = Path(str(run["run_dir"]))
        sidecar = read_sidecar(run_dir)
        started = getattr(sidecar, "started_at_ms", None) if sidecar is not None else None
        ended = getattr(sidecar, "ended_at_ms", None) if sidecar is not None else None
        if started is None:
            started = int(run.get("created_at_ms") or 0)
        effective_end = ended if ended is not None else end_ms
        run_id = str(run.get("run_id") or run_dir.name)
        if started < end_ms and effective_end >= start_ms:
            active.append(
                ActiveRunWindow(
                    run=run,
                    run_dir=run_dir,
                    run_id=run_id,
                    started_at_ms=started,
                    ended_at_ms=ended,
                    sidecar_started=sidecar is not None,
                )
            )
        elif force_include_live_run and live_run_id is not None and run_id == live_run_id:
            active.append(
                ActiveRunWindow(
                    run=run,
                    run_dir=run_dir,
                    run_id=run_id,
                    started_at_ms=started,
                    ended_at_ms=None,
                    sidecar_started=False,
                )
            )
    return active


def activity_order_refs_for_session(
    activity_rows: Sequence[object],
    *,
    start_ms: int,
    end_ms: int,
    row_time_ms: Callable[[object], int],
) -> set[str]:
    return {
        row.order_ref
        for row in activity_rows
        if getattr(row, "order_ref", None) and start_ms <= row_time_ms(row) < end_ms
    }


def activity_lifecycle_consistency_warnings(
    *,
    lifecycle_refs: set[str],
    activity_refs: set[str],
) -> list[ActivityReconciliationWarning]:
    warnings: list[ActivityReconciliationWarning] = []
    missing_activity = sorted(lifecycle_refs - activity_refs)
    if missing_activity:
        warnings.append(
            ActivityReconciliationWarning(
                code="lifecycle_order_missing_activity",
                message=(
                    "Lifecycle submit evidence exists for order refs that are absent from the Activity projection; "
                    "treat broker capture as incomplete until reconciled."
                ),
                row_ids=missing_activity,
            )
        )
    missing_lifecycle = sorted(activity_refs - lifecycle_refs)
    if missing_lifecycle:
        warnings.append(
            ActivityReconciliationWarning(
                code="activity_order_missing_lifecycle",
                message=(
                    "Activity has broker/order evidence for order refs that are absent from the lifecycle timeline; "
                    "treat lifecycle capture as incomplete until reconciled."
                ),
                row_ids=missing_lifecycle,
            )
        )
    return warnings


__all__ = [
    "NY_TZ",
    "ActiveRunWindow",
    "activity_lifecycle_consistency_warnings",
    "activity_order_refs_for_session",
    "ny_session_bounds_ms",
    "runs_active_in_window",
]
