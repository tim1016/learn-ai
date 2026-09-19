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

**Injected failure.** A process-local, thread-safe mode flag
(:func:`set_recorded_history_mode`) lets the qualification-only control
router (``app.routers.fleet_qualification_history``) switch this provider
between ``"healthy"``, ``"slow"`` (delays before answering, to hold a Clerk
request-pool slot for the fleet-lane capacity ceremony step) and
``"unavailable"`` (raises :class:`RecordedHistoryInjectedUnavailable`, which
``internal_fleet.py`` turns into a non-200 response -- the same "unexpected
coordinator response" the real ``RemoteHistoryBatchClient`` already converts
into the stable ``coordinator_unavailable`` notice, FR-010). No sleeping is
used to model "unavailable": a real timeout and a real non-200 response are
already equivalent as far as the Clerk-side client is concerned, so an
immediate refusal keeps the ceremony fast while still exercising that exact
fallback.

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
import math
import threading
from datetime import date
from typing import Literal

from app.data_lake.polygon_fetcher import PolygonBar
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.schemas.broker_v2_panel import ChartHistoryTimeframe
from app.schemas.fleet_history_batch import HistoryBatchResponse
from app.services.broker_v2_panel.chart_projection_service import MS_PER_DAY
from app.services.broker_v2_panel.history_batch_walk import fetch_complete_history_batch

#: The generator's own constants. Not a "seed" in the RNG sense (there is no
#: RNG -- see ``_stable_unit_fraction``): these just keep the synthesized
#: price series in a plausible, strictly-positive range so downstream OHLC
#: invariants (high >= max(o,c), low <= min(o,c), volume > 0) hold trivially.
_BASE_PRICE = 100.0
_WAVE_AMPLITUDE = 2.0
_WAVE_PERIOD_MS = 6 * 3_600_000  # an arbitrary 6-hour wave; only determinism matters
_NOISE_AMPLITUDE = 0.25

RecordedHistoryMode = Literal["healthy", "slow", "unavailable"]
_VALID_MODES: frozenset[str] = frozenset(("healthy", "slow", "unavailable"))

#: How long "slow" mode holds the coordinator's response before answering
#: healthy -- long enough for a concurrent capacity probe to observe the
#: Clerk's one-slot qualification request pool as occupied, short enough to
#: keep the ceremony fast and stay far under ``HISTORY_BATCH_INNER_TIMEOUT_S``
#: (45s) and the fleet-qualification harness's own ``/hold/request`` (1s)
#: convention it mirrors.
SLOW_MODE_DELAY_S = 1.5


class RecordedHistoryInjectedUnavailable(RuntimeError):
    """The recorded provider is in its qualification-injected unavailable mode."""


class _RecordedHistoryModeState:
    """Process-local, thread-safe mode flag (mirrors ``FaultInjectionRegistry``).

    The coordinator runs a single Uvicorn worker (``PythonDataService/Dockerfile``),
    so a module-level, lock-guarded flag is visible to every request without a
    shared store.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode: RecordedHistoryMode = "healthy"

    def get(self) -> RecordedHistoryMode:
        with self._lock:
            return self._mode

    def set(self, mode: RecordedHistoryMode) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"unknown recorded-history mode: {mode!r}")
        with self._lock:
            self._mode = mode


_MODE_STATE = _RecordedHistoryModeState()


def set_recorded_history_mode(mode: RecordedHistoryMode) -> None:
    """Arm the recorded provider's next answers with ``mode`` (issue #2206)."""
    _MODE_STATE.set(mode)


def recorded_history_mode() -> RecordedHistoryMode:
    """The recorded provider's current mode."""
    return _MODE_STATE.get()


def reset_recorded_history_mode_for_testing() -> None:
    """Reset to ``"healthy"`` (test isolation, mirrors ``reset_fault_injection_for_testing``)."""
    _MODE_STATE.set("healthy")


def _span_ms(multiplier: int, timespan: str) -> int:
    if timespan == "minute":
        return multiplier * 60_000
    if timespan == "hour":
        return multiplier * 3_600_000
    if timespan == "day":
        return multiplier * MS_PER_DAY
    raise ValueError(f"unsupported timespan: {timespan!r}")


def _stable_unit_fraction(symbol: str, t_ms: int, salt: int) -> float:
    """A value in ``[0, 1)`` that is a pure, deterministic function of its inputs.

    Uses ``hashlib.sha256`` rather than Python's builtin ``hash()``: string
    hashing is salted per-process (``PYTHONHASHSEED``) unless disabled, so
    ``hash()`` would make the same ``(symbol, t_ms)`` produce a different bar
    across ceremony runs or even across the coordinator's own worker restarts
    -- exactly the non-determinism this generator exists to avoid.
    """
    digest = hashlib.sha256(f"{symbol}:{t_ms}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def _deterministic_close(symbol: str, t_ms: int) -> float:
    wave = _WAVE_AMPLITUDE * math.sin(2 * math.pi * t_ms / _WAVE_PERIOD_MS)
    noise = (_stable_unit_fraction(symbol, t_ms, salt=0) - 0.5) * 2 * _NOISE_AMPLITUDE
    return _BASE_PRICE + wave + noise


def _deterministic_bar(symbol: str, t_ms: int, span_ms: int) -> PolygonBar:
    """One deterministic OHLCV bar for ``t_ms``, continuous with its neighbor.

    ``open`` is the previous slot's ``close`` (computed directly from the same
    pure function, not read from a cache), so the synthesized series is
    continuous across window boundaries even though the backward-widening
    walk fetches different, non-overlapping date ranges call by call.
    """
    close = _deterministic_close(symbol, t_ms)
    open_ = _deterministic_close(symbol, t_ms - span_ms)
    high = max(open_, close) + _stable_unit_fraction(symbol, t_ms, salt=1) * 0.1 + 0.01
    low = min(open_, close) - _stable_unit_fraction(symbol, t_ms, salt=2) * 0.1 - 0.01
    volume = 1_000 + int(_stable_unit_fraction(symbol, t_ms, salt=3) * 9_000)
    vwap = (open_ + high + low + close) / 4
    return PolygonBar(
        t_ms=t_ms, open=open_, high=high, low=low, close=close, volume=volume, vwap=vwap, n=1
    )


def _recorded_bars(symbol: str, start: date, end: date, multiplier: int, timespan: str) -> list[PolygonBar]:
    """Enumerate every scheduled bar-start in ``[start, end]`` and synthesize it.

    Every scheduled instant comes from :func:`session_windows_ms_utc` (the
    canonical NYSE calendar module) -- half-days, weekends, and holidays are
    handled exactly as the real Polygon walk expects them to be, with no
    hardcoded session boundary (``temporal-rigor.md``).
    """
    span_ms = _span_ms(multiplier, timespan)
    windows = session_windows_ms_utc(start, end)
    if timespan == "day":
        return [_deterministic_bar(symbol, window.open_ms_utc, span_ms) for window in windows]
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
