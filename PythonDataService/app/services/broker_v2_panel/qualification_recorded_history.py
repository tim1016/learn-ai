"""Qualification-only recorded history provider (issue #2206).

The Compose qualification environment gives the fleet-coordinator role a
non-working placeholder Polygon key (``qualification-polygon-placeholder``,
``compose.fleet.qualification.yaml``), so the coordinator's real provider
(:func:`app.services.broker_v2_panel.history_batch_walk.build_coordinator_history_batch`)
can never return real bars there -- qualification could not deterministically
prove success, failure, or recovery without a real credential.

**Design choice: injected at the complete-batch seam, faked at the
``HistoryBarSource`` vendor seam.** ``app.routers.internal_fleet.history_batch``
selects between the production complete-batch builder and
:func:`build_qualification_recorded_history_batch` -- that selection *is* the
"complete-batch seam" the issue names. But this module's own builder does not
special-case the walk: it hands :func:`fetch_complete_history_batch`
(``history_batch_walk.py``, unchanged) a recorded :data:`HistoryBarSource`
that stands in for the one real HTTP dependency (Polygon), exactly where the
production coordinator already injects one. The result is that the entire
backward-widening walk, the two-year floor, the completed-bar/half-day
calendar rules, and the notice-conversion path all keep running for real --
only the vendor HTTP call is replaced. Faking one level lower than the
complete-batch builder exercises strictly more of the production code than
short-circuiting the walk would, at no extra cost, so it is the one
implemented here.

**Recorded bars.** Real Polygon fixtures cannot be replayed verbatim: the
ceremony sends ``as_of_ms = now`` at an arbitrary wall-clock time (PRD
#2201 §11.7), so a fixture pinned to literal historical timestamps would
either miss the walk's requested window or need ad-hoc resampling (banned by
the issue). Instead this module is a **seeded, calendar-aligned generator**:
for any requested ``(symbol, start, end, multiplier, timespan)`` it enumerates
every real NYSE session in range via the canonical calendar module
(``app.lean_sidecar.trading_calendar`` -- never a hardcoded ``09:30``/``16:00``)
and derives one deterministic OHLCV bar per scheduled slot from a stable hash
of ``(symbol, bar_start_ms)``. The same instant always produces the same bar
regardless of which call fetched it or when the ceremony ran, which is what
lets a unit test pin an exact golden batch (see
``tests/fixtures/golden/qualification-recorded-history/``) while the live
ceremony calls the identical function with a real, moving ``as_of_ms``.

**Bit-exact across platforms.** Every price is built entirely from integer
cents -- a triangle wave and a SHA-256-derived offset, both pure integer
arithmetic -- and converted to a float only once, by dividing by 100 at the
very end. Division is correctly rounded on every IEEE-754-conformant
platform, so that one conversion (and CPython's own platform-independent
float-to-string algorithm downstream) is bit-exact everywhere. An earlier
version of this generator used ``math.sin`` for the wave: `sin` is *not*
required to be correctly rounded, and 814 of 20,000 sampled instants differed
by 1 ulp between macOS arm64 and Linux x86_64 -- enough to break the golden
fixture's exact-string comparison on CI (which runs Linux x86_64) even though
it passed locally.

**Daily bars are stamped like Polygon's, not at the session open.** Polygon's
real daily aggregates carry ``t`` = 00:00 America/New_York of the session
date, not that session's 09:30 ET open (confirmed by this repo's own
``test_daily_bar_completes_at_session_close_not_midnight_plus_one_day``).
This generator matches that convention via :func:`_session_midnight_et_ms_utc`
so the recorded fixture is shaped like production input, not merely
internally self-consistent.

**Injected failure.** A process-local mode flag (:func:`set_recorded_history_mode`)
lets the qualification-only control router (``app.routers.fleet_qualification_history``)
switch this provider between ``"healthy"``, ``"slow"`` (delays before answering, to
hold a Clerk request-pool slot for the fleet-lane capacity ceremony step) and
``"unavailable"`` (raises :class:`RecordedHistoryInjectedUnavailable`, which
``internal_fleet.py`` turns into a non-200 response -- the same "unexpected
coordinator response" the real ``RemoteHistoryBatchClient`` already converts
into the stable ``coordinator_unavailable`` notice, FR-010). No sleeping is
used to model "unavailable": a real timeout and a real non-200 response are
already equivalent as far as the Clerk-side client is concerned, so an
immediate refusal keeps the ceremony fast while still exercising that exact
fallback. The flag is a plain module-level variable, not a lock-guarded
object: the coordinator runs a single Uvicorn worker
(``PythonDataService/Dockerfile``), so every request executes on the same
asyncio event loop thread, and a bare assignment is already atomic there --
a lock would guard against a concurrency hazard this process cannot have.

This module is reachable only when
``app.routers.fleet_qualification.is_qualification_coordinator_lane`` (or its
environment-sourced wrapper) is true -- role ``fleet_coordinator``, the random
Compose ceremony namespace, and the ceremony's minted probe secret. Importing
this module is inert everywhere else; nothing here runs unless that seam
opts in.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, time
from typing import Literal, get_args
from zoneinfo import ZoneInfo

from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.schemas.broker_v2_panel import ChartHistoryTimeframe
from app.schemas.fleet_history_batch import HistoryBatchResponse
from app.services.broker_v2_panel.history_batch_walk import (
    fetch_complete_history_batch,
    span_ms_for,
)

#: The generator's own constants, all in integer cents (see the module
#: docstring's "Bit-exact across platforms"). Not a "seed" in the RNG sense
#: (there is no RNG -- see ``_stable_unit_fraction_int``): these just keep
#: the synthesized price series in a plausible, strictly-positive range so
#: downstream OHLC invariants (high >= max(o,c), low <= min(o,c), volume > 0)
#: hold trivially.
_BASE_PRICE_CENTS = 10_000
_WAVE_AMPLITUDE_CENTS = 200
_WAVE_PERIOD_MS = 6 * 3_600_000  # an arbitrary 6-hour wave; only determinism matters
_NOISE_AMPLITUDE_CENTS = 25

_ET = ZoneInfo("America/New_York")

RecordedHistoryMode = Literal["healthy", "slow", "unavailable"]
#: Derived from the ``Literal`` above, not spelled a second time: one place
#: names the three modes, and both this frozenset and the coordinator-side
#: request schema (``app.routers.fleet_qualification_history.RecordedHistoryModeRequest``)
#: read it back rather than repeating the three strings.
_VALID_MODES: frozenset[str] = frozenset(get_args(RecordedHistoryMode))

#: How long "slow" mode holds the coordinator's response before answering
#: healthy -- long enough for a concurrent capacity probe to observe the
#: Clerk's one-slot qualification request pool as occupied, short enough to
#: keep the ceremony fast and stay far under ``HISTORY_BATCH_INNER_TIMEOUT_S``
#: (45s) and the fleet-qualification harness's own ``/hold/request`` (1s)
#: convention it mirrors.
SLOW_MODE_DELAY_S = 1.5


class RecordedHistoryInjectedUnavailable(RuntimeError):
    """The recorded provider is in its qualification-injected unavailable mode."""


#: Process-local mode flag (mirrors ``FaultInjectionRegistry``'s shape, minus
#: its lock -- see the module docstring for why one is not needed here).
_mode: RecordedHistoryMode = "healthy"


def set_recorded_history_mode(mode: RecordedHistoryMode) -> None:
    """Arm the recorded provider's next answers with ``mode`` (issue #2206)."""
    global _mode
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown recorded-history mode: {mode!r}")
    _mode = mode


def recorded_history_mode() -> RecordedHistoryMode:
    """The recorded provider's current mode."""
    return _mode


def reset_recorded_history_mode_for_testing() -> None:
    """Reset to ``"healthy"`` (test isolation, mirrors ``reset_fault_injection_for_testing``)."""
    global _mode
    _mode = "healthy"


def _stable_unit_fraction_int(symbol: str, t_ms: int, salt: int) -> int:
    """A non-negative integer, deterministic and platform-independent.

    Uses ``hashlib.sha256`` rather than Python's builtin ``hash()``: string
    hashing is salted per-process (``PYTHONHASHSEED``) unless disabled, so
    ``hash()`` would make the same ``(symbol, t_ms)`` produce a different bar
    across ceremony runs or even across the coordinator's own worker restarts
    -- exactly the non-determinism this generator exists to avoid. The result
    is never routed through a float division: every caller reduces it with
    integer ``%``, so nothing here can accumulate the platform-dependent
    rounding a transcendental float function could (see the module docstring).
    """
    digest = hashlib.sha256(f"{symbol}:{t_ms}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _triangle_wave_cents(t_ms: int) -> int:
    """A deterministic pseudo-periodic wave, in integer cents.

    Not ``math.sin``: replaced because ``sin`` is not required to be
    correctly rounded, and observably was not -- see the module docstring's
    "Bit-exact across platforms". A triangle wave built from pure integer
    arithmetic (modulo and floor division only) has no such divergence and
    still gives the synthesized series a plausible, bounded, periodic shape;
    the module's own constants comment already notes that only determinism
    matters here, not the wave's exact shape.
    """
    half_period = _WAVE_PERIOD_MS // 2
    phase = t_ms % _WAVE_PERIOD_MS
    if phase < half_period:
        return -_WAVE_AMPLITUDE_CENTS + (2 * _WAVE_AMPLITUDE_CENTS * phase) // half_period
    return _WAVE_AMPLITUDE_CENTS - (2 * _WAVE_AMPLITUDE_CENTS * (phase - half_period)) // half_period


def _close_cents(symbol: str, t_ms: int) -> int:
    """The close price at ``t_ms``, in integer cents -- pure integer arithmetic."""
    wave = _triangle_wave_cents(t_ms)
    noise_span = 2 * _NOISE_AMPLITUDE_CENTS + 1
    noise = (_stable_unit_fraction_int(symbol, t_ms, salt=0) % noise_span) - _NOISE_AMPLITUDE_CENTS
    return _BASE_PRICE_CENTS + wave + noise


def _deterministic_bar(symbol: str, t_ms: int, span_ms: int) -> PolygonBar:
    """One deterministic OHLCV bar for ``t_ms``.

    ``open`` is ``_close_cents`` evaluated at ``t_ms - span_ms`` -- the same
    pure function the previous bar's own ``close`` would use if
    ``t_ms - span_ms`` were itself a real scheduled slot. It is **not**
    actually the prior real bar's close across a gap: a session open, a
    weekend, or a DST-affected span all put ``t_ms - span_ms`` on an instant
    no scheduled bar starts at, so this generator's series is not
    continuous across those gaps the way a real vendor's tape is. What this
    generator actually provides, and needs, is determinism -- the same
    instant always produces the same bar regardless of which call fetched it
    (the backward-widening walk fetches disjoint date ranges call by call) --
    not a gap-free OHLC series.

    Every price is converted from integer cents to a float exactly once
    (``/ 100``), so the only floating-point operation in this function is a
    single correctly-rounded division -- see the module docstring.

    Known differences from a real Polygon bar, accepted because this
    generator exists to prove wiring, not to model microstructure: hourly
    bars are not aligned to a real exchange session boundary beyond their
    calendar-derived start, and no extended-hours session is modeled (every
    bar falls inside the regular 09:30-16:00 ET session the canonical
    calendar reports).
    """
    close_cents = _close_cents(symbol, t_ms)
    open_cents = _close_cents(symbol, t_ms - span_ms)
    high_offset_cents = 1 + _stable_unit_fraction_int(symbol, t_ms, salt=1) % 10
    low_offset_cents = 1 + _stable_unit_fraction_int(symbol, t_ms, salt=2) % 10
    high_cents = max(open_cents, close_cents) + high_offset_cents
    low_cents = min(open_cents, close_cents) - low_offset_cents
    volume = 1_000 + _stable_unit_fraction_int(symbol, t_ms, salt=3) % 9_000
    return PolygonBar(
        t_ms=t_ms,
        open=open_cents / 100,
        high=high_cents / 100,
        low=low_cents / 100,
        close=close_cents / 100,
        volume=volume,
        vwap=(open_cents + high_cents + low_cents + close_cents) / 400,
        n=1,
    )


def _session_midnight_et_ms_utc(session_date: date) -> int:
    """Polygon's own daily-bar timestamp: 00:00 America/New_York of the
    session date -- not that session's 09:30 ET open.

    Confirmed by this repo's own
    ``test_daily_bar_completes_at_session_close_not_midnight_plus_one_day``
    (``tests/broker/v2panel/test_chart_projection.py``), which builds its
    daily ``PolygonBar`` fixture at exactly this instant. ``time(0, 0)`` here
    is not a market-session boundary (the kind ``temporal-rigor.md`` bans as
    a hardcoded literal) -- it is the vendor's own midnight-anchored
    timestamp convention for a *daily* bar, unrelated to the NYSE open/close
    schedule. The conversion still goes through ``ZoneInfo("America/New_York")``
    so DST is handled correctly, never a fixed offset.
    """
    return int(datetime.combine(session_date, time(0, 0), tzinfo=_ET).timestamp() * 1000)


def _recorded_bars(symbol: str, start: date, end: date, multiplier: int, timespan: str) -> list[PolygonBar]:
    """Enumerate every scheduled bar-start in ``[start, end]`` and synthesize it.

    Every scheduled session comes from :func:`session_windows_ms_utc` (the
    canonical NYSE calendar module) -- half-days, weekends, and holidays are
    handled exactly as the real Polygon walk expects them to be, with no
    hardcoded session boundary (``temporal-rigor.md``). Daily bars are
    stamped at session midnight ET (see :func:`_session_midnight_et_ms_utc`);
    intraday bars are stamped at their real session-open-aligned start, per
    the canonical calendar.
    """
    span_ms = span_ms_for(multiplier, timespan)
    windows = session_windows_ms_utc(start, end)
    if timespan == "day":
        return [
            _deterministic_bar(symbol, _session_midnight_et_ms_utc(window.session_date), span_ms)
            for window in windows
        ]
    bars: list[PolygonBar] = []
    for window in windows:
        t_ms = window.open_ms_utc
        while t_ms + span_ms <= window.close_ms_utc:
            bars.append(_deterministic_bar(symbol, t_ms, span_ms))
            t_ms += span_ms
    return bars


async def recorded_bar_source(
    symbol: str, start: date, end: date, multiplier: int, timespan: str
) -> list[PolygonBar]:
    """The qualification :data:`HistoryBarSource` (issue #2206).

    Matches ``history_batch_walk.HistoryBarSource`` exactly, so it plugs into
    the real, unmodified :func:`fetch_complete_history_batch` walk.
    """
    return _recorded_bars(symbol, start, end, multiplier, timespan)


async def build_qualification_recorded_history_batch(
    *,
    symbol: str,
    timeframe: ChartHistoryTimeframe,
    required_bar_count: int,
    as_of_ms: int,
) -> HistoryBatchResponse:
    """The coordinator's qualification-only complete-batch answer (issue #2206).

    Selected by ``app.routers.internal_fleet.history_batch`` in place of
    :func:`app.services.broker_v2_panel.history_batch_walk.build_coordinator_history_batch`
    only under the Compose qualification gate. Raises
    :class:`RecordedHistoryInjectedUnavailable` while armed to ``"unavailable"``
    -- the router converts that into a non-200 response, which the real
    Clerk-side client already treats identically to a timeout (FR-010).
    """
    mode = recorded_history_mode()
    if mode == "unavailable":
        raise RecordedHistoryInjectedUnavailable(
            "qualification recorded history provider is in its injected-unavailable mode"
        )
    if mode == "slow":
        await asyncio.sleep(SLOW_MODE_DELAY_S)
    return await fetch_complete_history_batch(
        symbol=symbol,
        timeframe=timeframe,
        required_bar_count=required_bar_count,
        as_of_ms=as_of_ms,
        bar_source=recorded_bar_source,
    )


__all__ = [
    "SLOW_MODE_DELAY_S",
    "RecordedHistoryInjectedUnavailable",
    "RecordedHistoryMode",
    "build_qualification_recorded_history_batch",
    "recorded_bar_source",
    "recorded_history_mode",
    "reset_recorded_history_mode_for_testing",
    "set_recorded_history_mode",
]
