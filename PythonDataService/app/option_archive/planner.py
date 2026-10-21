"""Calendar-derived archive deadlines; interactive composition keeps its floor."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from app.data_lake.option_lake import (
    OPTION_HISTORY_YEARS,
    OptionDataFailure,
    option_capture_opens_ms,
    option_history_floor,
)
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_open_ms_utc,
    session_window_for_date,
)
from app.schemas.option_archive import ArchivePhase
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms, shift_calendar_years

UNDERLYINGS = ("SPY", "QQQ", "IWM")


class SessionOutcome(StrEnum):
    """What one capture attempt means for the archive."""

    COMPLETE = "complete"
    #: A transient failure: the session is eligible again after the retry backoff.
    RETRY = "retry"
    #: A failure another attempt tonight cannot cure; explicit recovery may.
    SKIP_TONIGHT = "skip_tonight"
    #: Provider access must be corrected; the underlying waits for an explicit recovery.
    BLOCK_PROVIDER = "block_provider"
    #: The vendor no longer serves the session.
    LOST = "lost"
    #: The attempt yielded to regular hours, the evening observation or a shutdown; progress is kept.
    PAUSED = "paused"
    #: The staging budget is full; progress is kept.
    CAPACITY_PAUSED = "capacity_paused"


#: Outcomes that keep tonight's capture open after the pass ends.
RETRY_TONIGHT = frozenset({SessionOutcome.RETRY, SessionOutcome.PAUSED, SessionOutcome.CAPACITY_PAUSED})
#: Outcomes health reports as the underlying's failure.
FAILED = frozenset({SessionOutcome.RETRY, SessionOutcome.SKIP_TONIGHT, SessionOutcome.BLOCK_PROVIDER})
#: Outcomes that keep the session out of the rest of the night's passes.
SKIPPED_TONIGHT = frozenset({SessionOutcome.SKIP_TONIGHT, SessionOutcome.BLOCK_PROVIDER})


def classify(failure: OptionDataFailure | None, *, pausing: bool) -> SessionOutcome:
    """The outcome of an attempt that ended with ``failure``; ``pausing`` is whether the pause rule holds now."""
    if failure is None:
        return SessionOutcome.COMPLETE
    reason = failure.reason
    if reason == "staging_capacity":
        return SessionOutcome.CAPACITY_PAUSED
    if reason in ("paused", "lease_timeout"):
        return SessionOutcome.PAUSED if pausing else SessionOutcome.RETRY
    if reason in ("provider_rate_limited", "provider_api_error", "io_error"):
        return SessionOutcome.RETRY
    if reason == "outside_data_plan":
        return SessionOutcome.LOST
    if reason in ("provider_auth_error", "provider_entitlement_error"):
        return SessionOutcome.BLOCK_PROVIDER
    return SessionOutcome.SKIP_TONIGHT


def archive_history_boundary(today: date) -> date:
    anniversary = shift_calendar_years(today, -OPTION_HISTORY_YEARS)
    return min(expected_sessions(anniversary, anniversary + timedelta(days=14))[0], option_history_floor(today))


def archive_baseline_ms(today: date) -> int:
    boundary = archive_history_boundary(today)
    first = expected_sessions(boundary, boundary + timedelta(days=14))[0]
    return session_open_ms_utc(first)


def regular_hours_pause(now_ms: int, margin_ms: int = 60_000) -> bool:
    day = et_date_at_ms(now_ms)
    try:
        window = session_window_for_date(day)
    except LookupError:
        return False
    return window.open_ms_utc - margin_ms <= now_ms < window.close_ms_utc


def next_daily_run_ms(*, now_ms: int, minute_et: int, completed_day_ms: int | None,
                      retry_after_ms: int | None, margin_ms: int) -> int:
    """The first instant at or after ``now_ms`` when a once-a-day job is due.

    It starts at ``minute_et`` past ET midnight, once per day, never inside the
    regular-hours pause, and not before a pending retry backoff ends.
    """
    day = et_date_at_ms(now_ms)
    if completed_day_ms == et_midnight_ms(day):
        day += timedelta(days=1)
    due = max(now_ms, et_minute_of_day_ms(day, minute_et), retry_after_ms or 0)
    if regular_hours_pause(due, margin_ms):
        due = session_window_for_date(et_date_at_ms(due)).close_ms_utc
    return due


@dataclass(frozen=True)
class ArchiveWork:
    underlying: str
    session_ms: int
    deadline_ms: int


@dataclass(frozen=True)
class ArchivePlan:
    phase: ArchivePhase
    work: tuple[ArchiveWork, ...] = ()


def plan_archive(
    *, now_ms: int, baseline_ms: int, complete: frozenset[tuple[str, int]],
    lost: frozenset[tuple[str, int]], start_minute_et: int = 60, margin_ms: int = 60_000,
) -> ArchivePlan:
    today = et_date_at_ms(now_ms)
    if regular_hours_pause(now_ms, margin_ms):
        return ArchivePlan("rth_pause")
    if now_ms < et_minute_of_day_ms(today, start_minute_et):
        return ArchivePlan("waiting")
    boundary = archive_history_boundary(today)
    first = max(boundary, et_date_at_ms(baseline_ms))
    days = expected_sessions(first, today - timedelta(days=1))
    work: list[ArchiveWork] = []
    for day in days:
        window = session_window_for_date(day)
        if option_capture_opens_ms(day) > now_ms:
            continue
        deadline = et_midnight_ms(shift_calendar_years(day, OPTION_HISTORY_YEARS))
        for symbol in UNDERLYINGS:
            key = (symbol, window.open_ms_utc)
            if key not in complete and key not in lost:
                work.append(ArchiveWork(symbol, window.open_ms_utc, deadline))
    # Equal-date ordering rotates deterministically each session. A failed
    # underlying is attempted once per pass, then cannot block its peers.
    rotation = {day: position % len(UNDERLYINGS) for position, day in enumerate(days)}
    work.sort(key=lambda item: (
        item.deadline_ms, item.session_ms,
        (UNDERLYINGS.index(item.underlying) - rotation[et_date_at_ms(item.session_ms)]) % len(UNDERLYINGS),
    ))
    return ArchivePlan("capture" if work else "complete", tuple(work))
