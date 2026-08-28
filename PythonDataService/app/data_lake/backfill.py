"""Data-lake backfill orchestration — the per-day loop over ensure_data.

Wraps app.data_lake.ensure_data.ensure_data (the sole ensure seam) for a
job that must report live per-day progress. No new fetch/claim/write logic
lives here: run_backfill() only slices a DataRunSpec's date range into one
sub-range per canonical trading session, calls ensure_data() for each, and
folds the typed results into a running total the caller can stream.

Issue: #1836. Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import ArtifactFailure, DataAvailabilityResult, DataRunSpec, NonSessionRecord
from app.lean_sidecar.trading_calendar import expected_sessions

# ensure_data's Slice 1c claim path does not poll an in-flight lease (see
# _process_minute_trade_artifact's "polling not implemented in Slice 1c").
# Two concurrent backfills over overlapping ranges land on the same
# calendar day: the loser's claim conflicts with the winner's still-fetching
# row. Retrying at this layer — never touching the ensure seam or the
# catalog — turns that race into "wait a beat, then observe the artifact
# the other worker just completed" instead of a spurious failure.
_LEASE_RETRY_ATTEMPTS = 5
_LEASE_RETRY_BASE_DELAY_SECONDS = 0.05

EnsureFn = Callable[[DataRunSpec], Awaitable[DataAvailabilityResult]]


@dataclass(frozen=True)
class BackfillDayProgress:
    """One day's outcome, handed to the caller's on_day_progress callback."""

    day_index: int
    total_days: int
    trading_date: date
    fetched_count: int
    reused_count: int
    failures: tuple[ArtifactFailure, ...]


class BackfillResult(BaseModel):
    """Final job result — the whole range's outcome, folded day by day."""

    request_id: UUID
    market: str
    symbols: list[str]
    start_trading_date: date
    end_trading_date: date
    total_sessions: int
    days_completed: int
    days_with_failures: int
    fetched_artifact_count: int = 0
    reused_artifact_count: int = 0
    failures: list[ArtifactFailure] = []
    skipped_non_sessions: list[NonSessionRecord] = []
    overall_status: Literal["complete", "partial", "failed"]
    completed_at_ms: int
    duration_ms: int


async def _ensure_day_with_lease_retry(
    day_spec: DataRunSpec,
    ensure_fn: EnsureFn,
) -> DataAvailabilityResult:
    """Call ensure_fn for one day, retrying while blocked on another
    worker's in-flight claim (reason="lease_timeout").

    Every other typed failure (auth, entitlement, rate-limited, unknown
    symbol, ...) is returned immediately — those are not transient, and
    retrying them would just repeat the same provider call.
    """
    result = await ensure_fn(day_spec)
    for attempt in range(1, _LEASE_RETRY_ATTEMPTS + 1):
        if not any(f.reason == "lease_timeout" for f in result.failures):
            return result
        await asyncio.sleep(_LEASE_RETRY_BASE_DELAY_SECONDS * attempt)
        result = await ensure_fn(day_spec)
    return result


def _day_sub_spec(spec: DataRunSpec, trading_date: date) -> DataRunSpec:
    """Narrow spec to a single trading day, opting out of range/symbol-scoped
    artifacts that a per-day sub-call cannot correctly produce.

    Factor/map files are symbol-scoped (not day-scoped) — including them
    per day would just repeat the same corp-action fetch on every session.
    Daily-trade is a whole-range rollup — including it per day guarantees a
    data_contract_mismatch on every day after the first (see the
    include_daily_trade field docstring on DataRunSpec). model_copy() does
    not re-run validators, which is fine here: every field we set narrows
    an already-validated spec and can't violate a constraint the original
    didn't already satisfy.
    """
    return spec.model_copy(
        update={
            "start_trading_date": trading_date,
            "end_trading_date": trading_date,
            "include_factor_files": False,
            "include_map_files": False,
            "include_daily_trade": False,
        }
    )


async def run_backfill(
    spec: DataRunSpec,
    *,
    on_day_progress: Callable[[BackfillDayProgress], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    ensure_fn: EnsureFn = ensure_data,
) -> BackfillResult:
    """Ensure every canonical NYSE session in spec's range, one day at a time.

    Sessions come from the canonical NYSE calendar
    (app.lean_sidecar.trading_calendar.expected_sessions —
    temporal-rigor.md's authority), not ensure_data's own internal
    LEAN-image/hardcoded-holiday fallback (app.data_lake.sessions). The two
    can diverge on an edge date; when they do, ensure_data's per-day call
    simply finds nothing to require for that day (expand_required_artifacts
    returns an empty list) and the day completes trivially — it does not
    fail.

    This backfill is scoped to minute-bar (and same-day derived quote)
    coverage. Corp-action and whole-range daily-trade artifacts are left to
    a follow-up ensure_data() call over the full range once every day's
    minute bars are in place (see _day_sub_spec).

    cancel_check, when given, is called before each day and may raise to
    abort the loop (the job.runner contract: JobCancelled propagates to
    run_in_thread, which emits job.cancelled).
    """
    started_ms = int(time.time() * 1000)
    sessions = expected_sessions(spec.start_trading_date, spec.end_trading_date)

    fetched_total = 0
    reused_total = 0
    all_failures: list[ArtifactFailure] = []
    all_skipped: list[NonSessionRecord] = []
    days_completed = 0
    days_with_failures = 0

    for day_index, trading_date in enumerate(sessions, start=1):
        if cancel_check is not None:
            cancel_check()

        day_spec = _day_sub_spec(spec, trading_date)
        day_result = await _ensure_day_with_lease_retry(day_spec, ensure_fn)

        fetched_total += day_result.fetched_artifact_count
        reused_total += day_result.reused_artifact_count
        all_failures.extend(day_result.failures)
        all_skipped.extend(day_result.skipped_non_sessions)
        if day_result.failures:
            days_with_failures += 1
        else:
            days_completed += 1

        if on_day_progress is not None:
            on_day_progress(
                BackfillDayProgress(
                    day_index=day_index,
                    total_days=len(sessions),
                    trading_date=trading_date,
                    fetched_count=day_result.fetched_artifact_count,
                    reused_count=day_result.reused_artifact_count,
                    failures=tuple(day_result.failures),
                )
            )

    if all_failures and (fetched_total or reused_total):
        overall_status: Literal["complete", "partial", "failed"] = "partial"
    elif all_failures:
        overall_status = "failed"
    else:
        overall_status = "complete"

    completed_ms = int(time.time() * 1000)
    return BackfillResult(
        request_id=spec.request_id,
        market=spec.market,
        symbols=list(spec.symbols),
        start_trading_date=spec.start_trading_date,
        end_trading_date=spec.end_trading_date,
        total_sessions=len(sessions),
        days_completed=days_completed,
        days_with_failures=days_with_failures,
        fetched_artifact_count=fetched_total,
        reused_artifact_count=reused_total,
        failures=all_failures,
        skipped_non_sessions=all_skipped,
        overall_status=overall_status,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
