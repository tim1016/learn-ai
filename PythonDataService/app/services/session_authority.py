from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar import trading_calendar
from app.lean_sidecar.trading_calendar import is_trading_day, next_trading_day, session_window_for_date
from app.marketdata.feed import BarSessionPhase
from app.schemas.broker_capability import SessionKind
from app.utils.timestamps import to_ms_utc

# Not a second definition of the phase set: this is the canonical object from
# ``app.marketdata.feed``, aliased so session code reads in session vocabulary
# rather than bar vocabulary. ``TradingSessionPhase is BarSessionPhase``.
TradingSessionPhase = BarSessionPhase
#: What the calendar *schedules* for an instant (``scheduled_exchange_phase_at_ms``).
#: Deliberately a narrower, distinct alias: it labels bars and arms the IBKR
#: line watchdog, and proves no session the way ``TradingSessionPhase`` from
#: ``session_state_at_ms`` does. Every member is a ``BarSessionPhase``.
ScheduledExchangePhase = Literal["PRE", "RTH", "POST", "CLOSED"]
SessionAuthoritySource = Literal["nyse_calendar", "broker_declared_window"]

_NY = ZoneInfo("America/New_York")

# The phase sets this repo asks about, named for what they mean and defined
# once — they were four definitions with three memberships before the slice-3
# fix wave. ``TradingSessionPhase`` is the vocabulary; these are the questions.
# "Which phases does an extended run decide on?" is deliberately not among
# them: that answer is the declared window's span, and
# ``RunDecisionSession.includes`` reads the bounds rather than a phase set.
#: Sessions outside the regular one.
EXTENDED_PHASES: frozenset[TradingSessionPhase] = frozenset({"PRE", "POST", "OVERNIGHT"})
#: Extended sessions a program leg may actually be placed into (ADR 0059 D5.3
#: shapes a marketable limit for these; OVERNIGHT is a separate venue, R1).
TRADEABLE_EXTENDED_PHASES: frozenset[TradingSessionPhase] = frozenset({"PRE", "POST"})


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


@lru_cache(maxsize=512)
def declared_session_bounds(
    session_date: date, window: ExtendedHoursWindow
) -> ExtendedSessionBounds | None:
    """The declared window on ``session_date``, or ``None`` when it is not a trading day.

    One trading day's bounds are four integers that never change, but deriving
    them builds a fresh ``pandas_market_calendars`` schedule twice
    (``is_trading_day`` then ``session_window_for_date``) — ~3 ms. A per-bar
    predicate asking the same day about 390 bars paid that 390 times, which is
    what pinned the event loop on warmup and made the replay proof's two
    200k-bar filters minutes of blocking CPU. Resolve the day once; every
    caller after that compares integers.

    ``ExtendedHoursWindow`` is a frozen Pydantic model, so it hashes; the
    cache key is (date, window) and a differently-declared window can never
    read another window's bounds.
    """
    if not is_trading_day(session_date):
        return None
    return extended_session_bounds_ms(session_date, window=window)


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
    strategy_session_policy: Literal["rth_only"] | None = None,
    allowed_sessions: tuple[SessionKind, ...] | None = None,
    extended_window: ExtendedHoursWindow | None = None,
) -> SessionAuthorityState:
    """Return the scheduled session state at ``now_ms``.

    The canonical NYSE calendar can prove only RTH/CLOSED. PRE and POST are
    proven only by the executing broker's declared extended-hours window
    (``extended_window``, ADR 0059 D5.2), which resolves them by declaration
    around the calendar's regular session. Nothing proves OVERNIGHT; no
    caller may infer an extended phase from a local clock.
    """
    if now_ms < 0:
        raise ValueError("now_ms must be non-negative int64 ms UTC")
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


def declared_extended_phase_at_ms(*, now_ms: int, extended_window: ExtendedHoursWindow | None) -> bool:
    """Whether the broker's declared window puts ``now_ms`` in PRE or POST.

    The declared ``extended_window`` (ADR 0059 D5.2) is the only proof of an
    extended phase: with none, nothing is proven and the answer is ``False``.
    A resolved ``RTH`` or ``CLOSED`` is not extended either, so a running
    ``use_rth=False`` bot can never override fresh broker ``CLOSED`` evidence
    outside the declared window. The ENTER gate and its Clerk-boundary recheck
    (``market_liveness.MarketEntryPolicy``) and the panel's market pulse share
    this one predicate (#1671).

    **This answers the schedule, never liveness.** A declared window describes
    the session that was *supposed* to run at ``now_ms``; it cannot see an
    unscheduled PRE/POST closure, which Alpaca's RTH-only clock reports as
    plain ``CLOSED``, exactly as it reports an ordinary extended session. So a
    ``True`` here is a necessary condition for admitting extended exposure and
    never a sufficient one: ``market_liveness.liveness_blocks_entry`` pairs it
    with ``market_data_bars_live`` — the feed actually printing bars for the
    symbol — and admits only on both (ADR 0022: the calendar owns scheduled
    structure, the live feed owns liveness).
    """
    if extended_window is None:
        return False
    return session_state_at_ms(now_ms=now_ms, extended_window=extended_window).phase in EXTENDED_PHASES


def order_session_state_at_ms(
    *, now_ms: int, extended_window: ExtendedHoursWindow | None
) -> SessionAuthorityState:
    """The session a broker order placed at ``now_ms`` goes out in (#2440).

    :func:`session_state_at_ms` over the broker's declared window, with the
    after-hours session ending at the earlier of the declared close and the
    calendar's scheduled after-hours close. The declared window is one fixed
    pair of wall-clock minutes; on an early-close day the calendar ends
    after-hours hours earlier (2026-11-27: the regular close is 13:00 and
    after-hours ends at 17:00, not at the declared 20:00), and an order placed
    in between would reach the broker after its session. Past that close the
    instant is ``CLOSED`` until the declared window's next open. Order-placing
    code reads this; the declared window itself — which bars a run decides
    on — is unchanged (#2391 moves the scheduled bounds into the canonical
    calendar).
    """
    state = session_state_at_ms(now_ms=now_ms, extended_window=extended_window)
    if extended_window is None or state.phase != "POST" or state.next_transition_ms is None:
        return state
    session_date = _ny_dt(now_ms).date()
    scheduled = scheduled_extended_session_bounds(session_date)
    if scheduled is None or scheduled.close_ms >= state.next_transition_ms:
        return state
    if now_ms < scheduled.close_ms:
        return replace(state, next_transition_ms=scheduled.close_ms)
    next_open_ms = extended_session_bounds_ms(
        next_trading_day(session_date), window=extended_window
    ).open_ms
    return _state(
        phase="CLOSED",
        now_ms=now_ms,
        next_transition_ms=next_open_ms,
        timezone=state.timezone,
        source=state.source,
        extended_phase_proven=state.extended_phase_proven,
        strategy_session_policy=None,
        allowed_sessions=None,
    )


def scheduled_exchange_phase_at_ms(ts_ms: int) -> ScheduledExchangePhase:
    """The phase the canonical calendar schedules for ``ts_ms``: PRE, RTH, POST or CLOSED.

    This answers *when the exchange's extended session is scheduled*, which is
    what labels an IBKR bar and what arms the liveness watchdog of a
    ``useRTH=0`` line (#2299, #2313). It grants no strategy permission: that is
    :func:`session_state_at_ms`, which still needs a declared window before it
    will call an instant PRE or POST. The return type is
    :data:`ScheduledExchangePhase`, not ``TradingSessionPhase``, so the two
    answers cannot be mistaken for one another.

    Every bound comes from the calendar's schedule, so a half-day's early close
    moves the after-hours close with it. OVERNIGHT is never answered here.
    """
    if ts_ms < 0:
        raise ValueError("ts_ms must be non-negative int64 ms UTC")
    bounds = scheduled_extended_session_bounds(_ny_dt(ts_ms).date())
    if bounds is None or not (bounds.open_ms <= ts_ms < bounds.close_ms):
        return "CLOSED"
    if ts_ms < bounds.rth_open_ms:
        return "PRE"
    if ts_ms < bounds.rth_close_ms:
        return "RTH"
    return "POST"


@lru_cache(maxsize=512)
def scheduled_extended_session_bounds(session_date: date) -> ExtendedSessionBounds | None:
    """``session_date``'s pre-market open, regular session and after-hours close, or ``None``.

    A thin adapter, not a second calendar. The regular open and close come from
    the canonical :func:`~app.lean_sidecar.trading_calendar.session_window_for_date`;
    only the ``pre``/``post`` columns, which that module has no public accessor
    for, are read from its one calendar object (no second calendar is built).
    ``trading_calendar.py`` is a sealed program artifact (every signal
    program's ``artifact_paths``), so adding that accessor there would
    invalidate every golden-qualification receipt; #2391 moves this into the
    canonical calendar at the next planned re-seal. The regular-session parity
    with ``app/lean_sidecar/trading_calendar.py`` is pinned by
    ``tests/services/test_session_authority.py``. Memoised: a session day's
    schedule never changes, and the bar-liveness gate asks about the same day
    every 100 ms.
    """
    schedule = trading_calendar._CALENDAR.schedule(
        start_date=session_date, end_date=session_date, start="pre", end="post"
    )
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    regular = session_window_for_date(session_date)
    return ExtendedSessionBounds(
        open_ms=to_ms_utc(row["pre"]),
        rth_open_ms=regular.open_ms_utc,
        rth_close_ms=regular.close_ms_utc,
        close_ms=to_ms_utc(row["post"]),
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
    capability), which is what ``extended_phase_proven`` means.
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

    bounds = declared_session_bounds(day, window)
    if bounds is None:
        return _state_for("CLOSED", _next_open(day))
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


def _ny_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).astimezone(_NY)


def _next_session_open(now_ny: datetime) -> int:
    candidate = next_trading_day(now_ny.date())
    return session_window_for_date(candidate).open_ms_utc
