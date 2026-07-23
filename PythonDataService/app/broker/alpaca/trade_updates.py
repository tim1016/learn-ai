"""Owned ``trade_updates`` websocket consumer (Broker System v2, phase 2, S4).

Alpaca dispatches every order lifecycle transition (new / fill / partial_fill /
canceled / expired / rejected / replaced / …) over a trading websocket. This
module owns a **raw** ``websockets`` connection to that stream — deliberately
NOT alpaca-py's ``TradingStream``, which hides the raw frames the way the SDK's
REST models did (phase 1 rejected the SDK client for the same reason: we need
verbatim capture). The wire protocol we speak is exactly what alpaca-py's
``TradingStream`` speaks (it is the schema-drift authority), confirmed 2026-07:

- **Endpoint** (paper): ``wss://paper-api.alpaca.markets/stream``.
- **Encoding**: JSON (the ``/stream`` trading endpoint defaults to JSON; msgpack
  is opt-in via a header alpaca-py does not send for trading).
- **Auth frame**: ``{"action":"authenticate","data":{"key_id":…,"secret_key":…}}``.
- **Auth success**: ``{"stream":"authorization","data":{"status":"authorized"}}``.
- **Subscribe frame**: ``{"action":"listen","data":{"streams":["trade_updates"]}}``.
- **Event frame**: ``{"stream":"trade_updates","data":{"event":…,"execution_id":…,
  "order":{…},"timestamp":…,"price":…,"qty":…}}``.

The **exact** live-wire behavior is validated by the HITL gate (S7); S4's job is
correct protocol handling plus fully testable parsing / idempotency /
attribution driven by an **injected frame source**. The real socket is a thin
adapter (:func:`alpaca_socket_frames`) over the same core.

Per-frame flow (the order is load-bearing):

1. **Capture the raw bytes verbatim** to the capture journal (``STREAM`` family)
   BEFORE any parse. Secrets in the outbound auth frame are redacted by the
   shared journal redaction; inbound frames carry no secrets.
2. **Parse** JSON → the adapter's ``from_alpaca_trade_update`` →
   :class:`BrokerOrderEvent`.
3. **Dedup** on a stable per-event key (``execution_id`` for a fill, else
   ``order_id|event|timestamp``) under the temporal-rigor ``live_idempotent``
   rule: an exact redelivery is skipped + counted; an event for an
   already-**terminal** order is surfaced + counted (never silently dropped).
4. **Attribute + journal** via the Clerk: the wire ``client_order_id`` decides
   OWNED (namespace is ours → ``ORDER_EVENT``) vs UNEXPLAINED (foreign / absent
   → ``UNEXPLAINED_ORDER`` + counter). The S6 exposure hold is NOT wired here.

Reconnect: ``trade_updates`` has no replay-from-cursor, so on any disconnect the
consumer reconnects with bounded backoff and then performs a **REST
gap-reconcile** — ``GET /v2/orders`` for orders updated since the last-seen
event — feeding each through the same idempotent attribution path so a
re-observed event dedups.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.clerk import AlpacaClerk
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.config import BROKER_ID, AlpacaSettings, get_alpaca_settings
from app.broker.capture.journal import CaptureEndpoint, CaptureJournal, get_capture_journal
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)

# An injectable source of raw inbound frames (bytes or text, exactly as the
# socket delivered). A real socket wraps ``websockets``; tests inject a canned
# async iterator so no network is touched. Each connection attempt calls the
# factory to obtain a fresh iterator.
type FrameSource = Callable[[], AsyncIterator[bytes | str]]

# The two conversion/observation boundaries injected for deterministic tests.
type Clock = Callable[[], int]
type Backoff = Callable[[int], Awaitable[None]]

_STREAM_TRADE_UPDATES = "trade_updates"
_STREAM_AUTHORIZATION = "authorization"

# Statuses that mean the order can never transition again. An event whose stable
# key was already seen AND whose order is terminal is a stale redelivery: it is
# surfaced + counted (per live_idempotent), never silently dropped.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
)

_DEFAULT_MAX_BACKOFF_S = 30.0
_DEFAULT_BASE_BACKOFF_S = 1.0


def _now_ms() -> int:
    """Current instant as ``int64`` ms UTC (default observation clock)."""
    return int(datetime.now(UTC).timestamp() * 1000)


async def _default_backoff(attempt: int) -> None:
    """Exponential backoff capped at :data:`_DEFAULT_MAX_BACKOFF_S` seconds."""
    delay = min(_DEFAULT_BASE_BACKOFF_S * (2 ** max(0, attempt - 1)), _DEFAULT_MAX_BACKOFF_S)
    await asyncio.sleep(delay)


@dataclass
class TradeUpdateCounters:
    """Observable counters for the live consumer (surface, never silence).

    Every non-happy-path branch increments one of these so a live run can report
    what happened without it being a fatal event:

    - ``events_applied`` — distinct events attributed + journaled.
    - ``skipped_duplicate`` — exact redeliveries of an already-seen key.
    - ``stale_terminal`` — events for an already-terminal order (surfaced).
    - ``unexplained`` — events whose order this Clerk did not own.
    - ``parse_errors`` — frames that captured but would not parse.
    - ``reconnects`` — reconnect cycles performed.
    - ``gap_reconciled`` — orders pulled by a post-reconnect REST gap-fill.
    """

    events_applied: int = 0
    skipped_duplicate: int = 0
    stale_terminal: int = 0
    unexplained: int = 0
    parse_errors: int = 0
    reconnects: int = 0
    gap_reconciled: int = 0


@dataclass
class _SeenEvent:
    """The terminality of the order at the time a key was first accepted."""

    terminal: bool


def _event_key(
    event: str, occurred_at_ms: int, data: dict[str, Any], order: dict[str, Any]
) -> str:
    """A stable per-event dedup key.

    Alpaca fills/partial_fills carry a unique ``execution_id``; that is the best
    identity. Non-fill events (new / canceled / …) have no execution id, so key
    on ``order_id|event|occurred_at_ms`` — a tuple stable across a redelivery of
    the same lifecycle transition. The instant is the **canonical int64 ms**
    (temporal-rigor), never the raw wire string: the live-socket path and the
    REST gap-reconcile path (which reconstructs the instant from stored ms)
    must produce the *same* key for the same transition, and a raw ISO string
    with sub-millisecond precision would not round-trip identically.
    """
    execution_id = data.get("execution_id")
    if execution_id:
        return f"exec:{execution_id}"
    order_id = order.get("id", "")
    return f"{order_id}|{event}|{occurred_at_ms}"


def _is_terminal(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "")
    return status in _TERMINAL_STATUSES


class TradeUpdatesConsumer:
    """Owned raw ``trade_updates`` consumer with idempotency + attribution.

    The core is transport-agnostic: it is driven by a :type:`FrameSource` (an
    injectable async-iterator factory), a :type:`Clock`, and a :type:`Backoff`.
    The real socket is a thin adapter (:meth:`for_alpaca`) that supplies a
    ``websockets``-backed frame source; every non-socket concern (capture,
    parse, dedup, attribute, gap-reconcile) is identical either way and fully
    testable with no network.
    """

    def __init__(
        self,
        *,
        clerk: AlpacaClerk,
        read: BrokerReadPort,
        frame_source: FrameSource,
        journal: CaptureJournal | None = None,
        clock: Clock = _now_ms,
        backoff: Backoff = _default_backoff,
        max_reconnects: int | None = None,
    ) -> None:
        self._clerk = clerk
        self._read = read
        self._frame_source = frame_source
        self._journal = journal or get_capture_journal()
        self._clock = clock
        self._backoff = backoff
        # ``None`` = reconnect forever (production). A finite value bounds tests
        # so an injected finite frame source terminates deterministically.
        self._max_reconnects = max_reconnects
        self._counters = TradeUpdateCounters()
        self._seen: dict[str, _SeenEvent] = {}
        # Order ids that have reached a journaled terminal event. Any later event
        # for one is a stale re-observation of a finalized order — this is the
        # cross-path idempotency guard the REST gap-reconcile relies on: a fill's
        # socket ``exec:`` key cannot be reconstructed from a REST order, so
        # key-only dedup would re-journal a re-pulled terminal fill.
        self._terminal_orders: set[str] = set()
        # High-water mark of the last-seen event instant, for the REST
        # gap-reconcile after a reconnect (trade_updates has no cursor replay).
        self._last_event_ms: int | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def counters(self) -> TradeUpdateCounters:
        """The observable counters (read-only accessor for tests / health)."""
        return self._counters

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the consume loop as a background task (lifespan wiring)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.run(), name="alpaca-trade-updates")

    async def stop(self) -> None:
        """Cancel the consume task and wait for it to unwind cleanly."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        """Reconnect loop: consume until cancelled or the reconnect budget ends.

        Each cycle opens a fresh frame source, drains it, and — on a clean end
        of stream (a disconnect) — reconnects with backoff and REST
        gap-reconciles the orders missed while down. ``asyncio.CancelledError``
        propagates so a lifespan shutdown stops the loop immediately.
        """
        attempt = 0
        while True:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A frame-source failure is surfaced, then retried under backoff
                # — never a silent death of the live lifecycle feed.
                logger.warning(
                    "alpaca trade_updates stream errored; will reconnect",
                    extra={"action": "trade_updates_stream_error"},
                    exc_info=True,
                )

            attempt += 1
            self._counters.reconnects += 1
            if self._max_reconnects is not None and attempt > self._max_reconnects:
                logger.info(
                    "alpaca trade_updates reconnect budget exhausted; stopping",
                    extra={"action": "trade_updates_reconnect_budget_exhausted"},
                )
                return
            await self._backoff(attempt)
            await self._gap_reconcile()

    async def _consume_once(self) -> None:
        """Open one frame source and process frames until it is exhausted."""
        source = self._frame_source()
        async for frame in source:
            await self._handle_frame(frame)

    # ── Per-frame processing ─────────────────────────────────────────────────

    async def _handle_frame(self, frame: bytes | str) -> None:
        """Capture verbatim, parse, dedup, attribute — in that exact order."""
        raw = frame.encode("utf-8") if isinstance(frame, str) else frame
        # 1. Verbatim capture BEFORE parse: the on-disk record is exactly the
        #    wire bytes, so a parse failure still leaves an auditable frame.
        self._journal.record(
            broker=BROKER_ID,
            endpoint=CaptureEndpoint.STREAM,
            method="WS",
            params={"stream": _STREAM_TRADE_UPDATES},
            status=0,  # websocket frames carry no HTTP status.
            raw_body=raw,
        )

        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            self._counters.parse_errors += 1
            logger.warning(
                "alpaca trade_updates frame is not valid JSON",
                extra={"action": "trade_updates_parse_error"},
            )
            return
        if not isinstance(message, dict):
            self._counters.parse_errors += 1
            return

        stream = message.get("stream")
        if stream == _STREAM_AUTHORIZATION:
            self._log_authorization(message)
            return
        if stream != _STREAM_TRADE_UPDATES:
            # Control/keepalive frames (subscription acks, listening confirms)
            # are captured above; nothing to attribute.
            return
        await self._handle_trade_update(message.get("data") or {})

    def _log_authorization(self, message: dict[str, Any]) -> None:
        status = str((message.get("data") or {}).get("status") or "")
        if status == "authorized":
            logger.info(
                "alpaca trade_updates authorized",
                extra={"action": "trade_updates_authorized"},
            )
        else:
            # Not authorized — surfaced, not swallowed. The reconnect loop will
            # retry; a persistent auth failure is visible in logs.
            logger.warning(
                "alpaca trade_updates authorization not granted",
                extra={"action": "trade_updates_auth_denied", "status": status},
            )

    async def _handle_trade_update(self, data: dict[str, Any]) -> None:
        """Parse → dedup (live_idempotent) → attribute one trade-update event."""
        order = data.get("order") or {}
        try:
            event = adapter.from_alpaca_trade_update(data)
        except (KeyError, ValueError):
            self._counters.parse_errors += 1
            logger.warning(
                "alpaca trade_updates event would not map",
                extra={"action": "trade_updates_map_error"},
            )
            return

        order_id = str(order.get("id") or "")
        # Cross-path idempotency: once an order has a journaled terminal event,
        # any later event for it — an exact socket redelivery, or a REST
        # gap-reconcile re-pull that keys differently (a fill's socket ``exec:``
        # key cannot be reconstructed from a REST order) — is a stale
        # re-observation of a finalized order. Surface + count; never re-journal.
        if order_id and order_id in self._terminal_orders:
            self._counters.stale_terminal += 1
            logger.warning(
                "alpaca trade_updates redelivered event for a terminal order",
                extra={
                    "action": "trade_updates_stale_terminal",
                    "event": event.event_type,
                    "order_id": order_id,
                },
            )
            return

        key = _event_key(event.event_type, event.occurred_at_ms, data, order)
        seen = self._seen.get(key)
        if seen is not None:
            # Exact redelivery of a key we already accepted (live_idempotent).
            if seen.terminal:
                # The order was already terminal when first accepted; a repeat is
                # a stale redelivery of a finalized order. Surface + count — the
                # temporal-rigor rule forbids silently dropping it.
                self._counters.stale_terminal += 1
                logger.warning(
                    "alpaca trade_updates redelivered event for a terminal order",
                    extra={
                        "action": "trade_updates_stale_terminal",
                        "event": event.event_type,
                        "event_key": key,
                    },
                )
            else:
                self._counters.skipped_duplicate += 1
                logger.info(
                    "alpaca trade_updates idempotent skip of redelivered event",
                    extra={
                        "action": "trade_updates_skipped_duplicate",
                        "event": event.event_type,
                        "event_key": key,
                    },
                )
            return

        self._seen[key] = _SeenEvent(terminal=_is_terminal(order))
        # Track the last-seen instant for the post-reconnect gap-reconcile.
        if self._last_event_ms is None or event.occurred_at_ms > self._last_event_ms:
            self._last_event_ms = event.occurred_at_ms

        client_order_id = adapter.opt_str(order.get("client_order_id"))
        broker_order = adapter.from_alpaca_order(order) if order else None
        kind = await self._clerk.record_lifecycle_event(
            client_order_id=client_order_id,
            event=event,
            event_key=key,
            order=broker_order,
        )
        if kind is ClerkEntryKind.UNEXPLAINED_ORDER:
            self._counters.unexplained += 1
        else:
            self._counters.events_applied += 1
        # Mark the order finalized (owned or not) so a later re-observation of
        # this terminal order is recognized as stale regardless of its key.
        if order_id and _is_terminal(order):
            self._terminal_orders.add(order_id)

    # ── Reconnect gap-reconcile ──────────────────────────────────────────────

    async def _gap_reconcile(self) -> None:
        """Pull orders updated since the last-seen event and re-feed them.

        ``trade_updates`` cannot replay from a cursor, so after a reconnect the
        only way to catch events missed while down is a REST read. Each pulled
        order is fed through the same attribution/journal path as a synthetic
        lifecycle event; a re-observed event dedups on its stable key, so this
        is safe to run on every reconnect.
        """
        after_ms = self._last_event_ms
        try:
            orders = await self._read.list_orders(status="all", after_ms=after_ms)
        except Exception:
            # A gap-reconcile failure is surfaced, not fatal — the live stream
            # is already back; the next reconnect retries the gap-fill.
            logger.warning(
                "alpaca trade_updates gap-reconcile read failed",
                extra={"action": "trade_updates_gap_reconcile_error"},
                exc_info=True,
            )
            return

        for broker_order in orders:
            synthetic = _order_to_event_payload(broker_order)
            if synthetic is None:
                continue
            self._counters.gap_reconciled += 1
            await self._handle_trade_update(synthetic)

    # ── Real-socket adapter ──────────────────────────────────────────────────

    @classmethod
    def for_alpaca(
        cls,
        *,
        clerk: AlpacaClerk,
        read: BrokerReadPort,
        settings: AlpacaSettings | None = None,
        journal: CaptureJournal | None = None,
    ) -> TradeUpdatesConsumer:
        """Build a consumer backed by a real ``websockets`` frame source.

        The socket adapter authenticates and subscribes on each connect, then
        yields raw inbound frames; every downstream concern is the shared core.
        The auth frame's secrets are redacted by the capture journal (outbound
        frames are not captured here — only inbound frames flow through
        ``_handle_frame`` — so no key material is ever journaled).
        """
        resolved = settings or get_alpaca_settings()
        return cls(
            clerk=clerk,
            read=read,
            frame_source=lambda: alpaca_socket_frames(resolved),
            journal=journal,
        )


def _order_to_event_payload(broker_order: Any) -> dict[str, Any] | None:
    """Shape a ``BrokerOrder`` back into a ``trade_updates`` ``data`` payload.

    The gap-reconcile reads orders (not events), so it reconstructs the minimal
    ``data`` shape ``from_alpaca_trade_update`` consumes: the order's current
    ``status`` maps to a lifecycle ``event`` and its ``updated_at`` to the event
    ``timestamp``. Returns ``None`` when the status has no lifecycle event
    (e.g. an intermediate state), so nothing is fabricated.
    """
    event = _STATUS_TO_EVENT.get(str(broker_order.status))
    if event is None:
        return None
    timestamp_ms = broker_order.updated_at_ms or broker_order.submitted_at_ms
    if timestamp_ms is None:
        return None
    return {
        "event": event,
        "timestamp": _ms_to_rfc3339(timestamp_ms),
        "order": {
            "id": broker_order.order_id,
            "client_order_id": broker_order.client_order_id,
            "symbol": broker_order.symbol,
            "asset_class": broker_order.asset_class,
            "side": broker_order.side,
            "order_type": broker_order.order_type,
            "type": broker_order.order_type,
            "time_in_force": broker_order.time_in_force,
            "qty": broker_order.quantity,
            "filled_qty": broker_order.filled_quantity,
            "limit_price": broker_order.limit_price,
            "stop_price": broker_order.stop_price,
            "filled_avg_price": broker_order.filled_avg_price,
            "status": broker_order.status,
            "submitted_at": _opt_ms_to_rfc3339(broker_order.submitted_at_ms),
            "created_at": _opt_ms_to_rfc3339(broker_order.created_at_ms),
            "updated_at": _opt_ms_to_rfc3339(broker_order.updated_at_ms),
            "filled_at": _opt_ms_to_rfc3339(broker_order.filled_at_ms),
            "canceled_at": _opt_ms_to_rfc3339(broker_order.canceled_at_ms),
            "expired_at": _opt_ms_to_rfc3339(broker_order.expired_at_ms),
        },
    }


# Map a REST order status to the lifecycle event a gap-reconcile synthesizes.
# Only terminal / actionable statuses map — an intermediate status yields None.
_STATUS_TO_EVENT: dict[str, str] = {
    "new": "new",
    "accepted": "accepted",
    "partially_filled": "partial_fill",
    "filled": "fill",
    "canceled": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "done_for_day": "done_for_day",
    "replaced": "replaced",
}


def _ms_to_rfc3339(ms: int) -> str:
    """``int64`` ms UTC → RFC-3339 (for the synthetic gap-reconcile payload).

    The adapter re-parses this back to ms immediately; the round-trip keeps the
    synthetic payload byte-shaped like a real frame so it flows the same path.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _opt_ms_to_rfc3339(ms: int | None) -> str | None:
    return None if ms is None else _ms_to_rfc3339(ms)


async def alpaca_socket_frames(settings: AlpacaSettings) -> AsyncIterator[bytes | str]:
    """Connect, authenticate, subscribe, then yield raw inbound frames.

    A thin adapter over ``websockets`` — the ONLY place a real network socket is
    touched. Authenticates with ``{"action":"authenticate",…}`` and subscribes
    with ``{"action":"listen",…}`` (the alpaca-py TradingStream protocol, the
    schema-drift authority), then yields every inbound frame verbatim to the
    core. Imported lazily so the module (and its tests) load without the
    ``websockets`` dependency being import-time mandatory.
    """
    import websockets

    url = _stream_url(settings)
    async with websockets.connect(url) as socket:
        await socket.send(
            json.dumps(
                {
                    "action": "authenticate",
                    "data": {
                        "key_id": settings.api_key_id,
                        "secret_key": settings.api_secret_key,
                    },
                }
            )
        )
        await socket.send(
            json.dumps({"action": "listen", "data": {"streams": [_STREAM_TRADE_UPDATES]}})
        )
        async for frame in socket:
            yield frame


def _stream_url(settings: AlpacaSettings) -> str:
    """Derive the ``/stream`` websocket URL from the mode-derived base URL.

    The base URL is already mode-derived (paper vs live) and never independently
    configurable (config §7), so the ws URL cannot mismatch the mode: swap the
    ``https`` scheme for ``wss`` and append ``/stream``.
    """
    return settings.base_url.replace("https://", "wss://", 1).rstrip("/") + "/stream"


_consumer: TradeUpdatesConsumer | None = None


def get_trade_updates_consumer() -> TradeUpdatesConsumer | None:
    """Return the process-wide consumer, or ``None`` when not started.

    ``None`` means Alpaca is unconfigured (no keys) or the lifespan did not
    install it — the same "not configured" posture the Clerk uses.
    """
    return _consumer


def set_trade_updates_consumer(consumer: TradeUpdatesConsumer | None) -> None:
    """Install (or clear) the process-wide consumer — lifespan wiring."""
    global _consumer
    _consumer = consumer


def reset_trade_updates_consumer_for_testing() -> None:
    """Drop the process-wide consumer so a test starts clean."""
    global _consumer
    _consumer = None
