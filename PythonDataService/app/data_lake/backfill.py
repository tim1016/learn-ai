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
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.data_lake.catalog_client import MinuteBarLeaseStatus, select_minute_bar_lease_status
from app.data_lake.ensure_data import ensure_data, minute_bar_identity
from app.data_lake.types import (
    ArtifactFailure,
    ArtifactIdentity,
    DataAvailabilityResult,
    DataRunSpec,
    NonSessionRecord,
    OverallStatus,
    classify_overall_status,
)
from app.lean_sidecar.trading_calendar import expected_sessions

logger = logging.getLogger(__name__)

# ensure_data's Slice 1c claim path does not poll an in-flight lease (see
# _process_minute_trade_artifact's "polling not implemented in Slice 1c"):
# the loser of a claim race gets a generic reason="lease_timeout" whether
# the winner is still fetching or has already permanently failed, and its
# own claim fallback (select_coverage_minute_bars — 'complete' rows only)
# can never distinguish the two, no matter how many times it's re-called.
# Waiting out the winner's own lease here — polling the row directly,
# never touching the ensure seam or the catalog's write path — turns the
# still-fetching case into "observe the artifact the other worker just
# completed" instead of a spurious failure, and the permanently-failed
# case into the winner's real recorded reason instead of a misleading
# lease_timeout. The wait is bounded by the winner's own lease TTL, not an
# arbitrary short retry budget: a legitimate fetch can run for the
# provider's full request timeout (tens of seconds), far longer than a
# fixed handful of sub-second retries would tolerate.
_LEASE_POLL_INTERVAL_SECONDS = 0.5

# How often a still-waiting poll is actually surfaced to on_wait (roughly
# every 5s of wall time) — the caller (the job's SSE emitter) relays every
# callback it receives verbatim, so the cadence decision has to live here,
# next to the interval it's derived from, not be re-guessed by the caller
# from a private constant it shouldn't know about.
_WAIT_NOTIFY_EVERY = max(1, round(5.0 / _LEASE_POLL_INTERVAL_SECONDS))

EnsureFn = Callable[[DataRunSpec], Awaitable[DataAvailabilityResult]]
LeaseStatusFn = Callable[[ArtifactIdentity], Awaitable[MinuteBarLeaseStatus | None]]


@dataclass(frozen=True)
class BackfillDayProgress:
    """One day's outcome, handed to the caller's on_day_progress callback."""

    day_index: int
    total_days: int
    trading_date: date
    fetched_count: int
    reused_count: int
    failures: tuple[ArtifactFailure, ...]


@dataclass(frozen=True)
class BackfillWaitProgress:
    """One (throttled) poll of a blocked identity's lease, handed to the
    caller's on_wait callback.

    Lets the caller (the job's SSE emitter) keep the stream informative
    during a slow coalesce — "waiting on another worker" — instead of
    going silent for the length of the wait. The caller should relay
    every callback it receives; _WAIT_NOTIFY_EVERY above already decides
    which polls are worth surfacing.
    """

    trading_date: date
    symbol: str | None
    data_type: Literal["trade", "quote"] | None
    attempt: int


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
    overall_status: OverallStatus
    completed_at_ms: int
    duration_ms: int


def _failure_identity_key(failure: ArtifactFailure) -> tuple[str | None, date | None, str | None]:
    return (failure.symbol, failure.trading_date, failure.data_type)


def _failure_from_row(original: ArtifactFailure, row: MinuteBarLeaseStatus) -> ArtifactFailure:
    """Build the replacement ArtifactFailure once the blocking row is
    found to be permanently 'failed'.

    ``LastError`` already stores one of ``ArtifactFailure.reason``'s own
    typed values — every ensure_data.py call site passes
    ``fail_artifact()`` the exact string it puts in its own
    ``ArtifactFailure.reason`` (see e.g. ``_process_minute_trade_artifact``).
    Uses ``model_validate`` rather than ``model_copy`` — ``model_copy``
    skips validation entirely in Pydantic v2, which would silently accept
    a value ``LastError`` was never supposed to hold; ``model_validate``
    still runs the Literal check, so an unrecognized reason read across
    the DB-row boundary falls back to ``"internal_error"`` instead of
    producing a model holding an invalid one.
    """
    detail = row.error_message or row.last_error or "winner permanently failed this artifact"
    try:
        return ArtifactFailure.model_validate({**original.model_dump(), "reason": row.last_error, "detail": detail})
    except ValidationError:
        return ArtifactFailure.model_validate(
            {
                **original.model_dump(),
                "reason": "internal_error",
                "detail": f"winner failed with unrecognized reason {row.last_error!r}: {detail}",
            }
        )


async def _wait_for_lease_resolution(
    day_spec: DataRunSpec,
    failure: ArtifactFailure,
    *,
    status_fn: LeaseStatusFn,
    on_wait: Callable[[BackfillWaitProgress], None] | None,
) -> ArtifactFailure | None:
    """Poll one blocked minute-bar identity's row until the winner's claim
    resolves or its own lease expires.

    Returns:
      - ``None``: the row is now 'complete' — the coalesce case. The
        caller re-runs ``ensure_fn`` once more to pick up the cache hit.
      - a replacement ``ArtifactFailure``: the row is permanently
        'failed' — carries the winner's actual recorded reason instead of
        the generic ``lease_timeout`` ``ensure_data``'s claim fallback
        would keep reporting forever (it only ever checks for
        'complete').
      - the original failure, unchanged: the row is still 'fetching' but
        the winner's own lease TTL has now elapsed — genuinely stuck (the
        winner likely crashed); give up rather than wait forever. Lease
        stealing/retry for this case is Task 7's territory
        (``steal_or_retry_minute_bar`` already exists for it).
    """
    identity = minute_bar_identity(
        day_spec,
        symbol=failure.symbol,
        trading_date=failure.trading_date,
        data_type=failure.data_type,
    )
    attempt = 0
    while True:
        attempt += 1
        row = await status_fn(identity)
        if row is None or row.status == "complete":
            return None
        if row.status == "failed":
            return _failure_from_row(failure, row)

        now_ms = int(time.time() * 1000)
        if row.lease_expires_at_ms is None or now_ms >= row.lease_expires_at_ms:
            return failure

        if on_wait is not None and (attempt == 1 or attempt % _WAIT_NOTIFY_EVERY == 0):
            on_wait(
                BackfillWaitProgress(
                    trading_date=day_spec.start_trading_date,
                    symbol=failure.symbol,
                    data_type=failure.data_type,
                    attempt=attempt,
                )
            )
        logger.info(
            "data_lake.backfill: waiting on an in-flight lease",
            extra={
                "trading_date": day_spec.start_trading_date.isoformat(),
                "symbol": failure.symbol,
                "data_type": failure.data_type,
                "attempt": attempt,
            },
        )
        await asyncio.sleep(_LEASE_POLL_INTERVAL_SECONDS)


async def _ensure_day_with_lease_retry(
    day_spec: DataRunSpec,
    ensure_fn: EnsureFn,
    *,
    status_fn: LeaseStatusFn,
    on_wait: Callable[[BackfillWaitProgress], None] | None,
) -> DataAvailabilityResult:
    """Call ensure_fn for one day; wait out any lease_timeout by polling
    the blocked row directly instead of blindly re-running the whole day
    on a fixed backoff.

    Every other typed failure (auth, entitlement, rate-limited, unknown
    symbol, ...) is returned immediately — those are not transient, and
    retrying them would just repeat the same provider call.
    """
    result = await ensure_fn(day_spec)
    lease_failures = [f for f in result.failures if f.reason == "lease_timeout"]
    if not lease_failures:
        return result

    resolutions: dict[tuple[str | None, date | None, str | None], ArtifactFailure] = {}
    any_completed = False
    for failure in lease_failures:
        outcome = await _wait_for_lease_resolution(day_spec, failure, status_fn=status_fn, on_wait=on_wait)
        if outcome is None:
            any_completed = True
        else:
            resolutions[_failure_identity_key(failure)] = outcome

    if any_completed:
        # At least one blocked identity is now complete — re-run the day
        # so ensure_fn's own claim fallback reports it as a cache hit and
        # every other field (artifacts, counts, hash) stays self-consistent.
        result = await ensure_fn(day_spec)

    # Substitute the diagnosed outcome for anything still reporting
    # lease_timeout — a permanent-failure reason if we learned one, or the
    # unchanged still-stuck failure if the wait budget ran out. A fresh
    # ensure_fn call can never surface a 'failed' row on its own (its
    # claim fallback only ever checks for 'complete'), so without this
    # substitution a permanently-failed winner would report lease_timeout
    # forever.
    patched_failures = [
        resolutions.get(_failure_identity_key(f), f) if f.reason == "lease_timeout" else f for f in result.failures
    ]
    return result.model_copy(
        update={
            "failures": patched_failures,
            "overall_status": classify_overall_status(
                has_failures=bool(patched_failures), has_success=bool(result.artifacts)
            ),
        }
    )


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
    on_wait: Callable[[BackfillWaitProgress], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    ensure_fn: EnsureFn = ensure_data,
    status_fn: LeaseStatusFn = select_minute_bar_lease_status,
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
        day_result = await _ensure_day_with_lease_retry(day_spec, ensure_fn, status_fn=status_fn, on_wait=on_wait)

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
        overall_status=classify_overall_status(
            has_failures=bool(all_failures), has_success=bool(fetched_total or reused_total)
        ),
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
