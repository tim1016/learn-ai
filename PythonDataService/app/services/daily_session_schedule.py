"""Daily live-session stop-time policy.

The lifecycle PRD is day-strategy-only: a bot may start only before its
effective stop for the NYSE session.  The configured stop is the run's
``live_config.force_flat_at`` when present, otherwise the live runtime default,
and it is always clamped to the canonical exchange close for that date.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.engine.live.config import LiveConfig
from app.lean_sidecar.trading_calendar import session_close_ms_utc

_NY_TZ = ZoneInfo("America/New_York")
_DEFAULT_STOP = LiveConfig().force_flat_at


@dataclass(frozen=True)
class StartBoundaryVerdict:
    allowed: bool
    reason_code: str | None
    message: str | None
    session_date: str | None
    effective_stop_ms: int | None


def effective_stop_ms_for_date(session_date: date, live_config: Mapping[str, object] | None) -> int:
    """Return ``min(configured_stop, session_close)`` for one NYSE session."""

    close_ms = session_close_ms_utc(session_date)
    configured = configured_stop_from_live_config(live_config)
    if configured is None:
        return close_ms
    configured_dt = datetime.combine(session_date, configured, tzinfo=_NY_TZ)
    configured_ms = int(configured_dt.astimezone(UTC).timestamp() * 1000)
    return min(configured_ms, close_ms)


def start_boundary_verdict(now_ms: int, live_config: Mapping[str, object] | None) -> StartBoundaryVerdict:
    now_ny = datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(_NY_TZ)
    session_date = now_ny.date()
    try:
        effective_stop_ms = effective_stop_ms_for_date(session_date, live_config)
    except LookupError:
        return StartBoundaryVerdict(
            allowed=False,
            reason_code="NO_TRADING_SESSION",
            message="No NYSE session is open for this bot today. Run roll call on the next session day.",
            session_date=session_date.isoformat(),
            effective_stop_ms=None,
        )
    if now_ms >= effective_stop_ms:
        return StartBoundaryVerdict(
            allowed=False,
            reason_code="SESSION_STOP_REACHED",
            message="Start refused because the current time is at or after this bot's effective stop.",
            session_date=session_date.isoformat(),
            effective_stop_ms=effective_stop_ms,
        )
    return StartBoundaryVerdict(
        allowed=True,
        reason_code=None,
        message=None,
        session_date=session_date.isoformat(),
        effective_stop_ms=effective_stop_ms,
    )


def configured_stop_from_live_config(live_config: Mapping[str, object] | None) -> time | None:
    if live_config is None:
        return _DEFAULT_STOP
    raw = live_config.get("force_flat_at")
    if raw is None:
        return _DEFAULT_STOP
    if raw == "":
        return _DEFAULT_STOP
    if isinstance(raw, time):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"none", "null"}:
            return None
        parts = lowered.split(":")
        if len(parts) not in {2, 3}:
            return _DEFAULT_STOP
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
            return time(hour, minute, second)
        except ValueError:
            return _DEFAULT_STOP
    return _DEFAULT_STOP


__all__ = [
    "StartBoundaryVerdict",
    "configured_stop_from_live_config",
    "effective_stop_ms_for_date",
    "start_boundary_verdict",
]
