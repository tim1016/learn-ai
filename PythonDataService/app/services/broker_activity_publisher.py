"""Per-instance broker-activity publisher (ADR 0014 §4).

The publisher is the stateful, data-plane-owned orchestrator. It:

1. Consumes the existing ``stream_order_events`` from the IBKR client.
2. Filters events by ``bot_order_namespace`` (the per-instance scope).
3. Reads the engine's ``LiveStateEnvelope`` sidecar to assemble an
   ``EngineIntent`` per matched event.
4. Calls the pure reconciler (``author_row_from_event``) to produce a
   ``BrokerActivityRow``.
5. Appends the row to the ``broker_activity.jsonl`` WAL.
6. Fans the row out to all SSE subscribers for that instance.

Authoring itself is pure (lives in ``broker_activity_reconciler``); this
module holds the state — subscriber queues, dedupe cache, WAL handle, the
background task. The ``BrokerActivityPublisherRegistry`` provides the
data-plane-singleton lifecycle (one publisher per ``strategy_instance_id``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.schemas.broker_activity import (
    BrokerActivityRow,
    ReconciliationTimingPolicy,
    SizingProvenance,
)
from app.services.broker_activity_reconciler import (
    EngineIntent,
    ReconciliationContext,
    UnauthorableEventError,
    author_row_from_event,
    match_identity,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    stable_broker_activity_wal_path,
)

logger = logging.getLogger(__name__)


# Type alias for the per-publisher event source — the registry injects
# this so tests can pass a synthetic AsyncIterator instead of standing up
# a real IBKR client. Production wiring is
# ``functools.partial(stream_order_events, client)``.
EventSourceFactory = Callable[[], AsyncIterator[IbkrOrderEvent]]

# Subscriber queue size. Each SSE client gets one; if a client falls
# behind by this many rows we drop the connection rather than buffer
# unboundedly. 256 covers a slow client + a fast bursty publisher.
_SUBSCRIBER_QUEUE_SIZE = 256


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


class BrokerActivityPublisher:
    """Per-strategy-instance background task + subscriber pub-sub.

    Lifecycle:

    - ``start()`` — spawn the background task.
    - ``subscribe()`` — async context manager yielding a queue the
      caller drains; auto-unsubscribes on exit.
    - ``stop()`` — cancel the background task, drain queues, close WAL.
    """

    def __init__(
        self,
        *,
        strategy_instance_id: str,
        bot_order_namespace: str,
        run_dir: Path,
        artifacts_root: Path,
        timing_policy: ReconciliationTimingPolicy,
        event_source_factory: EventSourceFactory,
    ) -> None:
        self._strategy_instance_id = strategy_instance_id
        self._bot_order_namespace = bot_order_namespace
        self._timing_policy = timing_policy
        self._event_source_factory = event_source_factory
        self._wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
        self._sidecar = LiveStateSidecarRepo(
            stable_live_state_path(artifacts_root, strategy_instance_id)
        )

        self._subscribers: set[asyncio.Queue[BrokerActivityRow | None]] = set()
        self._seen_exec_ids: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # On cold start, seed the dedupe set from the WAL so we don't
        # re-author a row IBKR redelivers right after the publisher
        # restarts.
        for row in self._wal.read_all():
            if row.exec_id:
                self._seen_exec_ids.add(row.exec_id)

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background consumer task. Idempotent — calling
        twice has no effect (the second call is a no-op)."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"broker-activity-publisher:{self._strategy_instance_id}"
        )

    async def stop(self) -> None:
        """Cancel the background task and signal all subscribers to drain.

        Each subscriber's queue receives a ``None`` sentinel; subscribers
        loop on ``get()`` and treat ``None`` as end-of-stream."""
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for q in self._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # The subscriber is already behind — they'll see the
                # cancellation when they next try to read.
                pass

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── subscriber pub-sub ────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[BrokerActivityRow | None]:
        """Register a new subscriber and return their queue.

        Caller is responsible for ``unsubscribe()``-ing — typically via a
        ``try / finally`` in the SSE endpoint. The queue receives every
        row authored after the subscription begins, plus a single
        ``None`` sentinel when the publisher stops.
        """
        q: asyncio.Queue[BrokerActivityRow | None] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_SIZE
        )
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[BrokerActivityRow | None]) -> None:
        """Drop the subscriber. Idempotent."""
        self._subscribers.discard(q)

    def backfill(
        self, *, after_seq: int = 0, limit: int | None = None
    ) -> list[BrokerActivityRow]:
        """Synchronous WAL read for REST backfill — returns rows with
        ``seq > after_seq``, capped at ``limit``.

        Lets a freshly-connected cockpit client fetch history without
        racing the SSE channel; the client passes the highest seq it
        has, gets the next page, and switches to SSE when ``next_seq``
        is ``None`` (drained)."""
        return self._wal.read_from(after_seq=after_seq, limit=limit)

    def last_persisted_seq(self) -> int:
        return self._wal.last_seq()

    # ── background loop ──────────────────────────────────────────

    async def _run(self) -> None:
        """Drive the event source until cancelled or the source ends."""
        try:
            async for event in self._event_source_factory():
                if self._stopped.is_set():
                    break
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Per the truthfulness contract: never silently swallow.
            # The exception is logged with full context; the task ends
            # and is_running flips to False so health checks notice.
            logger.exception(
                "broker-activity publisher crashed",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "bot_order_namespace": self._bot_order_namespace,
                },
            )
            raise

    async def _handle_event(self, event: IbkrOrderEvent) -> None:
        """Author at most one row from one event; fan out to subscribers."""
        # Filter: only events bearing OUR namespace OR foreign events
        # (no namespace match) get authored. Intermediate status events
        # for OUR orders (Submitted, PreSubmitted) are skipped here.
        intent_id = match_identity(
            event,
            submitted_orders=self._read_submitted_orders(),
            bot_order_namespace=self._bot_order_namespace,
        )
        if intent_id is None:
            # Foreign event. Only author for fills / cancels / errors —
            # not for status transitions on someone else's order.
            if event.event_type not in ("fill", "cancel", "error"):
                return
        else:
            # Owned event. Skip intermediate status (Submitted /
            # PreSubmitted) — those don't produce rows in the activity
            # stream, only fills / cancels / rejections do.
            if event.event_type == "status" and (event.status or "") not in (
                "Cancelled",
                "ApiCancelled",
            ):
                return

        intent = self._build_engine_intent(intent_id) if intent_id else None

        seq = self._wal.allocate_seq()
        ctx = ReconciliationContext(
            seq=seq,
            ts_ms=_now_ms(),
            bot_order_namespace=self._bot_order_namespace,
            timing_policy=self._timing_policy,
            previously_seen_exec_ids=frozenset(self._seen_exec_ids),
            reconnect_recovery_active=False,  # wired in slice 3
        )

        try:
            row = author_row_from_event(event=event, intent=intent, ctx=ctx)
        except UnauthorableEventError:
            # The event is missing required fields the truthfulness
            # contract demands. Log and skip — this is a publisher-input
            # bug (events.py upstream should always populate
            # symbol/side/order_type), surface via logs, do NOT author
            # a fake row.
            logger.exception(
                "skipping unauthorable broker event",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "order_id": event.order_id,
                    "exec_id": event.exec_id,
                },
            )
            return

        self._wal.append_row(row)
        if row.exec_id:
            self._seen_exec_ids.add(row.exec_id)
        self._update_envelope_cursor(row.seq)
        self._broadcast(row)

    # ── helpers (engine state + broadcast + envelope) ─────────────

    def _read_submitted_orders(self) -> dict[str, dict]:
        envelope = self._sidecar.read()
        if envelope is None:
            return {}
        return dict(envelope.submitted_orders)

    def _build_engine_intent(self, intent_id: str) -> EngineIntent | None:
        envelope = self._sidecar.read()
        if envelope is None:
            return None
        submitted = envelope.submitted_orders.get(intent_id, {})
        # Sizing provenance comes from the per-trade sizing-resolution
        # ring buffer; the publisher matches by intent_id.
        sizing_provenance: SizingProvenance | None = None
        for entry in envelope.sizing_resolutions:
            if entry.get("intent_id") == intent_id:
                sizing_provenance = SizingProvenance.model_validate(
                    {
                        k: entry.get(k)
                        for k in (
                            "policy",
                            "requested_qty",
                            "reference_price_decimal_str",
                            "provenance",
                            "surface",
                            "skip_reason",
                        )
                        if k in entry
                    }
                )
                break
        return EngineIntent(
            intent_id=intent_id,
            mutation_attempt_id=submitted.get("mutation_attempt_id"),
            requested_qty=_safe_float(submitted.get("requested_qty")),
            requested_price=_safe_float(submitted.get("requested_price")),
            intent_created_ms=_safe_int(submitted.get("intent_created_ms")),
            dispatched_ms=_safe_int(submitted.get("dispatched_ms")),
            acked_ms=_safe_int(submitted.get("acked_ms")),
            sizing_provenance=sizing_provenance,
        )

    def _update_envelope_cursor(self, seq: int) -> None:
        envelope = self._sidecar.read()
        if envelope is None:
            return
        if seq <= envelope.last_broker_activity_wal_seq:
            return
        # NOTE: this is a best-effort cursor update — if the engine is
        # currently writing the envelope, we may race. The envelope's
        # atomic-write contract means at worst we lose this cursor
        # update; the publisher re-derives state from the WAL on
        # cold-start so no data is lost.
        try:
            self._sidecar.write(
                envelope.model_copy(update={"last_broker_activity_wal_seq": seq})
            )
        except (FileNotFoundError, OSError):
            # Sidecar may be temporarily missing during engine restart;
            # cursor will catch up on the next event.
            logger.debug(
                "couldn't update broker-activity cursor on sidecar",
                extra={"strategy_instance_id": self._strategy_instance_id},
            )

    def _broadcast(self, row: BrokerActivityRow) -> None:
        """Push the row to every subscriber. A full queue means the
        subscriber is too slow — drop the row for that subscriber and
        send them ``None`` so the SSE endpoint can close their
        connection. Other subscribers are unaffected."""
        dead: list[asyncio.Queue[BrokerActivityRow | None]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(row)
            except asyncio.QueueFull:
                logger.warning(
                    "dropping slow broker-activity subscriber",
                    extra={"strategy_instance_id": self._strategy_instance_id},
                )
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ── Data-plane-singleton registry ──────────────────────────────────────


class BrokerActivityPublisherRegistry:
    """Per-data-plane registry of running publishers, keyed by
    ``strategy_instance_id``.

    Lifecycle hook: when an instance is deployed, ``register`` creates
    and starts a publisher. When the instance stops or the data plane
    shuts down, ``unregister`` (or ``stop_all``) shuts it down.
    """

    def __init__(self) -> None:
        self._by_instance: dict[str, BrokerActivityPublisher] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        publisher: BrokerActivityPublisher,
        *,
        strategy_instance_id: str,
    ) -> BrokerActivityPublisher:
        """Add the publisher to the registry and start it. If an entry
        for ``strategy_instance_id`` already exists, the existing
        publisher is stopped first (the new one supersedes it).
        """
        async with self._lock:
            existing = self._by_instance.get(strategy_instance_id)
            if existing is not None and existing is not publisher:
                await existing.stop()
            self._by_instance[strategy_instance_id] = publisher
        publisher.start()
        return publisher

    def get(self, strategy_instance_id: str) -> BrokerActivityPublisher | None:
        return self._by_instance.get(strategy_instance_id)

    async def unregister(self, strategy_instance_id: str) -> None:
        async with self._lock:
            publisher = self._by_instance.pop(strategy_instance_id, None)
        if publisher is not None:
            await publisher.stop()

    async def stop_all(self) -> None:
        """Shutdown hook — stop every running publisher. The registry is
        left empty; the data plane's FastAPI lifespan calls this from
        the shutdown handler."""
        async with self._lock:
            publishers = list(self._by_instance.values())
            self._by_instance.clear()
        for p in publishers:
            await p.stop()

    def instances(self) -> tuple[str, ...]:
        return tuple(self._by_instance.keys())


# Module-level singleton — one registry per data-plane process. Imported
# by the lifecycle wiring in ``live_instances`` and by the SSE/REST
# endpoint module. Tests construct fresh registries; production reads
# this one.
_REGISTRY = BrokerActivityPublisherRegistry()


def get_publisher_registry() -> BrokerActivityPublisherRegistry:
    return _REGISTRY


__all__ = [
    "BrokerActivityPublisher",
    "BrokerActivityPublisherRegistry",
    "EventSourceFactory",
    "get_publisher_registry",
]
