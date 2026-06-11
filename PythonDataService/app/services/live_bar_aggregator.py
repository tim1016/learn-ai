"""Live 1-minute OHLCV ring buffer fed by IBKR ``reqRealTimeBars``.

Owns one async background task per symbol that consumes ``stream_minute_bars``
and appends closed 1-min bars to a bounded deque. The HTTP layer reads a
snapshot of the deque on each poll; nothing in the buffer is persisted.

Lifecycle:

* ``ensure_subscribed(symbol)`` — idempotent; first call starts the task,
  subsequent calls are no-ops while the task is alive.
* ``snapshot(symbol, since_ms=None)`` — returns the bars currently in the
  deque (optionally filtered to ``start_ms > since_ms``).
* A task that errors (broker disconnect, IBKRBarStreamError) records the
  cause on ``state.last_error`` and exits. The next ``ensure_subscribed``
  call restarts it; the operator's path is "fix the broker, hit refresh".

This module deliberately runs inside the FastAPI process and shares the
public broker session (client_id 42) via ``app.broker.ibkr.client.get_client``
— it does NOT spin up a second IBKR socket. Running the engine
(``host_daemon``) and this aggregator side-by-side gives two independent
``reqRealTimeBars`` subscriptions on the same contract: IBKR allows that.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from app.broker.ibkr.bars import stream_minute_bars, stream_raw_5s_bars
from app.broker.ibkr.client import IbkrClient, NotConnectedError, get_client
from app.broker.ibkr.models import IbkrMinuteBar

logger = logging.getLogger(__name__)

# 500 closed 1-min bars ≈ 8h 20m of RTH — covers a full session plus
# warmup tail. Plenty for the chart, bounded so the buffer can't grow.
_RING_BUFFER_SIZE = 500

# 4 000 raw 5-sec bars ≈ 5h 33m at one bar every 5 seconds. The 5-sec
# chart is for short-term, high-resolution context — half a session of
# tape is plenty, and the buffer would otherwise dominate process memory
# (4 000 × ~120 bytes/bar = ~470 kB per symbol, vs ~60 kB for 1-min).
_RING_BUFFER_SIZE_5S = 4_000


SubscriptionStatus = Literal["idle", "subscribing", "streaming", "errored"]


@dataclass
class _SymbolState:
    """Per-symbol subscription state."""

    bars: deque[IbkrMinuteBar] = field(default_factory=lambda: deque(maxlen=_RING_BUFFER_SIZE))
    task: asyncio.Task | None = None
    status: SubscriptionStatus = "idle"
    last_error: str | None = None
    last_bar_ms: int | None = None


class LiveBarAggregator:
    """Singleton-style aggregator owning per-symbol background streams.

    Construct it once at module import time; the FastAPI router holds a
    reference. The aggregator does not own the IBKR connection — it
    resolves the broker client via ``get_client()`` on demand so connect /
    disconnect lifecycle changes are observed naturally.
    """

    def __init__(self) -> None:
        self._states: dict[str, _SymbolState] = {}
        self._states_5s: dict[str, _SymbolState] = {}
        # Protect dict access from concurrent ensure_subscribed calls.
        self._lock = asyncio.Lock()

    def _key(self, symbol: str) -> str:
        return symbol.strip().upper()

    async def ensure_subscribed(self, symbol: str) -> _SymbolState:
        """Start the per-symbol 1-min stream task if not already running.

        Returns the symbol's state regardless of whether the task was
        already alive. Caller may inspect ``state.status`` and
        ``state.last_error`` to surface subscription health.
        """
        key = self._key(symbol)
        async with self._lock:
            state = self._states.setdefault(key, _SymbolState())
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    self._run_stream(key, state), name=f"live-bar-stream:{key}"
                )
                state.status = "subscribing"
                state.last_error = None
            return state

    async def ensure_subscribed_5s(self, symbol: str) -> _SymbolState:
        """Start the per-symbol raw 5-sec stream task if not already running.

        Independent of ``ensure_subscribed`` (the 1-min path) — they own
        separate buffers and separate ``reqRealTimeBars`` subscriptions.
        """
        key = self._key(symbol)
        async with self._lock:
            state = self._states_5s.setdefault(
                key, _SymbolState(bars=deque(maxlen=_RING_BUFFER_SIZE_5S))
            )
            if state.task is None or state.task.done():
                state.task = asyncio.create_task(
                    self._run_stream_5s(key, state),
                    name=f"live-bar-stream-5s:{key}",
                )
                state.status = "subscribing"
                state.last_error = None
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
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                logger.debug("Aggregator task ended on shutdown: %s", exc)

    async def _run_stream(self, symbol: str, state: _SymbolState) -> None:
        """Background task: pump ``stream_minute_bars`` into the deque."""
        await self._pump(symbol, state, stream_minute_bars, "1m")

    async def _run_stream_5s(self, symbol: str, state: _SymbolState) -> None:
        """Background task: pump ``stream_raw_5s_bars`` into the deque."""
        await self._pump(symbol, state, stream_raw_5s_bars, "5s")

    async def _pump(
        self,
        symbol: str,
        state: _SymbolState,
        source_factory,
        label: str,
    ) -> None:
        try:
            client = self._resolve_client()
            state.status = "streaming"
            async for bar in source_factory(client, symbol):
                state.bars.append(bar)
                state.last_bar_ms = bar.start_ms
        except NotConnectedError as exc:
            state.status = "errored"
            state.last_error = f"broker not connected: {exc}"
            logger.warning("Live bar stream %s for %s: %s", label, symbol, exc)
        except asyncio.CancelledError:
            state.status = "idle"
            raise
        except Exception as exc:  # noqa: BLE001
            state.status = "errored"
            state.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Live bar stream %s for %s ended with %s", label, symbol, exc
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


# Process-wide singleton — instantiated at import. The router imports this.
LIVE_BAR_AGGREGATOR = LiveBarAggregator()
