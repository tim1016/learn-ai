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
consumer reconnects with bounded backoff, re-subscribes, and then performs a
**REST gap-reconcile** — ``GET /v2/orders`` for recently closed orders — feeding
each through the same idempotent attribution path so a re-observed event dedups.

Execution-health freshness is event-ordered rather than wall-clock-expiring.
Alpaca supplies no custody heartbeat and a legitimately quiet account can have
no trade updates for hours, so elapsed silence cannot distinguish health from a
broken stream. The failure budget is therefore zero *received unusable frames*:
any capture, JSON, or event-mapping failure immediately supersedes prior usable
evidence and fails the submit gate closed. A later successfully mapped live
trade-update frame restores health. Control-frame silence changes neither state,
and frame failures stay inside the socket cycle to avoid reconnect thrash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.stream_health import ExecutionEvidenceHealth
from app.broker.alpaca.clerk.trade_evidence import (
    LegacyLifecycleRecorder,
    LegacyTradeUpdateEvidenceSink,
    TradeUpdateEvidenceSink,
)
from app.broker.alpaca.config import BROKER_ID, AlpacaSettings, get_alpaca_settings
from app.broker.alpaca.fault_injection import (
    FrameFaultKind,
    frame_for_fault,
    get_fault_injection_registry,
    injection_permitted,
)
from app.broker.capture.journal import CaptureEndpoint, CaptureJournal, get_capture_journal
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent
from app.broker.contract.ports import BrokerReadPort
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

# An injectable source of raw inbound frames (bytes or text, exactly as the
# socket delivered). A real socket wraps ``websockets``; tests inject a canned
# async iterator so no network is touched. Each connection attempt calls the
# factory to obtain a fresh iterator.
type FrameSource = Callable[[], AsyncIterator[bytes | str]]

# The two conversion/observation boundaries injected for deterministic tests.
type Backoff = Callable[[int], Awaitable[None]]

_STREAM_TRADE_UPDATES = "trade_updates"
_STREAM_AUTHORIZATION = "authorization"

# Statuses that mean the order can never transition again. An event whose stable
# key was already seen AND whose order is terminal is a stale redelivery: it is
# surfaced + counted (per live_idempotent), never silently dropped.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)

_DEFAULT_MAX_BACKOFF_S = 30.0
_DEFAULT_BASE_BACKOFF_S = 1.0

# Bounded page of recently closed orders re-pulled on each reconnect to recover
# terminal transitions missed while disconnected. Alpaca's order-list max is 500;
# a wider historical sweep is S6's reconciliation job.
_GAP_RECONCILE_LIMIT = 500
_ACTIVITY_RECOVERY_LIMIT = 100


_now_ms = now_ms_utc


async def _default_backoff(attempt: int) -> None:
    """Exponential backoff capped at :data:`_DEFAULT_MAX_BACKOFF_S` seconds."""
    max_exponent = max(
        0, math.ceil(math.log2(_DEFAULT_MAX_BACKOFF_S / _DEFAULT_BASE_BACKOFF_S))
    )
    exponent = min(max(0, attempt - 1), max_exponent)
    delay = min(_DEFAULT_BASE_BACKOFF_S * (2**exponent), _DEFAULT_MAX_BACKOFF_S)
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
    - ``capture_failures`` — frames refused because verbatim capture failed.
    - ``event_key_collisions`` — changed payloads that reused an event key.
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
    capture_failures: int = 0
    event_key_collisions: int = 0


@dataclass
class _SeenEvent:
    """The accepted payload fingerprint and terminality for one event key."""

    fingerprint: str
    terminal: bool


def _broker_order_fingerprint_fields(broker_order: BrokerOrder) -> dict[str, object]:
    """Return the one canonical broker-order shape used by fingerprints."""
    return {
        "order_id": broker_order.order_id,
        "client_order_id": broker_order.client_order_id,
        "symbol": broker_order.symbol,
        "side": broker_order.side,
        "order_type": broker_order.order_type,
        "time_in_force": broker_order.time_in_force,
        "quantity": broker_order.quantity,
        "filled_quantity": broker_order.filled_quantity,
        "limit_price": broker_order.limit_price,
        "stop_price": broker_order.stop_price,
        "filled_avg_price": broker_order.filled_avg_price,
        "status": broker_order.status,
        "submitted_at_ms": broker_order.submitted_at_ms,
        "created_at_ms": broker_order.created_at_ms,
        "updated_at_ms": broker_order.updated_at_ms,
        "filled_at_ms": broker_order.filled_at_ms,
        "canceled_at_ms": broker_order.canceled_at_ms,
        "expired_at_ms": broker_order.expired_at_ms,
    }


def _event_fingerprint(
    event: BrokerOrderEvent,
    order: dict[str, Any],
    broker_order: BrokerOrder | None,
) -> str:
    """Return a canonical fingerprint for exact-redelivery verification.

    An event key identifies a lifecycle slot, not an immutable payload: a
    corrected fill can reuse its ``execution_id`` and two observations can
    collapse to the same millisecond. Only equivalent ledger meaning is an
    idempotent redelivery. The parsed event fields are canonical
    (timestamps are int64 ms) and the broker order captures the rest of the
    state the Clerk journals.
    """
    payload = {
        "event": event.event_type,
        "occurred_at_ms": event.occurred_at_ms,
        "price": event.price,
        "quantity": event.quantity,
        "client_order_id": order.get("client_order_id"),
        "order": (
            _broker_order_fingerprint_fields(broker_order)
            if broker_order is not None
            else None
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _terminal_state_fingerprint(
    event: BrokerOrderEvent, broker_order: BrokerOrder | None
) -> str | None:
    """Identify the canonical terminal state shared by stream and REST views.

    A websocket fill describes the final execution slice while the REST order
    returned by gap reconciliation describes the aggregate fill. Those payloads
    are intentionally different even when they represent the same terminal
    transition, so the full event fingerprint cannot deduplicate them. This
    projection keeps the stable terminal ledger meaning while still allowing a
    corrected aggregate state to be journaled as a variant.
    """
    if broker_order is None:
        return None
    order_fields = _broker_order_fingerprint_fields(broker_order)
    for observation_field in (
        "submitted_at_ms",
        "created_at_ms",
        "updated_at_ms",
    ):
        order_fields.pop(observation_field)
    payload = {
        "event": event.event_type,
        "occurred_at_ms": event.occurred_at_ms,
        **order_fields,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _variant_event_key(event_key: str, fingerprint: str) -> str:
    """Give a corrected observation a stable, auditable distinct journal key."""
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"{event_key}:variant:{digest}"


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
        read: BrokerReadPort,
        frame_source: FrameSource,
        evidence_sink: TradeUpdateEvidenceSink | None = None,
        clerk: LegacyLifecycleRecorder | None = None,
        journal: CaptureJournal | None = None,
        clock: Clock = _now_ms,
        backoff: Backoff = _default_backoff,
        max_reconnects: int | None = None,
    ) -> None:
        if (evidence_sink is None) == (clerk is None):
            raise ValueError("provide exactly one of evidence_sink or clerk")
        self._evidence_sink = (
            evidence_sink
            if evidence_sink is not None
            else LegacyTradeUpdateEvidenceSink(clerk)
        )
        self._read = self._evidence_sink.guard_reconnect_read(read)
        self._frame_source = frame_source
        self._journal = journal or get_capture_journal()
        self._clock = clock
        self._backoff = backoff
        # ``None`` = reconnect forever (production). A finite value bounds tests
        # so an injected finite frame source terminates deterministically.
        self._max_reconnects = max_reconnects
        self._counters = TradeUpdateCounters()
        self._seen: dict[str, _SeenEvent] = {}
        # Canonical terminal states by order id. A REST gap-reconcile projects an
        # aggregate order instead of the websocket's final execution slice, so
        # this state projection is the cross-path idempotency guard. Full event
        # fingerprints still protect live redeliveries and corrections.
        self._terminal_orders: dict[str, set[str]] = {}
        self._task: asyncio.Task[None] | None = None
        # S4 (#1262) connection watermark: True from the first accepted frame
        # of a cycle until that source is exhausted or errors. The dual-health
        # submission gate reads it (with its change time) as the execution
        # channel's health fact.
        self._connected = False
        self._connection_changed_at_ms = clock()
        # Quiet-after-connect is healthy because Alpaca supplies no lifecycle
        # heartbeat. A received unusable frame flips this false until a later
        # live trade update maps successfully.
        self._evidence_health = ExecutionEvidenceHealth(
            healthy=True,
            observed_at_ms=clock(),
        )

    @property
    def counters(self) -> TradeUpdateCounters:
        """The observable counters (read-only accessor for tests / health)."""
        return self._counters

    @property
    def connected(self) -> bool:
        """True while a trade_updates source is live (S4 gate input)."""
        return self._connected

    @property
    def connection_changed_at_ms(self) -> int:
        """When the connection state last flipped, int64 ms UTC (P7 age)."""
        return self._connection_changed_at_ms

    @property
    def evidence_health(self) -> ExecutionEvidenceHealth:
        """Latest evidence-bearing frame outcome and int64-ms observation."""
        return self._evidence_health

    def _mark_connection(self, connected: bool) -> None:
        if self._connected != connected:
            self._connected = connected
            self._connection_changed_at_ms = self._clock()

    def _mark_evidence_health(self, healthy: bool) -> None:
        self._evidence_health = ExecutionEvidenceHealth(
            healthy=healthy,
            observed_at_ms=self._clock(),
        )

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
                await self._consume_once(reconcile_after_connect=attempt > 0)
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

    async def _consume_once(self, *, reconcile_after_connect: bool) -> None:
        """Open one source, re-subscribe, then process frames until exhaustion."""
        source = self._frame_source()
        try:
            first_frame = await anext(source)
        except StopAsyncIteration:
            return
        # ``anext`` establishes the next source before reconciliation starts.
        # For the real socket, it yields the auth acknowledgement only after
        # the server accepted the ``listen`` subscription; a reconnect is live
        # before the REST snapshot closes the previous gap.
        try:
            if reconcile_after_connect:
                await self._gap_reconcile()
            # Reconnect admission remains closed until the active authority has
            # reconciled the missing window. The first connection has no gap.
            self._mark_connection(True)
            await self._handle_frame(first_frame)
            async for frame in source:
                await self._handle_frame(frame)
        finally:
            self._mark_connection(False)

    # ── Per-frame processing ─────────────────────────────────────────────────

    async def _handle_frame(self, frame: bytes | str) -> None:
        """Capture verbatim, parse, dedup, attribute — in that exact order."""
        raw = frame.encode("utf-8") if isinstance(frame, str) else frame
        # 1. Verbatim capture BEFORE parse: the on-disk record is exactly the
        #    wire bytes, so a parse failure still leaves an auditable frame.
        captured = self._journal.record(
            broker=BROKER_ID,
            endpoint=CaptureEndpoint.STREAM,
            method="WS",
            params={"stream": _STREAM_TRADE_UPDATES},
            status=0,  # websocket frames carry no HTTP status.
            raw_body=raw,
        )
        if not captured:
            self._counters.capture_failures += 1
            self._mark_evidence_health(False)
            logger.error(
                "alpaca trade_updates frame could not be captured; refusing to derive lifecycle state",
                extra={"action": "trade_updates_capture_failed"},
            )
            return

        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            self._counters.parse_errors += 1
            self._mark_evidence_health(False)
            logger.warning(
                "alpaca trade_updates frame is not valid JSON",
                extra={"action": "trade_updates_parse_error"},
            )
            return
        if not isinstance(message, dict):
            self._counters.parse_errors += 1
            self._mark_evidence_health(False)
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

    async def _handle_trade_update(
        self, data: dict[str, Any], *, from_gap_reconcile: bool = False
    ) -> None:
        """Parse → dedup (live_idempotent) → attribute one trade-update event."""
        order = data.get("order") or {}
        try:
            event = (
                _from_gap_recovery_event(data)
                if from_gap_reconcile
                else adapter.from_alpaca_trade_update(data)
            )
            # Map the embedded order in the SAME guard, BEFORE any state mutation.
            # ``from_alpaca_trade_update`` only reads ``event``/``timestamp`` (and
            # the top-level execution slice), so a frame with a malformed ``order``
            # would pass it and then raise here — outside the guard it would poison
            # ``_seen`` and abort the drain. A bad order is a parse error: the frame
            # is already captured, the counter is incremented, the stream continues.
            broker_order = adapter.from_alpaca_order(order) if order else None
        except (KeyError, ValueError):
            self._counters.parse_errors += 1
            if not from_gap_reconcile:
                self._mark_evidence_health(False)
            logger.warning(
                "alpaca trade_updates event would not map",
                extra={"action": "trade_updates_map_error"},
            )
            return

        if not from_gap_reconcile:
            self._mark_evidence_health(True)

        order_id = str(order.get("id") or "")
        fingerprint = _event_fingerprint(event, order, broker_order)
        terminal_state = _terminal_state_fingerprint(event, broker_order)
        # Cross-path idempotency applies specifically to the synthetic REST
        # projection. A live correction must continue to the full fingerprint
        # check below even when the order is already terminal.
        if (
            from_gap_reconcile
            and order_id
            and terminal_state is not None
            and terminal_state in self._terminal_orders.get(order_id, set())
        ):
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
            if seen.fingerprint != fingerprint:
                self._counters.event_key_collisions += 1
                key = _variant_event_key(key, fingerprint)
                variant_seen = self._seen.get(key)
                if variant_seen is not None:
                    self._counters.skipped_duplicate += 1
                    return
                logger.warning(
                    "alpaca trade_updates reused an event key with a changed payload",
                    extra={
                        "action": "trade_updates_event_key_collision",
                        "event_key": key,
                        "order_id": order_id,
                    },
                )
            else:
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

        client_order_id = adapter.opt_str(order.get("client_order_id"))
        kind = await self._evidence_sink.record_lifecycle_event(
            client_order_id=client_order_id,
            event=event,
            event_key=key,
            order=broker_order,
            recovery_source="closed_orders_window" if from_gap_reconcile else None,
            recovery_window_limit=_GAP_RECONCILE_LIMIT if from_gap_reconcile else None,
        )
        # Only a durable Clerk append earns idempotency state. If the Clerk
        # raises, the reconnect loop can retry the still-unseen frame.
        self._seen[key] = _SeenEvent(
            fingerprint=fingerprint, terminal=_is_terminal(order)
        )
        if kind is ClerkEntryKind.UNEXPLAINED_ORDER:
            self._counters.unexplained += 1
        else:
            self._counters.events_applied += 1
        # Mark the order finalized (owned or not) so a later re-observation of
        # this terminal order is recognized as stale regardless of its key.
        if order_id and _is_terminal(order) and terminal_state is not None:
            self._terminal_orders.setdefault(order_id, set()).add(terminal_state)

    # ── Reconnect gap-reconcile ──────────────────────────────────────────────

    async def _gap_reconcile(self) -> None:
        """Re-pull recently CLOSED orders and re-feed them idempotently.

        ``trade_updates`` has no cursor replay, and Alpaca's order-list ``after``
        filter keys on order *submission* time — not the transition time we care
        about — so it cannot select "orders that changed while we were down"
        (an order submitted before its last-seen event but filled during the gap
        is silently excluded). Instead, pull a bounded page of recently closed
        (terminal) orders and re-feed each through the same path: an order whose
        terminal event we missed is recovered, and one we already saw is absorbed
        by the terminal-order guard (:attr:`_terminal_orders`).

        Only terminal orders are pulled. An order still open missed no terminal
        transition, and re-feeding it could double-journal a non-terminal event
        whose synthetic timestamp differs from the socket's. A full historical
        sweep beyond the bounded page is S6's reconciliation job.
        """
        # SQLite performs its authoritative order/position reconciliation here;
        # legacy implements this hook as a no-op and keeps the bounded recovery
        # below. A failure propagates so ``connected`` and admission stay closed.
        await self._evidence_sink.reconcile_gap()
        await self._reconcile_activity_window()
        try:
            orders = await self._read.list_orders(
                status="closed", limit=_GAP_RECONCILE_LIMIT
            )
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
            await self._handle_trade_update(synthetic, from_gap_reconcile=True)

    async def _reconcile_activity_window(self) -> None:
        """Persist a bounded, cursor-derived Alpaca account-activity recovery receipt.

        This is intentionally separate from the synthetic order lifecycle event:
        Alpaca activities are account evidence and do not claim IBKR callback
        equivalence.  The cursor is reconstructed from the durable Clerk ledger
        on every reconnect, so a process restart cannot forget the gap boundary.
        """

        try:
            self._counters.gap_reconciled += (
                await self._evidence_sink.recover_activity_window(
                    read=self._read,
                    limit=_ACTIVITY_RECOVERY_LIMIT,
                )
            )
        except Exception:
            logger.warning(
                "alpaca trade_updates account-activity recovery failed",
                extra={"action": "trade_updates_activity_recovery_error"},
                exc_info=True,
            )

    # ── Real-socket adapter ──────────────────────────────────────────────────

    @classmethod
    def for_alpaca(
        cls,
        *,
        read: BrokerReadPort,
        evidence_sink: TradeUpdateEvidenceSink | None = None,
        clerk: LegacyLifecycleRecorder | None = None,
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

        def _socket_source() -> AsyncIterator[bytes | str]:
            return alpaca_socket_frames(resolved)

        def _injected_source() -> AsyncIterator[bytes | str]:
            return _inject_frame_faults(_socket_source())

        # Dev-only fault-injection seam (PRD #1354): wrap the real socket source
        # so armed frame faults interleave through the SAME consumer path. The
        # wrap is applied ONLY when the seam is permitted (off by default,
        # paper-only), so a normal run's hot path has zero injection code.
        frame_source: FrameSource = _injected_source if injection_permitted() else _socket_source

        return cls(
            read=read,
            frame_source=frame_source,
            evidence_sink=evidence_sink,
            clerk=clerk,
            journal=journal,
        )


def _order_to_event_payload(broker_order: BrokerOrder) -> dict[str, Any] | None:
    """Shape a ``BrokerOrder`` into a canonical REST-recovery event payload.

    The gap-reconcile reads aggregate orders, not execution events. Its
    ``timestamp_ms`` is canonical boundary data and fill fields remain clearly
    labelled as cumulative recovery by the selected evidence sink. It never
    fabricates an execution ID. Returns ``None`` when the status has no
    lifecycle event (e.g. an intermediate state).
    """
    event = _STATUS_TO_EVENT.get(str(broker_order.status))
    if event is None:
        return None
    timestamp_ms = _lifecycle_timestamp_ms(broker_order, event)
    if timestamp_ms is None:
        return None
    payload: dict[str, Any] = {
        "event": event,
        "timestamp_ms": timestamp_ms,
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
    if event in {"fill", "partial_fill"}:
        payload["price"] = broker_order.filled_avg_price
        payload["qty"] = broker_order.filled_quantity
    return payload


def _from_gap_recovery_event(payload: dict[str, Any]) -> BrokerOrderEvent:
    """Map a synthetic REST order snapshot without impersonating a websocket fill."""
    event_type = str(payload["event"])
    if event_type not in adapter.ALPACA_TRADE_UPDATE_EVENTS:
        raise ValueError(f"Unrecognized Alpaca recovery lifecycle event {event_type!r}.")
    execution_id = adapter.opt_str(payload.get("execution_id"))
    return BrokerOrderEvent(
        event_type=event_type,
        occurred_at_ms=adapter.trade_update_occurred_at_ms(payload),
        price=adapter.opt_float(payload.get("price")),
        quantity=adapter.opt_float(payload.get("qty")),
        execution_id=(execution_id.strip() or None) if execution_id is not None else None,
    )


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


def _lifecycle_timestamp_ms(broker_order: Any, event: str) -> int | None:
    """Use the vendor's transition instant before falling back to ``updated``."""
    event_timestamp = {
        "fill": broker_order.filled_at_ms,
        "canceled": broker_order.canceled_at_ms,
        "expired": broker_order.expired_at_ms,
    }.get(event)
    return event_timestamp or broker_order.updated_at_ms or broker_order.submitted_at_ms


def _ms_to_rfc3339(ms: int) -> str:
    """``int64`` ms UTC → RFC-3339 (for the synthetic gap-reconcile payload).

    The adapter re-parses this back to ms immediately; the round-trip keeps the
    synthetic payload byte-shaped like a real frame so it flows the same path.
    """
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def _opt_ms_to_rfc3339(ms: int | None) -> str | None:
    return None if ms is None else _ms_to_rfc3339(ms)


async def _inject_frame_faults(
    source: AsyncIterator[bytes | str],
) -> AsyncIterator[bytes | str]:
    """Interleave armed dev-only fault frames into a real inbound frame stream.

    Wraps a real frame source (PRD #1354). After each real frame it drains any
    armed frame faults and yields the crafted frames, so they thread through the
    SAME ``_handle_frame`` path (capture → parse → live_idempotent dedup →
    attribution → journal) as socket frames. The last real ``trade_updates``
    frame is remembered so ``REDELIVER_LAST`` re-emits it verbatim (proving
    idempotent absorption). Draining is inert when the seam is not permitted, so
    a mis-wired wrap can never fabricate a frame off-paper.

    Injection is triggered by real inbound frames; the live socket always
    delivers frames (auth/listen acks, keepalives), so an armed fault fires on
    the next frame rather than requiring a bot execution.
    """
    registry = get_fault_injection_registry()
    last_trade_update: str | None = None

    def _drain() -> tuple[list[str], bool]:
        frames: list[str] = []
        disconnect = False
        for fault in registry.drain_frame_faults():
            if fault.kind == FrameFaultKind.DISCONNECT:
                logger.warning(
                    "fault injection: trade_updates connection dropped",
                    extra={"action": "frame_fault_disconnect", "kind": fault.kind},
                )
                disconnect = True
                break
            frame = frame_for_fault(fault, last_frame=last_trade_update)
            if frame is None:
                logger.warning(
                    "fault injection: nothing to redeliver (no prior trade_updates frame)",
                    extra={"action": "frame_fault_redeliver_empty", "kind": fault.kind},
                )
                continue
            logger.warning(
                "alpaca frame fault injected (dev seam)",
                extra={"action": "frame_fault_injected", "kind": fault.kind},
            )
            frames.append(frame)
        return frames, disconnect

    async for frame in source:
        yield frame
        text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
        try:
            message = json.loads(text)
        except (ValueError, TypeError):
            message = None
        if isinstance(message, dict) and message.get("stream") == _STREAM_TRADE_UPDATES:
            last_trade_update = text
        injected_frames, disconnect = _drain()
        for injected in injected_frames:
            yield injected
        if disconnect:
            raise ConnectionError("injected trade_updates disconnect")


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
        auth_frame = await socket.recv()
        try:
            authorization = json.loads(auth_frame)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Alpaca sent an invalid authorization response") from exc
        if (
            not isinstance(authorization, dict)
            or authorization.get("stream") != _STREAM_AUTHORIZATION
            or (authorization.get("data") or {}).get("status") != "authorized"
        ):
            raise RuntimeError("Alpaca did not authorize the trade_updates stream")
        await socket.send(
            json.dumps({"action": "listen", "data": {"streams": [_STREAM_TRADE_UPDATES]}})
        )
        # Preserve the inbound authorization frame in the same capture path as
        # every later socket frame. The listen was sent only after authorization
        # and before this first yield, so reconnect gap-reconcile runs live.
        yield auth_frame
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
