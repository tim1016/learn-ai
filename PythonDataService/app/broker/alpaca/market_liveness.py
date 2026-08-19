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
# Tolerance for a status event's source_timestamp_ms reading ahead of our
# own receipt clock (ordinary vendor/network clock skew) before it is
# refused as unverifiable rather than merely stale.
_MAX_FUTURE_SKEW_MS = 5_000

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
        self._store.mark_stream_disconnected(observed_at_ms=self._clock())
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
        self._store.mark_stream_disconnected(observed_at_ms=self._clock())
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
                # Disconnection alone already fails every symbol closed via
                # MarketLivenessStore.fact's `connected` check, so clearing
                # per-symbol evidence here would only ever discard it, never
                # protect anything. Alpaca's status stream has no
                # snapshot-on-subscribe and no REST fallback for current halt
                # state, so a HALTED record is the only proof a still-halted
                # symbol has once the socket reconnects; wiping it here would
                # silently report that symbol TRADABLE the instant any frame
                # (for any symbol) arrives on the new connection, before a
                # fresh transition ever confirms a resume. Evidence is
                # invalidated only at full consumer start/stop (see
                # ``start``/``stop``), never on an ordinary reconnect cycle.
                self._store.mark_stream_disconnected(observed_at_ms=self._clock())

            attempt += 1
            if self._max_reconnects is not None and attempt > self._max_reconnects:
                return
            await self._backoff(attempt)

    def handle_frame(self, frame: bytes | str) -> None:
        """Capture and map one raw status frame, ignoring it when malformed.

        A malformed frame is refused evidence for itself only — it does not
        wipe previously-established evidence for other symbols. Under the
        connection-watermark model (see the module docstring), absence of
        per-symbol evidence already means "not blocked", so treating a
        single bad frame as grounds to wipe the halted-symbol map would
        silently un-halt every symbol this connection cycle had legitimately
        proven halted. Nor does a reconnect invalidate that map (see
        ``_consume_statuses``) — only a genuine resume transition for that
        symbol does. Receiving a frame at all — good or bad — still proves
        the socket itself is alive.
        """
        self._store.mark_stream_connected(observed_at_ms=self._clock())
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
            logger.error(
                "alpaca market-status frame could not be captured; refusing its evidence",
                extra={"action": "market_liveness_status_capture_failed"},
            )
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "alpaca market-status frame is not valid JSON",
                extra={"action": "market_liveness_status_parse_error"},
            )
            return
        messages = payload if isinstance(payload, list) else [payload]
        if not all(isinstance(message, dict) for message in messages):
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
            logger.warning(
                "alpaca market-status message omitted its symbol",
                extra={"action": "market_liveness_status_symbol_missing"},
            )
            return
        source_timestamp_ms = _message_timestamp_ms(message)
        if source_timestamp_ms is None:
            # A missing or unparsable source time is unverifiable external
            # evidence — never accept the state transition. The symbol keeps
            # whatever state its last verified evidence proved (unaffected
            # by this message either way), so a corrupted resume cannot
            # lift a genuine halt.
            logger.warning(
                "alpaca market-status message has no verifiable source time; the transition was not applied",
                extra={"action": "market_liveness_status_timestamp_missing", "symbol": symbol},
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

        receipt_ms = self._clock()
        if source_timestamp_ms > receipt_ms + _MAX_FUTURE_SKEW_MS:
            if state == "TRADABLE":
                # source_timestamp_ms becomes the primary ordering key in
                # observe_symbol_status's freshness-ordering guard —
                # accepting a future-dated *resume* would make every
                # subsequent legitimate event for this symbol look "older"
                # and be silently discarded forever, pinning this corrupted
                # or clock-skewed resume in place and masking a real, later
                # halt. Refuse it before it can order anything.
                logger.warning(
                    "alpaca market-status resume is dated in the future beyond tolerance; the transition was not applied",
                    extra={
                        "action": "market_liveness_status_timestamp_future",
                        "symbol": symbol,
                        "source_timestamp_ms": source_timestamp_ms,
                        "receipt_ms": receipt_ms,
                    },
                )
                return
            # A future-dated HALT/unrecognized-status event is still
            # safety-relevant negative evidence — dropping it, unlike a
            # dropped resume, would leave the symbol exposed to whatever it
            # last resolved to (TRADABLE by default with no prior record).
            # Apply it fail-closed, but stamp it with the receipt clock
            # instead of the unverifiable claimed time: accepting the bogus
            # future value verbatim would poison the ordering key exactly
            # the way a future-dated resume would, permanently masking a
            # later legitimate resume for this symbol.
            logger.warning(
                "alpaca market-status message is dated in the future beyond tolerance; applying it fail-closed with the receipt time instead",
                extra={
                    "action": "market_liveness_status_timestamp_future_fail_closed",
                    "symbol": symbol,
                    "source_timestamp_ms": source_timestamp_ms,
                    "receipt_ms": receipt_ms,
                },
            )
            source_timestamp_ms = receipt_ms

        self._store.observe_symbol_status(
            SymbolTradingStatusEvidence(
                symbol=symbol,
                state=state,
                source=_STATUS_SOURCE,
                observed_at_ms=receipt_ms,
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
