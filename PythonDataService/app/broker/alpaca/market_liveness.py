"""Owned Alpaca evidence source for live market liveness (#1671).

The canonical calendar owns *scheduled* session phases. This consumer owns the
separate present-tense evidence path: it polls Alpaca's market-wide clock and
subscribes to the stock-data trading-status stream for symbol halt/resume
events. The status stream is transition-oriented, so a missing, malformed, or
disconnected source never implies tradability: the shared fact becomes
``UNKNOWN`` and all new exposure fails closed.

The core is driven by an injected frame source and clock. Tests therefore prove
the exact evidence mapping without opening a broker connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from app.broker.alpaca import adapter
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.capture.journal import CaptureEndpoint, CaptureJournal, get_capture_journal
from app.broker.contract.ports import BrokerReadPort
from app.schemas.market_liveness import SymbolTradingStatusEvidence
from app.services.market_liveness import MarketLivenessStore, get_market_liveness_store
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

type FrameSource = Callable[[], AsyncIterator[bytes | str]]
type Backoff = Callable[[int], Awaitable[None]]

_STATUS_SOURCE = "alpaca.stock_data.status"
_STATUS_STREAM = "market_statuses"
_STATUS_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
_CLOCK_POLL_INTERVAL_S = 1.0
_MAX_RECONNECT_BACKOFF_S = 30.0

_HALT_CODES = frozenset({"H", "2", "P"})
_RESUME_CODES = frozenset({"T", "3"})


async def _default_backoff(attempt: int) -> None:
    """Wait with bounded exponential reconnect backoff."""
    await asyncio.sleep(min(2 ** max(attempt - 1, 0), _MAX_RECONNECT_BACKOFF_S))


class AlpacaMarketLivenessConsumer:
    """Populate the shared liveness store from independently fresh sources."""

    def __init__(
        self,
        *,
        read: BrokerReadPort,
        frame_source: FrameSource,
        store: MarketLivenessStore | None = None,
        journal: CaptureJournal | None = None,
        clock: Clock = now_ms_utc,
        backoff: Backoff = _default_backoff,
        max_reconnects: int | None = None,
        clock_poll_interval_s: float = _CLOCK_POLL_INTERVAL_S,
    ) -> None:
        self._read = read
        self._frame_source = frame_source
        self._store = store or get_market_liveness_store()
        self._journal = journal or get_capture_journal()
        self._clock = clock
        self._backoff = backoff
        self._max_reconnects = max_reconnects
        self._clock_poll_interval_s = clock_poll_interval_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the source pair once for the application lifespan."""
        if self._task is not None and not self._task.done():
            return
        self._store.mark_clock_unavailable(
            observed_at_ms=self._clock(),
            reason="Live Alpaca market-liveness source is starting.",
        )
        self._store.clear_symbol_statuses()
        self._task = asyncio.create_task(self.run(), name="alpaca-market-liveness")

    async def stop(self) -> None:
        """Cancel background polling and invalidate evidence on shutdown."""
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        self._store.mark_clock_unavailable(
            observed_at_ms=self._clock(),
            reason="Live Alpaca market-liveness source stopped.",
        )
        self._store.clear_symbol_statuses()

    async def run(self) -> None:
        """Run clock polling and status-stream reconnects until cancelled."""
        clock_task = asyncio.create_task(self._poll_clock(), name="alpaca-market-clock")
        try:
            await self._consume_statuses()
        finally:
            clock_task.cancel()
            with suppress(asyncio.CancelledError):
                await clock_task

    async def refresh_clock(self) -> None:
        """Read one broker clock observation, replacing stale proof on errors."""
        try:
            clock = await self._read.get_clock_evidence()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._store.mark_clock_unavailable(
                observed_at_ms=self._clock(),
                reason="The live Alpaca market-clock request failed.",
            )
            logger.warning(
                "alpaca market clock request failed; new exposure is blocked",
                extra={"action": "market_liveness_clock_error"},
                exc_info=True,
            )
            return
        self._store.observe_clock(clock)

    async def _poll_clock(self) -> None:
        while True:
            await self.refresh_clock()
            await asyncio.sleep(self._clock_poll_interval_s)

    async def _consume_statuses(self) -> None:
        attempt = 0
        while True:
            self._store.clear_symbol_statuses()
            try:
                async for frame in self._frame_source():
                    self.handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "alpaca market-status stream errored; new exposure is blocked until reconnect",
                    extra={"action": "market_liveness_status_stream_error"},
                    exc_info=True,
                )
            finally:
                # A stream connection only proves events received during its own
                # epoch. Never carry a status across disconnect/reconnect.
                self._store.clear_symbol_statuses()

            attempt += 1
            if self._max_reconnects is not None and attempt > self._max_reconnects:
                return
            await self._backoff(attempt)

    def handle_frame(self, frame: bytes | str) -> None:
        """Capture and map one raw status frame, failing closed when malformed."""
        raw = frame.encode("utf-8") if isinstance(frame, str) else frame
        captured = self._journal.record(
            broker="alpaca",
            endpoint=CaptureEndpoint.STREAM,
            method="WS",
            params={"stream": _STATUS_STREAM},
            status=0,
            raw_body=raw,
        )
        if not captured:
            self._store.clear_symbol_statuses()
            logger.error(
                "alpaca market-status frame could not be captured; refusing its evidence",
                extra={"action": "market_liveness_status_capture_failed"},
            )
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            self._store.clear_symbol_statuses()
            logger.warning(
                "alpaca market-status frame is not valid JSON",
                extra={"action": "market_liveness_status_parse_error"},
            )
            return
        messages = payload if isinstance(payload, list) else [payload]
        if not all(isinstance(message, dict) for message in messages):
            self._store.clear_symbol_statuses()
            logger.warning(
                "alpaca market-status frame has an invalid message shape",
                extra={"action": "market_liveness_status_shape_error"},
            )
            return
        for message in messages:
            self._handle_status_message(message)

    def _handle_status_message(self, message: dict[str, Any]) -> None:
        if message.get("T") != "s":
            return
        symbol = str(message.get("S") or "").strip().upper()
        if not symbol:
            self._store.clear_symbol_statuses()
            logger.warning(
                "alpaca market-status message omitted its symbol",
                extra={"action": "market_liveness_status_symbol_missing"},
            )
            return
        status_code = str(message.get("sc") or "").upper()
        if status_code in _HALT_CODES:
            state = "HALTED"
            reason_code = "ALPACA_STATUS_HALT"
            reason = "Alpaca stock-data status reports the symbol halted or paused."
        elif status_code in _RESUME_CODES:
            state = "TRADABLE"
            reason_code = "ALPACA_STATUS_RESUME"
            reason = "Alpaca stock-data status reports the symbol trading."
        else:
            state = "UNKNOWN"
            reason_code = "ALPACA_STATUS_UNKNOWN"
            reason = f"Alpaca reported unrecognized trading status {status_code or 'missing'} for {symbol}."
        source_timestamp_ms = _message_timestamp_ms(message)
        self._store.observe_symbol_status(
            SymbolTradingStatusEvidence(
                symbol=symbol,
                state=state,
                source=_STATUS_SOURCE,
                observed_at_ms=self._clock(),
                source_timestamp_ms=source_timestamp_ms,
                reason_code=reason_code,
                reason=reason,
            )
        )

    @classmethod
    def for_alpaca(
        cls,
        *,
        read: BrokerReadPort,
        settings: AlpacaSettings | None = None,
        store: MarketLivenessStore | None = None,
        journal: CaptureJournal | None = None,
    ) -> AlpacaMarketLivenessConsumer:
        """Build the production consumer with Alpaca's raw data websocket."""
        resolved = settings or get_alpaca_settings()

        def frame_source() -> AsyncIterator[bytes | str]:
            return alpaca_market_status_frames(resolved)

        return cls(
            read=read,
            frame_source=frame_source,
            store=store,
            journal=journal,
        )


def _message_timestamp_ms(message: dict[str, Any]) -> int | None:
    timestamp = message.get("t")
    if timestamp is None:
        return None
    try:
        return adapter.rfc3339_to_ms(str(timestamp))
    except (TypeError, ValueError):
        return None


async def alpaca_market_status_frames(settings: AlpacaSettings) -> AsyncIterator[bytes | str]:
    """Authenticate and subscribe to raw Alpaca symbol trading-status events."""
    import websockets

    async with websockets.connect(_STATUS_WS_URL) as socket:
        await socket.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": settings.api_key_id,
                    "secret": settings.api_secret_key,
                }
            )
        )
        await socket.send(json.dumps({"action": "subscribe", "statuses": ["*"]}))
        async for frame in socket:
            yield frame


_consumer: AlpacaMarketLivenessConsumer | None = None


def get_market_liveness_consumer() -> AlpacaMarketLivenessConsumer | None:
    """Return the lifespan-owned source, if Alpaca has been configured."""
    return _consumer


def set_market_liveness_consumer(consumer: AlpacaMarketLivenessConsumer | None) -> None:
    """Install or clear the application-owned liveness source."""
    global _consumer
    _consumer = consumer


def reset_market_liveness_consumer_for_testing() -> None:
    """Drop the global consumer reference between isolated tests."""
    global _consumer
    _consumer = None
