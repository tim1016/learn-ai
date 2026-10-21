"""Calendar-derived archive deadlines; interactive composition keeps its floor."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.data_lake.option_lake import OPTION_HISTORY_YEARS, option_capture_opens_ms, option_history_floor
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_open_ms_utc,
    session_window_for_date,
)
from app.utils.session_anchors import et_date_at_ms

UNDERLYINGS = ("SPY", "QQQ", "IWM")
_ET = ZoneInfo("America/New_York")
# The read-only account probe observed at 1792557343508 served 270 bars
# for anniversary session 1729517400000, and refused the earlier sessions.
# The account includes the two-year anniversary itself (#3418).
ARCHIVE_ANNIVERSARY_INCLUSIVE = True


def archive_history_boundary(today: date) -> date:
    try:
        anniversary = today.replace(year=today.year - OPTION_HISTORY_YEARS)
    except ValueError:
        anniversary = today.replace(year=today.year - OPTION_HISTORY_YEARS, day=28)
    return min(expected_sessions(anniversary, anniversary + timedelta(days=14))[0], option_history_floor(today))


def archive_baseline_ms(today: date) -> int:
    boundary = archive_history_boundary(today)
    first = expected_sessions(boundary, boundary + timedelta(days=14))[0]
    return session_open_ms_utc(first)


def wall_clock_ms(day: date, minute_et: int) -> int:
    return int(datetime(day.year, day.month, day.day, minute_et // 60, minute_et % 60, tzinfo=_ET).timestamp() * 1000)


def regular_hours_pause(now_ms: int, margin_ms: int = 60_000) -> bool:
    day = et_date_at_ms(now_ms)
    try:
        window = session_window_for_date(day)
    except LookupError:
        return False
    return window.open_ms_utc - margin_ms <= now_ms < window.close_ms_utc


@dataclass(frozen=True)
class ArchiveWork:
    underlying: str
    session_ms: int
    deadline_ms: int


@dataclass(frozen=True)
class ArchivePlan:
    phase: Literal["capture", "rth_pause", "waiting", "complete"]
    work: tuple[ArchiveWork, ...] = ()


def plan_archive(
    *, now_ms: int, baseline_ms: int, complete: frozenset[tuple[str, int]],
    lost: frozenset[tuple[str, int]], start_minute_et: int = 60, margin_ms: int = 60_000,
) -> ArchivePlan:
    today = et_date_at_ms(now_ms)
    if regular_hours_pause(now_ms, margin_ms):
        return ArchivePlan("rth_pause")
    if now_ms < wall_clock_ms(today, start_minute_et):
        return ArchivePlan("waiting")
    boundary = archive_history_boundary(today)
    first = max(boundary, et_date_at_ms(baseline_ms))
    days = expected_sessions(first, today - timedelta(days=1))
    work: list[ArchiveWork] = []
    for day in days:
        window = session_window_for_date(day)
        if option_capture_opens_ms(day) > now_ms:
            continue
        try:
            expiry_day = day.replace(year=day.year + OPTION_HISTORY_YEARS)
        except ValueError:
            expiry_day = day.replace(year=day.year + OPTION_HISTORY_YEARS, day=28)
        deadline = wall_clock_ms(expiry_day, 0)
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
