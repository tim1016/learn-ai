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
from datetime import date, timedelta
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
    trading_date_to_calendar_anchor_ms,
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

# Reasons that mean "this provider credential/account is broken for every
# remaining day too" (auth, entitlement) — retrying them per day would just
# repeat the same doomed call up to _MAX_RANGE_YEARS*366 times.
# provider_rate_limited is bundled in here as a documented stop rather than
# a bounded backoff-and-retry: a real backoff/retry policy for rate limits
# is Task 7's territory (steal_or_retry_minute_bar already exists for the
# analogous lease-stuck case); stopping and reporting it typed is strictly
# better than silently burning the rest of the range against a limit that
# just tripped.
_GLOBALLY_FATAL_REASONS = frozenset(
    {
        "provider_auth_error",
        "provider_entitlement_error",
        "provider_rate_limited",
    }
)

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
    """Final job result — the whole range's outcome, folded day by day.

    days_completed + days_with_failures + days_unattempted always equals
    total_sessions. days_unattempted is non-zero only when a
    globally-fatal provider failure (auth, entitlement, rate-limited)
    stopped the run early — those sessions were never even attempted, a
    materially different fact from "attempted and failed" that a caller
    should not have to infer from the failures list.
    """

    request_id: UUID
    market: str
    symbols: list[str]
    start_trading_date: date
    end_trading_date: date
    total_sessions: int
    days_completed: int
    days_with_failures: int
    days_unattempted: int = 0
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
    Daily-trade is a whole-symbol rollup that now rebuilds automatically
    when its source set changes (#1870) rather than refusing, so including
    it per day would no longer fail — it would just rebuild the same daily
    zip N times in a row, once per session, for no benefit (see the
    include_daily_trade field docstring on DataRunSpec). All three are
    produced once instead, by the one follow-up call _rollup_spec builds
    after this loop completes. model_copy() does not re-run validators,
    which is fine here: every field we set narrows an already-validated
    spec and can't violate a constraint the original didn't already
    satisfy.
    """
    anchor_ms = trading_date_to_calendar_anchor_ms(trading_date)
    return spec.model_copy(
        update={
            "start_trading_date_ms": anchor_ms,
            "end_trading_date_ms": anchor_ms,
            "include_factor_files": False,
            "include_map_files": False,
            "include_daily_trade": False,
        }
    )


def _rollup_spec(spec: DataRunSpec, attempted_sessions: list[date]) -> DataRunSpec:
    """Full-range spec for the post-loop factor/map-file and daily-trade rollup.

    The day loop below narrows every sub-spec to exactly one day and opts
    out of factor files, map files, and daily-trade (see _day_sub_spec) —
    all three are symbol- or range-scoped, not day-scoped, and are
    deliberately deferred to this one follow-up call rather than being
    repeated on every iteration. Narrowed to the sessions actually attempted
    so a fatal-abort's un-attempted tail is not re-tried here — the
    daily-trade rollup itself derives from the full catalog for the symbol
    regardless of this spec's own window (see
    ensure_data._process_daily_trade_artifact), so narrowing this window
    costs the rollup nothing. Minute-trade/quote days in this window are all
    already 'complete' from the loop, so this call's Pass 1 is cache hits —
    zero provider calls beyond whatever factor/map files it fetches for the
    first time.
    """
    return spec.model_copy(
        update={
            "start_trading_date_ms": trading_date_to_calendar_anchor_ms(attempted_sessions[0]),
            "end_trading_date_ms": trading_date_to_calendar_anchor_ms(attempted_sessions[-1]),
            "include_factor_files": True,
            "include_map_files": True,
            "include_daily_trade": True,
        }
    )


def _missing_bar_failures(
    day_spec: DataRunSpec, trading_date: date, day_result: DataAvailabilityResult
) -> list[ArtifactFailure]:
    """Detect a canonical session that ensure_data's own calendar silently
    dropped, per requested (symbol, data_type).

    run_backfill iterates the canonical NYSE calendar
    (app.lean_sidecar.trading_calendar); ensure_data's per-day
    expand_required_artifacts consults its own, separate LEAN-image/
    hardcoded calendar (app.data_lake.sessions). If the two diverge on
    this date, ensure_data requires nothing for it: the day "completes"
    with only the unconditional Phase 0 metadata artifacts and zero
    requested bars — silently counted as backfilled without this check.
    Only flags a (symbol, data_type) that produced neither an artifact
    nor a failure; a real provider failure already explains the gap and
    is left alone rather than double-reported.
    """
    produced = {
        (a.symbol, a.data_type)
        for a in day_result.artifacts
        if a.artifact_kind == "time_series_bars" and a.resolution == "minute"
    }
    already_explained = {
        (f.symbol, f.data_type) for f in day_result.failures if f.artifact_kind == "time_series_bars"
    }
    missing: list[ArtifactFailure] = []
    for symbol in day_spec.symbols:
        for data_type in day_spec.data_types:
            key = (symbol, data_type)
            if key in produced or key in already_explained:
                continue
            missing.append(
                ArtifactFailure(
                    artifact_kind="time_series_bars",
                    symbol=symbol,
                    trading_date=trading_date,
                    data_type=data_type,
                    reason="session_not_produced",
                    detail=(
                        f"the canonical NYSE calendar treats {trading_date.isoformat()} as a session, "
                        "but ensure_data's own calendar did not require a bar artifact for it "
                        "(calendar divergence) — no matching artifact or failure was produced"
                    ),
                    attempt_count=0,
                )
            )
    return missing


def _run_aborted_failure(trading_date: date, fatal: ArtifactFailure, remaining_dates: list[date]) -> ArtifactFailure:
    """Typed marker recording that a globally-fatal failure stopped the
    run before the remaining sessions were ever attempted.

    Not tied to one symbol/day (trading_date=None) — it describes the
    whole unattempted remainder, not a single artifact.
    """
    detail_suffix = f": {fatal.detail}" if fatal.detail else ""
    return ArtifactFailure(
        artifact_kind="time_series_bars",
        symbol=None,
        trading_date=None,
        data_type=None,
        reason="run_aborted",
        detail=(
            f"stopped after a globally-fatal {fatal.reason} on {trading_date.isoformat()}{detail_suffix} — "
            f"{len(remaining_dates)} remaining session(s) "
            f"({remaining_dates[0].isoformat()}..{remaining_dates[-1].isoformat()}) were not attempted"
        ),
        attempt_count=0,
    )


def _skipped_non_sessions(spec: DataRunSpec, sessions: list[date]) -> list[NonSessionRecord]:
    """Every calendar date in the requested range that is not a canonical
    NYSE session — weekends and holidays the day loop below never visits.

    Deliberately NOT accumulated from each day's own
    DataAvailabilityResult.skipped_non_sessions: every per-day sub-spec's
    range IS a single canonical session by construction (it came from
    `sessions` itself), so ensure_data's own one-day calendar check
    trivially finds no gaps in it — that field is always empty on every
    sub-result. The requested range's actual non-sessions are computed
    directly from the calendar instead.
    """
    session_set = set(sessions)
    out: list[NonSessionRecord] = []
    current = spec.start_trading_date
    while current <= spec.end_trading_date:
        if current not in session_set:
            reason: Literal["weekend", "market_holiday"] = "weekend" if current.weekday() >= 5 else "market_holiday"
            out.append(NonSessionRecord(market=spec.market, trading_date=current, reason=reason))
        current += timedelta(days=1)
    return out


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
    finds nothing to require for it (expand_required_artifacts returns an
    empty list) and would otherwise complete trivially — _missing_bar_failures
    catches that and reports it as a typed session_not_produced failure per
    requested (symbol, data_type) instead of silently counting the day as
    backfilled.

    A globally-fatal provider failure (auth, entitlement, rate-limited —
    see _GLOBALLY_FATAL_REASONS) stops the loop after the day it first
    appears on: the remaining sessions are marked days_unattempted and one
    typed run_aborted failure records why, rather than repeating the same
    doomed provider call for every remaining day in the range.

    The day loop is scoped to minute-bar (and same-day derived quote)
    coverage. Corp-action and daily-trade artifacts are deliberately left
    out of every per-day sub-spec (see _day_sub_spec) and produced by one
    follow-up ensure_fn() call over the attempted range once the loop
    completes (#1869) — see _rollup_spec. That follow-up only runs when at
    least one day actually produced a bar (bar_success_total > 0); its own
    fetched/reused/failure counts fold into this function's totals the same
    way each day's do. overall_status is classified from requested
    minute-bar artifacts only — the Phase 0 metadata bootstrap that
    ensure_data unconditionally attempts every day counts toward its own
    fetched/reused totals but must not make an all-days-failed backfill read
    as "partial".

    cancel_check, when given, is called before each day and may raise to
    abort the loop (the job.runner contract: JobCancelled propagates to
    run_in_thread, which emits job.cancelled).
    """
    started_ms = int(time.time() * 1000)
    sessions = expected_sessions(spec.start_trading_date, spec.end_trading_date)
    skipped_non_sessions = _skipped_non_sessions(spec, sessions)

    fetched_total = 0
    reused_total = 0
    bar_success_total = 0
    all_failures: list[ArtifactFailure] = []
    days_completed = 0
    days_with_failures = 0
    days_unattempted = 0
    first_failed_day_index: int | None = None

    for day_index, trading_date in enumerate(sessions, start=1):
        if cancel_check is not None:
            cancel_check()

        day_spec = _day_sub_spec(spec, trading_date)
        day_result = await _ensure_day_with_lease_retry(day_spec, ensure_fn, status_fn=status_fn, on_wait=on_wait)

        missing = _missing_bar_failures(day_spec, trading_date, day_result)
        if missing:
            day_result = day_result.model_copy(
                update={
                    "failures": [*day_result.failures, *missing],
                    "overall_status": classify_overall_status(
                        has_failures=True, has_success=bool(day_result.artifacts)
                    ),
                }
            )

        fatal = next((f for f in day_result.failures if f.reason in _GLOBALLY_FATAL_REASONS), None)
        remaining_dates = sessions[day_index:] if fatal is not None else []
        if fatal is not None and remaining_dates:
            day_result = day_result.model_copy(
                update={"failures": [*day_result.failures, _run_aborted_failure(trading_date, fatal, remaining_dates)]}
            )

        fetched_total += day_result.fetched_artifact_count
        reused_total += day_result.reused_artifact_count
        bar_success_total += sum(1 for a in day_result.artifacts if a.artifact_kind == "time_series_bars")
        all_failures.extend(day_result.failures)
        if day_result.failures:
            days_with_failures += 1
            if first_failed_day_index is None:
                first_failed_day_index = day_index
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

        if fatal is not None:
            days_unattempted = len(remaining_dates)
            break

    # Follow-up rollup: factor/map files and the daily-trade artifact, all
    # deliberately deferred out of the per-day loop above (see _rollup_spec).
    # Skipped when nothing succeeded — an empty or wholly-failed range has no
    # minute-trade coverage for the rollup to derive anything from.
    #
    # The rollup's window is a single contiguous range (_rollup_spec takes
    # only a start/end), so it cannot skip an individual failed day sandwiched
    # between successes — it can only be truncated. Truncate it at the first
    # day (fatal or not) that had ANY failure: every day from there on is not
    # 'complete' for at least one requested data type, so re-including it
    # would have this call's Pass 1 retry a failure that already happened
    # once this run — repeating a doomed auth/rate-limit call in the fatal
    # case, or just duplicating the same failure in the report otherwise.
    # This can under-cover the factor_file's history window when a later day
    # past the first failure succeeded (its own window is spec-scoped, unlike
    # the daily-trade artifact's, whose source set is read from the full
    # catalog regardless of this spec's window — see ensure_data.
    # _process_daily_trade_artifact) — an acceptable trade-off since a later
    # ensure_data/backfill call over that wider range naturally rebuilds it
    # once the underlying provider issue is resolved.
    attempted_sessions = sessions[: len(sessions) - days_unattempted]
    rollup_sessions = (
        attempted_sessions[: first_failed_day_index - 1]
        if first_failed_day_index is not None
        else attempted_sessions
    )
    if rollup_sessions and bar_success_total > 0:
        # cancel_check is otherwise only evaluated at the top of each day-loop
        # iteration; without a check here a cancellation requested during (or
        # right after) the loop would be silently absorbed by this call —
        # provider I/O for corp-action artifacts included — and the job would
        # still emit job.completed instead of job.cancelled.
        if cancel_check is not None:
            cancel_check()
        rollup_spec = _rollup_spec(spec, rollup_sessions)
        rollup_result = await ensure_fn(rollup_spec)
        if cancel_check is not None:
            cancel_check()
        fetched_total += rollup_result.fetched_artifact_count
        reused_total += rollup_result.reused_artifact_count
        all_failures.extend(rollup_result.failures)

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
        days_unattempted=days_unattempted,
        fetched_artifact_count=fetched_total,
        reused_artifact_count=reused_total,
        failures=all_failures,
        skipped_non_sessions=skipped_non_sessions,
        overall_status=classify_overall_status(has_failures=bool(all_failures), has_success=bar_success_total > 0),
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
