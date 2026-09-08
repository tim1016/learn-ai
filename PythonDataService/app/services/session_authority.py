from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import is_trading_day, next_trading_day, session_window_for_date
from app.marketdata.feed import BarSessionPhase
from app.schemas.broker_capability import SessionDataCapability, SessionKind
from app.utils.timestamps import to_ms_utc

# Not a second definition of the phase set: this is the canonical object from
# ``app.marketdata.feed``, aliased so session code reads in session vocabulary
# rather than bar vocabulary. ``TradingSessionPhase is BarSessionPhase``.
TradingSessionPhase = BarSessionPhase
SessionAuthoritySource = Literal["ibkr_capability", "nyse_calendar", "broker_declared_window"]

_NY = ZoneInfo("America/New_York")
_SESSION_PRIORITY: tuple[SessionKind, ...] = ("RTH", "PRE", "POST", "OVERNIGHT")
_DAY_SESSION_SEQUENCE: tuple[SessionKind, ...] = ("PRE", "RTH", "POST")
# A snapshot publishes one day's per-instrument windows plus an overnight
# boundary.  Retaining it beyond a day could project yesterday's entitlement
# onto a new session, so it cannot author an extended phase after this bound.
CAPABILITY_MAX_AGE_MS = 24 * 60 * 60 * 1_000


def et_minute_of_day_ms(day: date, minute_of_day: int) -> int:
    """``minute_of_day`` past midnight on ``day`` in America/New_York, as int64 ms UTC.

    Wall-clock arithmetic on an aware datetime, so the UTC offset is the one
    in force at that wall time — a fixed offset would be an hour wrong across
    a DST boundary (temporal-rigor rule). The minutes are capability data;
    this function holds no session literal of its own.
    """
    wall = datetime(day.year, day.month, day.day, tzinfo=_NY) + timedelta(minutes=minute_of_day)
    return to_ms_utc(wall)


@dataclass(frozen=True)
class ExtendedSessionBounds:
    """One trading day's declared extended session around its regular session."""

    open_ms: int
    rth_open_ms: int
    rth_close_ms: int
    close_ms: int


def extended_session_bounds_ms(session_date: date, *, window: ExtendedHoursWindow) -> ExtendedSessionBounds:
    """The declared window applied to ``session_date``'s calendar session (ADR 0059 D5.2)."""
    if not is_trading_day(session_date):
        raise ValueError(f"{session_date.isoformat()} is not a trading day")
    regular = session_window_for_date(session_date)
    open_ms = et_minute_of_day_ms(session_date, window.open_minute_et)
    close_ms = et_minute_of_day_ms(session_date, window.close_minute_et)
    if not (open_ms <= regular.open_ms_utc and regular.close_ms_utc <= close_ms):
        raise ValueError("the declared extended window must enclose the regular session")
    return ExtendedSessionBounds(
        open_ms=open_ms,
        rth_open_ms=regular.open_ms_utc,
        rth_close_ms=regular.close_ms_utc,
        close_ms=close_ms,
    )


@dataclass(frozen=True)
class SessionAuthorityState:
    phase: TradingSessionPhase
    permits_strategy_activity: bool
    next_transition_ms: int | None
    timezone: str
    as_of_ms: int
    source: SessionAuthoritySource
    extended_phase_proven: bool


def session_state_at_ms(
    *,
    now_ms: int,
    capability: SessionDataCapability | None = None,
    symbol: str | None = None,
    account_id: str | None = None,
    strategy_session_policy: Literal["rth_only"] | None = None,
    allowed_sessions: tuple[SessionKind, ...] | None = None,
    extended_window: ExtendedHoursWindow | None = None,
) -> SessionAuthorityState:
    """Return the scheduled session state for one instrument and account.

    The canonical NYSE calendar can prove only RTH/CLOSED. PRE and POST can
    also be proven by the executing broker's declared extended-hours window
    (``extended_window``), which resolves them by declaration around the
    calendar's regular session — no probe required. OVERNIGHT still needs a
    current capability snapshot matched to both the target instrument and
    account; no caller may infer it from a local clock.
    """
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative int64 ms UTC")
    if _is_fresh_matching_capability(
        capability,
        now_ms=now_ms,
        symbol=symbol,
        account_id=account_id,
    ):
        assert capability is not None
        state = _session_from_capability(
            now_ms=now_ms,
            capability=capability,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )
        if state is not None:
            return state
    if extended_window is not None:
        return _session_from_declared_window(
            now_ms=now_ms,
            window=extended_window,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )
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
            extended_phase_proven=True,
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
        extended_phase_proven=True,
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
            next_transition_ms=_next_session_open(now_ny),
            timezone="America/New_York",
            source="nyse_calendar",
            extended_phase_proven=False,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )

    if now_ms < session_window.open_ms_utc:
        phase: TradingSessionPhase = "CLOSED"
        next_transition_ms = session_window.open_ms_utc
    elif now_ms < session_window.close_ms_utc:
        phase = "RTH"
        next_transition_ms = session_window.close_ms_utc
    else:
        phase = "CLOSED"
        next_transition_ms = _next_session_open(now_ny)

    return _state(
        phase=phase,
        now_ms=now_ms,
        next_transition_ms=next_transition_ms,
        timezone="America/New_York",
        source="nyse_calendar",
        extended_phase_proven=False,
        strategy_session_policy=strategy_session_policy,
        allowed_sessions=allowed_sessions,
    )


def _session_from_declared_window(
    *,
    now_ms: int,
    window: ExtendedHoursWindow,
    strategy_session_policy: Literal["rth_only"] | None,
    allowed_sessions: tuple[SessionKind, ...] | None,
) -> SessionAuthorityState:
    """PRE/RTH/POST from the calendar's regular session and the broker's declared window.

    Proven by declaration (the executing broker publishes the window as a
    capability), which is what ``extended_phase_proven`` means for a broker
    with no probe-based session capability.
    """
    day = _ny_dt(now_ms).date()

    def _state_for(phase: TradingSessionPhase, next_transition_ms: int) -> SessionAuthorityState:
        return _state(
            phase=phase,
            now_ms=now_ms,
            next_transition_ms=next_transition_ms,
            timezone="America/New_York",
            source="broker_declared_window",
            extended_phase_proven=True,
            strategy_session_policy=strategy_session_policy,
            allowed_sessions=allowed_sessions,
        )

    def _next_open(after: date) -> int:
        return extended_session_bounds_ms(next_trading_day(after), window=window).open_ms

    if not is_trading_day(day):
        return _state_for("CLOSED", _next_open(day))
    bounds = extended_session_bounds_ms(day, window=window)
    if now_ms < bounds.open_ms:
        return _state_for("CLOSED", bounds.open_ms)
    if now_ms < bounds.rth_open_ms:
        return _state_for("PRE", bounds.rth_open_ms)
    if now_ms < bounds.rth_close_ms:
        return _state_for("RTH", bounds.rth_close_ms)
    if now_ms < bounds.close_ms:
        return _state_for("POST", bounds.close_ms)
    return _state_for("CLOSED", _next_open(day))


def _state(
    *,
    phase: TradingSessionPhase,
    now_ms: int,
    next_transition_ms: int | None,
    timezone: str,
    source: SessionAuthoritySource,
    extended_phase_proven: bool,
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
        extended_phase_proven=extended_phase_proven,
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


def _is_fresh_matching_capability(
    capability: SessionDataCapability | None,
    *,
    now_ms: int,
    symbol: str | None,
    account_id: str | None,
) -> bool:
    """Accept only a current, scoped, structurally valid capability snapshot."""
    if capability is None or symbol is None or account_id is None:
        return False
    if capability.symbol != symbol.upper() or capability.account_id != account_id:
        return False
    age_ms = now_ms - capability.probed_at_ms
    if age_ms < 0 or age_ms > CAPABILITY_MAX_AGE_MS:
        return False
    if not all(_is_valid_window(capability, kind) for kind in _SESSION_PRIORITY):
        return False
    return _has_ordered_day_sessions(capability)


def _is_valid_window(capability: SessionDataCapability, kind: SessionKind) -> bool:
    session = capability.sessions.get(kind)
    if session is None:
        return False
    open_ms = session.window_today_open_ms
    close_ms = session.window_today_close_ms
    if open_ms is None or close_ms is None:
        return open_ms is None and close_ms is None
    return open_ms < close_ms


def _has_ordered_day_sessions(capability: SessionDataCapability) -> bool:
    """Reject overlapping or out-of-order PRE/RTH/POST evidence windows."""
    windows = [
        window
        for kind in _DAY_SESSION_SEQUENCE
        if (window := _window_tuple(capability, kind)) is not None
    ]
    return all(
        earlier_close_ms <= later_open_ms
        for (_earlier_open_ms, earlier_close_ms), (later_open_ms, _later_close_ms) in pairwise(
            windows
        )
    )


def _ny_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(_NY)


def _next_session_open(now_ny: datetime) -> int:
    candidate = next_trading_day(now_ny.date())
    return session_window_for_date(candidate).open_ms_utc


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
