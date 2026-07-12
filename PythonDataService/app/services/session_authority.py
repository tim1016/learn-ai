from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import next_trading_day, session_window_for_date
from app.schemas.broker_capability import SessionDataCapability, SessionKind

TradingSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]
SessionAuthoritySource = Literal["ibkr_capability", "nyse_calendar"]

_NY = ZoneInfo("America/New_York")
_PRE_OPEN = time(4, 0)
_POST_CLOSE = time(20, 0)
_SESSION_PRIORITY: tuple[SessionKind, ...] = ("RTH", "PRE", "POST", "OVERNIGHT")


@dataclass(frozen=True)
class SessionAuthorityState:
    phase: TradingSessionPhase
    permits_strategy_activity: bool
    next_transition_ms: int | None
    timezone: str
    as_of_ms: int
    source: SessionAuthoritySource


def session_state_at_ms(
    *,
    now_ms: int,
    capability: SessionDataCapability | None = None,
    strategy_session_policy: Literal["rth_only"] | None = None,
    allowed_sessions: tuple[SessionKind, ...] | None = None,
) -> SessionAuthorityState:
    """Return the authoritative live-session state for one instrument instant."""
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative int64 ms UTC")
    if capability is not None:
        state = _session_from_capability(
            now_ms=now_ms,
            capability=capability,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )
        if state is not None:
            return state
    return _session_from_nyse_calendar(
        now_ms=now_ms,
        strategy_session_policy=strategy_session_policy,
        allowed_sessions=allowed_sessions,
    )


def _session_from_capability(
    *,
    now_ms: int,
    capability: SessionDataCapability,
    strategy_session_policy: Literal["rth_only"] | None,
    allowed_sessions: tuple[SessionKind, ...] | None,
) -> SessionAuthorityState | None:
    windows = {
        kind: window
        for kind in _SESSION_PRIORITY
        if (window := _window_tuple(capability, kind)) is not None
    }
    if not windows:
        return None
    min_window = min(open_ms for open_ms, _close_ms in windows.values())
    max_window = max(close_ms for _open_ms, close_ms in windows.values())
    if now_ms < min_window or now_ms >= max_window:
        next_transition = _next_capability_transition(now_ms, windows)
        if next_transition is None:
            return None
        return _state(
            phase="CLOSED",
            now_ms=now_ms,
            next_transition_ms=next_transition,
            timezone=capability.time_zone_id,
            source="ibkr_capability",
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )

    phase: TradingSessionPhase = "CLOSED"
    for kind in _SESSION_PRIORITY:
        window = windows.get(kind)
        if window is None:
            continue
        open_ms, close_ms = window
        if open_ms <= now_ms < close_ms:
            phase = "OVERNIGHT" if kind == "OVERNIGHT" else kind
            break
    return _state(
        phase=phase,
        now_ms=now_ms,
        next_transition_ms=_next_capability_transition(now_ms, windows),
        timezone=capability.time_zone_id,
        source="ibkr_capability",
        strategy_session_policy=strategy_session_policy,
        allowed_sessions=allowed_sessions,
    )


def _session_from_nyse_calendar(
    *,
    now_ms: int,
    strategy_session_policy: Literal["rth_only"] | None,
    allowed_sessions: tuple[SessionKind, ...] | None,
) -> SessionAuthorityState:
    now_ny = _ny_dt(now_ms)
    try:
        session_window = session_window_for_date(now_ny.date())
    except LookupError:
        return _state(
            phase="CLOSED",
            now_ms=now_ms,
            next_transition_ms=_next_session_pre_open(now_ny),
            timezone="America/New_York",
            source="nyse_calendar",
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )

    session_open_ny = _ny_dt(session_window.open_ms_utc)
    session_close_ny = _ny_dt(session_window.close_ms_utc)
    pre_open_ny = _at(now_ny, _PRE_OPEN)
    post_close_ny = _at(now_ny, _POST_CLOSE)

    if now_ny < pre_open_ny:
        phase: TradingSessionPhase = "CLOSED"
        next_transition_ms = _ms_utc(pre_open_ny)
    elif now_ny < session_open_ny:
        phase = "PRE"
        next_transition_ms = session_window.open_ms_utc
    elif now_ny < session_close_ny:
        phase = "RTH"
        next_transition_ms = session_window.close_ms_utc
    elif now_ny < post_close_ny:
        phase = "POST"
        next_transition_ms = _ms_utc(post_close_ny)
    else:
        phase = "CLOSED"
        next_transition_ms = _next_session_pre_open(now_ny)

    return _state(
        phase=phase,
        now_ms=now_ms,
        next_transition_ms=next_transition_ms,
        timezone="America/New_York",
        source="nyse_calendar",
        strategy_session_policy=strategy_session_policy,
        allowed_sessions=allowed_sessions,
    )


def _state(
    *,
    phase: TradingSessionPhase,
    now_ms: int,
    next_transition_ms: int | None,
    timezone: str,
    source: SessionAuthoritySource,
    strategy_session_policy: Literal["rth_only"] | None,
    allowed_sessions: tuple[SessionKind, ...] | None,
) -> SessionAuthorityState:
    permitted = allowed_sessions or ("RTH",)
    permits = phase in permitted
    return SessionAuthorityState(
        phase=phase,
        permits_strategy_activity=permits,
        next_transition_ms=next_transition_ms,
        timezone=timezone,
        as_of_ms=now_ms,
        source=source,
    )


def _window_tuple(
    capability: SessionDataCapability,
    kind: SessionKind,
) -> tuple[int, int] | None:
    session = capability.sessions.get(kind)
    if session is None:
        return None
    if session.window_today_open_ms is None or session.window_today_close_ms is None:
        return None
    return session.window_today_open_ms, session.window_today_close_ms


def _next_capability_transition(
    now_ms: int,
    windows: dict[SessionKind, tuple[int, int]],
) -> int | None:
    transitions = sorted(
        boundary
        for open_ms, close_ms in windows.values()
        for boundary in (open_ms, close_ms)
        if boundary > now_ms
    )
    return transitions[0] if transitions else None


def _ny_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(_NY)


def _at(ny_day: datetime, t: time) -> datetime:
    return datetime.combine(ny_day.date(), t, tzinfo=_NY)


def _ms_utc(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def _next_session_pre_open(now_ny: datetime) -> int:
    candidate = next_trading_day(now_ny.date())
    target = datetime.combine(candidate, _PRE_OPEN, tzinfo=_NY)
    return _ms_utc(target)
