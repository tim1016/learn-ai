"""Daily live-session stop-time policy.

RTH-only bots may start only before their effective stop for the NYSE session.
The configured stop is the run's ``live_config.force_flat_at`` when present,
otherwise the live runtime default, and it is clamped to the canonical
exchange close for that date. Extended-session bots (PRE/POST/OVERNIGHT in
``allowed_sessions``) default to no daily stop; the session gate controls
submissions while the process can continue across day/night boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.engine.live.config import LiveConfig, normalize_allowed_sessions
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


@dataclass(frozen=True)
class CohortWindowVerdict:
    """Whether one fixed-duration cohort can finish before a daily stop."""

    allowed: bool
    reason_code: str | None
    message: str | None
    session_date: str | None
    effective_stop_ms: int | None
    required_window_end_ms: int


def effective_stop_ms_for_date(session_date: date, live_config: Mapping[str, object] | None) -> int | None:
    """Return the effective stop for one NYSE session, or ``None`` for continuous lifecycle."""

    close_ms = session_close_ms_utc(session_date)
    configured = configured_stop_from_live_config(live_config)
    if configured is None:
        return None
    configured_dt = datetime.combine(session_date, configured, tzinfo=_NY_TZ)
    configured_ms = int(configured_dt.astimezone(UTC).timestamp() * 1000)
    if live_config is not None and _declares_extended_session(live_config):
        return configured_ms
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
    if effective_stop_ms is not None and now_ms >= effective_stop_ms:
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


def cohort_window_verdict(
    now_ms: int,
    *,
    live_configs: Sequence[Mapping[str, object]],
    required_window_ms: int,
) -> CohortWindowVerdict:
    """Require a cohort's entire proof window to fit before every member's stop.

    Members that have no daily stop (for example an explicitly extended-session
    lifecycle) do not constrain the window. A malformed or unavailable policy
    must be rejected by the caller before it reaches this function.
    """

    if required_window_ms <= 0:
        raise ValueError("required_window_ms must be positive")
    now_ny = datetime.fromtimestamp(now_ms / 1_000, tz=UTC).astimezone(_NY_TZ)
    session_date = now_ny.date()
    try:
        stops = tuple(
            stop
            for live_config in live_configs
            if (stop := effective_stop_ms_for_date(session_date, live_config)) is not None
        )
    except LookupError:
        return CohortWindowVerdict(
            allowed=False,
            reason_code="NO_TRADING_SESSION",
            message="No NYSE session is open for this cohort today. Run it on the next session day.",
            session_date=session_date.isoformat(),
            effective_stop_ms=None,
            required_window_end_ms=now_ms + required_window_ms,
        )
    required_window_end_ms = now_ms + required_window_ms
    effective_stop_ms = min(stops, default=None)
    if effective_stop_ms is not None and required_window_end_ms > effective_stop_ms:
        return CohortWindowVerdict(
            allowed=False,
            reason_code="COHORT_WINDOW_EXCEEDS_SESSION_STOP",
            message=(
                "Start refused because the T+0/T+15m/T+30m schedule and its 15-minute "
                "validation window will not finish before the earliest effective stop."
            ),
            session_date=session_date.isoformat(),
            effective_stop_ms=effective_stop_ms,
            required_window_end_ms=required_window_end_ms,
        )
    return CohortWindowVerdict(
        allowed=True,
        reason_code=None,
        message=None,
        session_date=session_date.isoformat(),
        effective_stop_ms=effective_stop_ms,
        required_window_end_ms=required_window_end_ms,
    )


def configured_stop_from_live_config(live_config: Mapping[str, object] | None) -> time | None:
    if live_config is None:
        return _DEFAULT_STOP
    has_force_flat = "force_flat_at" in live_config
    if not has_force_flat and _declares_extended_session(live_config):
        return None
    raw = live_config.get("force_flat_at")
    if raw is None:
        return None if has_force_flat else _DEFAULT_STOP
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


def _declares_extended_session(live_config: Mapping[str, object]) -> bool:
    raw = live_config.get("allowed_sessions")
    if raw is None:
        return False
    try:
        allowed = normalize_allowed_sessions(raw)
    except (TypeError, ValueError):
        return False
    return any(session != "RTH" for session in allowed)


__all__ = [
    "CohortWindowVerdict",
    "StartBoundaryVerdict",
    "cohort_window_verdict",
    "configured_stop_from_live_config",
    "effective_stop_ms_for_date",
    "start_boundary_verdict",
]
