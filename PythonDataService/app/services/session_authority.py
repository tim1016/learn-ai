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


SessionSubmitBlockReason = Literal[
    "session_closed",
    "strategy_session_not_permitted",
    "order_mechanism_not_enabled",
    "extended_limit_price_unavailable",
]

_TRADEABLE_PHASES: tuple[TradingSessionPhase, ...] = ("PRE", "RTH", "POST", "OVERNIGHT")
_EXTENDED_PHASES: tuple[TradingSessionPhase, ...] = ("PRE", "POST", "OVERNIGHT")


def evaluate_session_submit(
    *,
    phase: TradingSessionPhase,
    allowed_sessions: tuple[SessionKind, ...],
    order_mechanism_sessions: tuple[SessionKind, ...],
    extended_reference_price_ok: bool,
) -> SessionSubmitBlockReason | None:
    """Pure submit-gate decision: return a block reason, or ``None`` to allow.

    Kept free of portfolio state so the branch logic is unit-testable in
    isolation and the submit path carries no session branching of its own.
    ``order_mechanism_sessions`` is the set the *mechanism* can actually place
    into (see :func:`order_mechanism_sessions_from_capability`), a distinct axis
    from the strategy-declared ``allowed_sessions``.
    """
    if phase not in _TRADEABLE_PHASES:
        return "session_closed"
    if phase not in allowed_sessions:
        return "strategy_session_not_permitted"
    if phase not in order_mechanism_sessions:
        return "order_mechanism_not_enabled"
    if phase in _EXTENDED_PHASES and not extended_reference_price_ok:
        return "extended_limit_price_unavailable"
    return None


def order_mechanism_sessions_from_capability(
    capability: SessionDataCapability | None,
    *,
    extended_placement_enabled: bool,
) -> tuple[SessionKind, ...]:
    """Which sessions the *order mechanism* can actually place into.

    RTH is always mechanism-ready (market orders in regular hours). An extended
    session is added only when BOTH (a) extended placement is explicitly enabled
    — which requires the spread-guarded marketable-limit mechanism (PRD #1005
    Slice 3) that is not yet built — AND (b) the capability probe proves the
    broker will accept a live off-hours order on live data. Derived from the
    probe, never from the strategy's declared allow-list, so a strategy cannot
    self-authorize placement into a session the broker or the data can't
    support.
    """
    ready: list[SessionKind] = ["RTH"]
    if extended_placement_enabled and capability is not None:
        for kind in ("PRE", "POST", "OVERNIGHT"):
            session = capability.sessions.get(kind)
            if (
                session is not None
                and session.tradeable == "yes"
                and session.order_eligible_outside_rth
                and session.data == "live"
            ):
                ready.append(kind)
    return tuple(ready)
