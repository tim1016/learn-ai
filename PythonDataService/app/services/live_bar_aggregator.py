"""Live 1-minute OHLCV ring buffer fed by IBKR ``reqRealTimeBars``.

Owns one async background task per symbol that consumes ``stream_minute_bars``
and appends closed 1-min bars to a bounded deque. The HTTP layer reads a
snapshot of the deque on each poll. When a ``BarPersistence`` is attached
the aggregator also:

* Replays today's persisted bars into the deque on each ``ensure_subscribed``
  so a restart hands the chart today's morning bars before the stream
  produces a single new one (Slice 4).
* Writes each emitted bar through persistence so a future restart can
  replay them.

Lifecycle:

* ``ensure_subscribed(symbol)`` — idempotent; first call replays the
  persisted log (if any) and starts the task; subsequent calls are no-ops
  while the task is alive.
* ``snapshot(symbol, since_ms=None)`` — returns the bars currently in the
  deque (optionally filtered to ``start_ms > since_ms``).
* A task that errors (broker disconnect, IBKRBarStreamError) records the
  cause on ``state.last_error`` and exits. The next ``ensure_subscribed``
  call restarts it.

This module deliberately runs inside the FastAPI process and shares the
public broker session (client_id 42) via ``app.broker.ibkr.client.get_client``
— it does NOT spin up a second IBKR socket. Within that process, 1-minute and
5-second consumers for the same contract multiplex through the shared
registry in ``app.broker.ibkr.bars``. Host-runner children remain separate
processes/IBKR clients because their order identity is intentionally isolated.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from app.broker.ibkr.bars import stream_minute_bars, stream_raw_5s_bars
from app.broker.ibkr.client import IbkrClient, NotConnectedError, get_client
from app.broker.ibkr.models import IbkrMinuteBar
from app.services.bar_persistence import (
    BarPersistence,
    BarPersistenceRegressionError,
)

logger = logging.getLogger(__name__)

# 500 closed 1-min bars ≈ 8h 20m of RTH — covers a full session plus
# warmup tail. Plenty for the chart, bounded so the buffer can't grow.
_RING_BUFFER_SIZE = 500

# 4 000 raw 5-sec bars ≈ 5h 33m at one bar every 5 seconds. The 5-sec
# chart is for short-term, high-resolution context — half a session of
# tape is plenty, and the buffer would otherwise dominate process memory
# (4 000 × ~120 bytes/bar = ~470 kB per symbol, vs ~60 kB for 1-min).
_RING_BUFFER_SIZE_5S = 4_000

# Expected (end_ms - start_ms) per resolution. The partial-bar guard at
# the stream/consolidator boundary (Slice 4) drops any bar whose window
# does not match — those are restart-mid-minute artifacts that would
# otherwise render as a ragged short first candle.
_EXPECTED_WINDOW_MS_1M = 60_000
_EXPECTED_WINDOW_MS_5S = 5_000


SubscriptionStatus = Literal[
    "idle", "subscribing", "streaming", "errored", "resubscribing"
]


@dataclass
class _SymbolState:
    """Per-symbol subscription state."""

    bars: deque[IbkrMinuteBar] = field(default_factory=lambda: deque(maxlen=_RING_BUFFER_SIZE))
    task: asyncio.Task | None = None
    status: SubscriptionStatus = "idle"
    last_error: str | None = None
    last_bar_ms: int | None = None


def _today_utc():
    """UTC date for the current wall clock. The replay path uses this as
    the partition key for today's JSONL."""
    return datetime.now(UTC).date()


class LiveBarAggregator:
    """Singleton-style aggregator owning per-symbol background streams.

    Construct it once at module import time; the FastAPI router holds a
    reference. The aggregator does not own the IBKR connection — it
    resolves the broker client via ``get_client()`` on demand so connect /
    disconnect lifecycle changes are observed naturally.

    Optional ``persistence`` enables restart resilience (Slice 4): on
    subscribe the aggregator replays today's JSONL into the ring buffer
    and writes every emitted bar through the same store so the next
    restart finds them on disk.
    """

    def __init__(
        self,
        persistence: BarPersistence | None = None,
        *,
        today_provider: Callable[[], object] | None = None,
    ) -> None:
        self._states: dict[str, _SymbolState] = {}
        self._states_5s: dict[str, _SymbolState] = {}
        # Protect dict access from concurrent ensure_subscribed calls.
        self._lock = asyncio.Lock()
        self._persistence = persistence
        # Allow tests to inject a fixed "today" so persisted bars with a
        # historical anchor still replay through ensure_subscribed.
        self._today_provider = today_provider or _today_utc

    def _key(self, symbol: str) -> str:
        return symbol.strip().upper()

    async def ensure_subscribed(self, symbol: str) -> _SymbolState:
        """Start the per-symbol 1-min stream task if not already running.

        On first call (or first call after the task exited), the persisted
        JSONL for today is replayed into the deque so the chart shows
        today's bars immediately instead of waiting for the stream to
        produce the next minute.

        Failure stickiness: when a prior task ended with ``status='errored'``
        (e.g. broker disconnected at task start), the state's
        ``last_error`` is preserved here so a snapshot poll between
        restart and the new task's first bar still surfaces the failure
        to the operator. ``_pump`` clears ``last_error`` and flips
        ``status`` to ``streaming`` on the first successful bar.
        """
        key = self._key(symbol)
        async with self._lock:
            state = self._states.setdefault(key, _SymbolState())
            self._seed_from_persistence(state, key, "1m")
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    self._run_stream(key, state), name=f"live-bar-stream:{key}"
                )
                # Only nudge ``status`` back to 'subscribing' when there is
                # no prior failure to remember. The pump clears ``last_error``
                # on its first successful bar (see ``_pump``).
                if state.last_error is None:
                    state.status = "subscribing"
            return state

    async def ensure_subscribed_5s(self, symbol: str) -> _SymbolState:
        """Start the per-symbol raw 5-sec stream task if not already running.

        Independent buffer and task from ``ensure_subscribed`` (the 1-min
        path), but the two consumers share one underlying ``reqRealTimeBars``
        line when they use the same public client and contract. Preserves
        ``last_error`` across restart for the same reason documented on
        ``ensure_subscribed``.
        """
        key = self._key(symbol)
        async with self._lock:
            state = self._states_5s.setdefault(
                key, _SymbolState(bars=deque(maxlen=_RING_BUFFER_SIZE_5S))
            )
            self._seed_from_persistence(state, key, "5s")
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    self._run_stream_5s(key, state),
                    name=f"live-bar-stream-5s:{key}",
                )
                if state.last_error is None:
                    state.status = "subscribing"
            return state

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[IbkrMinuteBar]:
        """Return buffered 1-min bars for ``symbol`` (start_ms ascending).

        ``since_ms`` filters to bars with ``start_ms > since_ms`` so the
        frontend can paginate incrementally. Returns ``[]`` if the symbol
        has never been subscribed or the buffer is empty.
        """
        return self._snapshot_from(self._states.get(self._key(symbol)), since_ms)

    def snapshot_5s(self, symbol: str, since_ms: int | None = None) -> list[IbkrMinuteBar]:
        """Return buffered raw 5-sec bars for ``symbol`` (start_ms ascending)."""
        return self._snapshot_from(self._states_5s.get(self._key(symbol)), since_ms)

    @staticmethod
    def _snapshot_from(
        state: _SymbolState | None, since_ms: int | None
    ) -> list[IbkrMinuteBar]:
        if state is None:
            return []
        if since_ms is None:
            return list(state.bars)
        return [b for b in state.bars if b.start_ms > since_ms]

    def status(self, symbol: str) -> tuple[SubscriptionStatus, str | None, int | None]:
        """Return (status, last_error, last_bar_ms) for the 1-min stream."""
        state = self._states.get(self._key(symbol))
        if state is None:
            return "idle", None, None
        return state.status, state.last_error, state.last_bar_ms

    def status_5s(
        self, symbol: str
    ) -> tuple[SubscriptionStatus, str | None, int | None]:
        """Return (status, last_error, last_bar_ms) for the 5-sec stream."""
        state = self._states_5s.get(self._key(symbol))
        if state is None:
            return "idle", None, None
        return state.status, state.last_error, state.last_bar_ms

    async def shutdown(self) -> None:
        """Cancel all running tasks. Safe to call multiple times."""
        async with self._lock:
            tasks: list[asyncio.Task] = []
            for state in (*self._states.values(), *self._states_5s.values()):
                if state.task is not None and not state.task.done():
                    state.task.cancel()
                    tasks.append(state.task)
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception) as exc:
                logger.debug("Aggregator task ended on shutdown: %s", exc)

    async def resubscribe_all(self) -> None:
        """Recreate every active live-bar subscription after broker recovery.

        IBKR code 1101 means market-data subscriptions were lost. The broker
        monitor calls this after reconnect so already-open chart/watch streams
        don't stay stuck until a user happens to poll the exact endpoint that
        lazily restarts them.
        """
        async with self._lock:
            restart_1m = list(self._states.items())
            restart_5s = list(self._states_5s.items())
            tasks: list[asyncio.Task] = []
            for _, state in (*restart_1m, *restart_5s):
                if state.task is not None and not state.task.done():
                    state.task.cancel()
                    tasks.append(state.task)
                state.status = "resubscribing"
                state.last_error = None
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception) as exc:
                logger.debug("Aggregator task ended during resubscribe: %s", exc)
        async with self._lock:
            for symbol, state in restart_1m:
                state.task = asyncio.create_task(
                    self._run_stream(symbol, state),
                    name=f"live-bar-stream:{symbol}",
                )
            for symbol, state in restart_5s:
                state.task = asyncio.create_task(
                    self._run_stream_5s(symbol, state),
                    name=f"live-bar-stream-5s:{symbol}",
                )
        if restart_1m or restart_5s:
            logger.info(
                "Resubscribed live bar streams after broker recovery",
                extra={
                    "action": "live_bar_resubscribe_all",
                    "one_minute_count": len(restart_1m),
                    "five_second_count": len(restart_5s),
                },
            )

    def _seed_from_persistence(
        self, state: _SymbolState, symbol: str, resolution: str
    ) -> None:
        """Hydrate the deque with today's persisted bars (Slice 4).

        Idempotent: once the deque has any bars (from a prior replay or
        from the live stream) further calls leave it alone — a stream
        already updating the buffer must not be wound back to a stale
        on-disk snapshot.
        """
        if self._persistence is None:
            return
        if state.bars:
            return
        try:
            bars = self._persistence.replay(symbol, resolution, self._today_provider())
        except Exception as exc:
            logger.warning(
                "Replay from BarPersistence failed for %s/%s: %s",
                symbol,
                resolution,
                exc,
            )
            return
        for bar in bars:
            state.bars.append(bar)
        if bars:
            state.last_bar_ms = bars[-1].start_ms
            logger.info(
                "Replayed %d %s bars for %s from persistence",
                len(bars),
                resolution,
                symbol,
            )

    async def _run_stream(self, symbol: str, state: _SymbolState) -> None:
        """Background task: pump ``stream_minute_bars`` into the deque.

        ``guard_partial_first_bar=True`` because the 1-min path runs through a
        consolidator: a daemon restart mid-minute lets the consolidator close
        an under-filled first minute. The 5s path streams raw IBKR bars (no
        consolidation), so it does not need the guard.
        """
        await self._pump(
            symbol,
            state,
            stream_minute_bars,
            "1m",
            _EXPECTED_WINDOW_MS_1M,
            guard_partial_first_bar=True,
        )

    async def _run_stream_5s(self, symbol: str, state: _SymbolState) -> None:
        """Background task: pump ``stream_raw_5s_bars`` into the deque."""
        await self._pump(
            symbol,
            state,
            stream_raw_5s_bars,
            "5s",
            _EXPECTED_WINDOW_MS_5S,
            guard_partial_first_bar=False,
        )

    async def _pump(
        self,
        symbol: str,
        state: _SymbolState,
        source_factory: Callable[[IbkrClient, str], AsyncIterator[IbkrMinuteBar]],
        label: str,
        expected_window_ms: int,
        *,
        guard_partial_first_bar: bool,
    ) -> None:
        first_bar_seen = False
        try:
            client = self._resolve_client()
            # Hold off on flipping to 'streaming' until the first bar
            # actually arrives — a healthy reqRealTimeBars subscription
            # still takes up to the bar's window for that first emission,
            # and a sticky ``last_error`` from a prior failed task must
            # remain visible to the snapshot endpoint until then.
            async for bar in source_factory(client, symbol):
                # Partial-bar guard (Slice 4): a restart mid-window can let
                # the consolidator emit a first bar that covers only part of
                # the resolution's window. Drop it so the chart never shows
                # a ragged short candle and the persistence log never
                # records a malformed bar that would corrupt replay.
                if guard_partial_first_bar and not first_bar_seen:
                    first_bar_seen = True
                    if (bar.end_ms - bar.start_ms) != expected_window_ms:
                        logger.info(
                            "Dropping partial first %s bar at stream boundary",
                            label,
                            extra={
                                "symbol": symbol,
                                "resolution": label,
                                "start_ms": bar.start_ms,
                                "window_ms": bar.end_ms - bar.start_ms,
                                "expected_window_ms": expected_window_ms,
                                "action": "partial_first_bar_dropped",
                            },
                        )
                        # The pump still considers the stream live even though
                        # we didn't surface this bar — flip status anyway so
                        # the operator sees the green badge.
                        if state.status != "streaming":
                            state.status = "streaming"
                            state.last_error = None
                        continue

                self._persist_bar(symbol, label, bar)

                state.bars.append(bar)
                state.last_bar_ms = bar.start_ms
                if state.status != "streaming":
                    state.status = "streaming"
                    state.last_error = None
        except NotConnectedError as exc:
            state.status = "errored"
            state.last_error = f"broker not connected: {exc}"
            logger.warning("Live bar stream %s for %s: %s", label, symbol, exc)
        except asyncio.CancelledError:
            state.status = "idle"
            raise
        except Exception as exc:
            state.status = "errored"
            state.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Live bar stream %s for %s ended with %s", label, symbol, exc
            )

    def _persist_bar(self, symbol: str, resolution: str, bar: IbkrMinuteBar) -> None:
        """Write the bar through ``BarPersistence`` if attached.

        A non-monotonic regression raises out of ``append`` — log + swallow
        here rather than letting it kill the stream task. The persistence
        layer has already quarantined the day's JSONL; the operator's
        recovery path is to investigate that file.
        """
        if self._persistence is None:
            return
        try:
            self._persistence.append(symbol, resolution, bar)
        except BarPersistenceRegressionError as exc:
            logger.error(
                "BarPersistence quarantined %s/%s on non-monotonic bar: %s",
                symbol,
                resolution,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "BarPersistence append failed for %s/%s: %s",
                symbol,
                resolution,
                exc,
            )

    def _resolve_client(self) -> IbkrClient:
        """Fetch the public broker client. ``get_client`` itself raises
        ``NotConnectedError`` if the lifespan event never installed one;
        we add a connectivity check on top so a stale client also surfaces.
        """
        client = get_client()
        if not client.is_connected():
            raise NotConnectedError("public broker session not connected")
        return client


def _build_default_aggregator() -> LiveBarAggregator:
    """Construct the process-wide aggregator with persistence wired in.

    Reads ``IbkrSettings`` lazily so a test that monkeypatches the settings
    *before* importing this module still gets a fresh aggregator at boot.
    Falls back to a persistence-less aggregator if the settings cannot be
    resolved (e.g. unit tests that bypass the broker subsystem) — better
    to lose restart-replay than to fail import.
    """
    try:
        from pathlib import Path

        from app.broker.ibkr.config import get_settings

        settings = get_settings()
        persistence = BarPersistence(
            root=Path(settings.live_bars_root),
            retention_days=int(settings.live_bars_retention_days),
        )
        return LiveBarAggregator(persistence=persistence)
    except Exception as exc:
        logger.warning(
            "Falling back to persistence-less LiveBarAggregator: %s", exc
        )
        return LiveBarAggregator()


# Process-wide singleton — instantiated at import. The router imports this.
LIVE_BAR_AGGREGATOR = _build_default_aggregator()
