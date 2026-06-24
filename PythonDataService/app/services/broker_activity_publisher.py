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

Slice 3 (ADR 0011 amendment): the publisher additionally exposes a
``sweep_reconnect_recovery()`` method the auto-reconnect monitor calls on
every successful reconnect. The sweep fetches the day's executions via the
injected ``recovery_source_factory`` (production wiring runs
``IB.reqExecutionsAsync``), dedupes against the publisher's running
``_seen_exec_ids`` set, and authors any unseen execution with
``ReconciliationContext.reconnect_recovery_active=True`` so the
``reconnect_recovery`` template fires. While the sweep is active the
registry surfaces ``any_recovery_active() == True`` and
``place_paper_order`` refuses new submissions — the broker connection is
busy replaying history and a new order would race the sweep.

PR 6 (operator-notice §11): a periodic sweep loop (``_periodic_sweep_loop``)
runs immediately on start, then every ``_sweep_interval_ms`` milliseconds.
The sweep fetches via the same ``recovery_source_factory``, filters by
``_sweep_lookback_ms`` (client-side, keyed on ``exec_time_ms``), and emits
a ``reconciliation.discovered_execution_not_in_engine_state`` critical
``OperatorIncident`` via ``IncidentStore`` when the sweep finds a fill that
is (a) not in the dedupe set and (b) carries no engine intent.  The engine
remains authoritative; the publisher never silently corrects cockpit state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.intent_ledger import (
    _UNRESOLVED_STATUSES,
    LedgerProjection,
)
from app.engine.live.intent_ledger import (
    fold as fold_intent_events,
)
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
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
    author_pending_row,
    author_row_from_event,
    match_identity,
    parse_order_ref,
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


# Type alias for the per-publisher reconnect-recovery source. Production
# wiring fetches IBKR executions via ``IB.reqExecutionsAsync`` and adapts
# each ``Fill`` into an ``IbkrOrderEvent``; tests inject a synthetic
# coroutine returning a fixed list. The factory is invoked once per
# successful reconnect and its result is processed to completion before
# the submission halt lifts.
RecoverySourceFactory = Callable[[], Awaitable[list[IbkrOrderEvent]]]

# Subscriber queue size. Each SSE client gets one; if a client falls
# behind by this many rows we drop the connection rather than buffer
# unboundedly. 256 covers a slow client + a fast bursty publisher.
_SUBSCRIBER_QUEUE_SIZE = 256

# PR 6 — periodic sweep cadence and lookback window.
# ``DEFAULT_SWEEP_INTERVAL_MS``: how often the periodic sweep runs (60 s).
# ``DEFAULT_SWEEP_LOOKBACK_MS``: client-side filter applied to
# ``exec_time_ms`` on each returned fill — only executions newer than
# ``now - lookback_ms`` are processed.  Limits exposure when the factory
# returns a full-day snapshot that is large on high-turnover accounts.
DEFAULT_SWEEP_INTERVAL_MS = 60_000
DEFAULT_SWEEP_LOOKBACK_MS = 900_000


# Period of the pending-intent tick (gap #1 of the broker-activity handoff).
# The publisher's main loop only reacts to broker events; without this
# tick, an intent the engine has emitted but the broker hasn't yet
# acknowledged is invisible in the cockpit. 2 s keeps the operator's
# Working/Pending panel within roughly one heartbeat of the engine's
# pending_intents while costing one sidecar+WAL stat per tick.
_PENDING_INTENT_TICK_S = 2.0


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


class _ConsumerEnded(Exception):
    """Raised by ``_consumer_wrapper`` when the event consumer completes
    normally (the async generator was exhausted without raising).

    Normal completion is not a success condition for the supervisor — the
    publisher is no longer consuming broker events and must be restarted.
    Surfacing it as an exception causes the ``TaskGroup`` to cancel the
    pending-intent sibling and exit, which flips ``is_running`` to False
    so the registry can detect and re-bootstrap as appropriate.
    """


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
        recovery_source_factory: RecoverySourceFactory | None = None,
        incident_store: IncidentStore | None = None,
        sweep_interval_ms: int = DEFAULT_SWEEP_INTERVAL_MS,
        sweep_lookback_ms: int = DEFAULT_SWEEP_LOOKBACK_MS,
    ) -> None:
        self._strategy_instance_id = strategy_instance_id
        self._bot_order_namespace = bot_order_namespace
        self._timing_policy = timing_policy
        self._event_source_factory = event_source_factory
        self._recovery_source_factory = recovery_source_factory
        # PR 6 — incident store and sweep config.
        self._incident_store = incident_store
        self._sweep_interval_ms = sweep_interval_ms
        self._sweep_lookback_ms = sweep_lookback_ms
        self._wal = BrokerActivityWal(stable_broker_activity_wal_path(run_dir))
        self._sidecar = LiveStateSidecarRepo(
            stable_live_state_path(artifacts_root, strategy_instance_id)
        )
        # Intent WAL — the durable record of every submit-lifecycle event.
        # We fold it into the submitted-orders projection so a fresh fill
        # bearing this instance's own ``order_ref`` matches even when the
        # sidecar's ``submitted_orders`` map is empty (the normal durable
        # submit path writes the WAL but only updates the sidecar
        # asynchronously via the engine's flush cycle).
        self._intent_wal = IntentWal(run_dir / "intent_events.jsonl")
        self._fold_cache: tuple[float, dict[str, dict]] | None = None

        self._subscribers: set[asyncio.Queue[BrokerActivityRow | None]] = set()
        self._seen_exec_ids: set[str] = set()
        # Per-intent dedup for the pending-intent tick: once a pending row
        # has been authored for a given intent_id, the tick suppresses
        # further pending rows for it. Entries are pruned when the intent
        # leaves the unacked state (broker acked, intent removed, or
        # ledger fold no longer surfaces it) so a re-emergence of the
        # same intent_id would re-author.
        self._authored_pending_intent_ids: set[str] = set()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._pending_tick_period_s: float = _PENDING_INTENT_TICK_S
        # Test-only: the supervisor stores child task references here so
        # tests can observe them via ``_snapshot_children_for_tests``.
        self._child_tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()
        # Slice 3 (ADR 0011 amendment) — flipped True for the duration
        # of ``sweep_reconnect_recovery``. While true, ``place_paper_order``
        # refuses new submissions (the broker connection is replaying
        # history and a new order would race the sweep) and every authored
        # row carries ``reconnect_recovery_active=True`` so the
        # ``reconnect_recovery`` template fires instead of e.g. a raw
        # excessive-lag verdict on an exec the publisher missed mid-drop.
        self._reconnect_recovery_active: bool = False
        # Serialises concurrent reconnect sweeps — a flapping connection could
        # trigger back-to-back reconnects; the second sweep waits for the
        # first to finish so the dedupe set is the merged truth, not a torn
        # read.
        self._recovery_lock = asyncio.Lock()
        # Serialises concurrent periodic sweeps against each other.  Separate
        # from ``_recovery_lock`` so a slow periodic sweep does not block a
        # reconnect sweep (and vice versa) — they serve different purposes and
        # must not deadlock each other.
        self._sweep_lock = asyncio.Lock()
        # On cold start, seed both dedupe sets from the WAL so we don't
        # re-author a row IBKR redelivers right after the publisher
        # restarts (``_seen_exec_ids``) AND so the pending-intent tick
        # doesn't re-emit an ``engine_only_pending`` row for an intent
        # that still has no broker resolution but already has a
        # persisted pending row (``_authored_pending_intent_ids``).
        #
        # The pending-row seed is keyed by ``intent_id`` parsed from
        # ``order_ref``; a later non-pending row with the same
        # ``order_ref`` (the broker fill / cancel that supersedes the
        # pending) drops the dedupe entry so the tick is free to
        # re-author if the engine ever re-emits the intent.
        # PR 5 — most-recent row's wall-clock ms for the health surface.
        # Cold-start: latest_row_ms is None until this process observes a row.
        # We do NOT seed from the WAL here because that would cause the health
        # composer to compare now_ms against a stale historical timestamp and
        # report ``degraded`` immediately after a healthy restart on a quiet
        # strategy. The WAL is read below only for dedup (_seen_exec_ids) and
        # pending-intent bookkeeping (_authored_pending_intent_ids); the
        # health cursor advances exclusively inside _persist_and_broadcast
        # when a row is authored in-process.
        self._latest_row_ms: int | None = None
        latest_verdict_by_intent: dict[str, str] = {}
        for row in self._wal.read_all():
            if row.exec_id:
                self._seen_exec_ids.add(row.exec_id)
            parsed = parse_order_ref(row.order_ref)
            if parsed is None:
                continue
            latest_verdict_by_intent[parsed[1]] = row.verdict
        for intent_id, verdict in latest_verdict_by_intent.items():
            if verdict == "engine_only_pending":
                self._authored_pending_intent_ids.add(intent_id)

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the supervisor task. Idempotent — calling twice has no
        effect when the supervisor is still alive."""
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._stopped.clear()
        self._child_tasks = []
        self._supervisor_task = asyncio.create_task(
            self._run_supervisor(),
            name=f"broker-activity-supervisor:{self._strategy_instance_id}",
        )

    async def stop(self) -> None:
        """Cancel the supervisor (which cancels all children) and signal
        all subscribers to drain.

        Each subscriber's queue receives a ``None`` sentinel; subscribers
        loop on ``get()`` and treat ``None`` as end-of-stream."""
        self._stopped.set()
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=5.0)
            except (asyncio.CancelledError, TimeoutError):
                logger.debug(
                    "publisher supervisor cancelled during stop",
                    extra={"strategy_instance_id": self._strategy_instance_id},
                )
            self._supervisor_task = None
        for q in self._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                # The subscriber is already behind — they'll see the
                # cancellation when they next try to read.
                logger.debug(
                    "subscriber queue full during stop; end sentinel not enqueued",
                    extra={"strategy_instance_id": self._strategy_instance_id},
                )

    @property
    def is_running(self) -> bool:
        return (
            self._supervisor_task is not None and not self._supervisor_task.done()
        )

    @property
    def latest_row_ms(self) -> int | None:
        """Wall-clock ms of the most recent row authored by this publisher.

        ``None`` when no rows have been authored yet (cold-start with an
        empty WAL).  Updated by ``_persist_and_broadcast`` on every new
        row so the health surface can detect a stalled feed.
        """
        return self._latest_row_ms

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

    # ── test helper ───────────────────────────────────────────────

    def _snapshot_children_for_tests(self) -> list[asyncio.Task[None]]:
        """Return the current list of child tasks spawned by the supervisor.

        Test-only helper — production code must not call this. The list
        is populated by ``_run_supervisor`` before ``TaskGroup.__aexit__``
        blocks, so callers must snapshot *after* ``start()`` and give the
        event loop a tick to allow the supervisor to create the children.
        """
        return list(self._child_tasks)

    # ── background loop ──────────────────────────────────────────

    async def _run_supervisor(self) -> None:
        """Single owning supervisor that runs both children under a TaskGroup.

        The TaskGroup's structured-concurrency guarantee ensures:
        - If either child raises, the other is cancelled automatically.
        - ``stop()`` cancels only this task; cancellation cascades to
          children via the TaskGroup's internal bookkeeping.
        - A child crash that escapes to this level is caught as an
          ``ExceptionGroup``, logged as CRITICAL, and the supervisor
          exits (causing ``is_running`` to flip False so health checks
          notice).

        Normal completion of the consumer is treated as an error: the
        publisher is no longer consuming broker events and must be
        restarted. ``_consumer_wrapper`` raises ``_ConsumerEnded`` on
        normal completion so the TaskGroup cancels the sibling and the
        supervisor exits cleanly. The registry detects the supervisor's
        death via ``is_running`` and can re-bootstrap as appropriate.
        """
        try:
            async with asyncio.TaskGroup() as tg:
                consumer = tg.create_task(
                    self._consumer_wrapper(),
                    name=f"broker-activity-publisher:{self._strategy_instance_id}",
                )
                pending = tg.create_task(
                    self._pending_intent_loop(),
                    name=f"broker-activity-pending-tick:{self._strategy_instance_id}",
                )
                # PR 6 — periodic IBKR reqExecutions sweep. Lives inside the
                # supervisor's TaskGroup so structured-concurrency cancellation
                # propagates uniformly: stop() cancels the supervisor → group
                # cancels all three children.
                sweep = tg.create_task(
                    self._periodic_sweep_loop(),
                    name=f"broker-activity-periodic-sweep:{self._strategy_instance_id}",
                )
                self._child_tasks = [consumer, pending, sweep]
        except* _ConsumerEnded:
            logger.warning(
                "broker-activity event consumer ended normally; supervisor exiting",
                extra={"strategy_instance_id": self._strategy_instance_id},
            )
        except* Exception as eg:
            logger.critical(
                "broker-activity supervisor exiting due to unhandled child exception(s)",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "exceptions": [str(exc) for exc in eg.exceptions],
                },
            )

    async def _consumer_wrapper(self) -> None:
        """Run the event consumer; raise ``_ConsumerEnded`` on normal exit.

        ``asyncio.TaskGroup`` cancels siblings on a child *raise* but NOT on
        a child's normal return. If the event source is exhausted without
        raising, ``_run_event_consumer`` returns cleanly — leaving the
        supervisor waiting on ``_pending_intent_loop`` forever, with
        ``is_running`` still True and broker events silently lost.

        Wrapping the consumer and converting its normal completion to
        ``_ConsumerEnded`` surfaces the exit to the TaskGroup, which then
        cancels the pending-intent sibling and lets the supervisor exit.
        ``is_running`` flips False and the registry can re-bootstrap.
        """
        await self._run_event_consumer()
        raise _ConsumerEnded("event source exhausted without raising")

    async def _run_event_consumer(self) -> None:
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

    async def _pending_intent_loop(self) -> None:
        """Periodic tick that surfaces engine intents not yet broker-acked.

        Runs as a sibling child task inside the supervisor's TaskGroup
        alongside ``_run_event_consumer`` so the cockpit's Working/Pending
        panel reflects engine state even when no broker events are arriving.
        A crash in one tick is logged and the loop continues — a single
        bad fold should not silence the rest of the publisher.
        """
        try:
            while not self._stopped.is_set():
                try:
                    self._pending_intent_tick()
                except Exception:
                    # Same discipline as ``_handle_event``: log with
                    # context but keep the loop alive. The next tick may
                    # succeed (transient sidecar IO race during engine
                    # flush, etc.).
                    logger.exception(
                        "broker-activity pending-intent tick failed",
                        extra={
                            "strategy_instance_id": self._strategy_instance_id,
                        },
                    )
                await asyncio.sleep(self._pending_tick_period_s)
        except asyncio.CancelledError:
            raise

    def _pending_intent_tick(self) -> None:
        """Author ``engine_only_pending`` rows for unacked intents.

        Reads the intent WAL, folds it, and for every intent whose status
        is in ``_UNRESOLVED_STATUSES`` (the canonical "still pending"
        predicate from ``intent_ledger``) AND has a usable ``order_spec``
        AND hasn't already been authored, calls ``author_pending_row`` and
        broadcasts.

        ``INTENT_NOT_ACCEPTED`` is treated as resolved-terminal (not
        pending) because ``intent_ledger._UNRESOLVED_STATUSES`` excludes
        it: ``live_portfolio`` writes it after a ``PROVABLY_ABSENT`` probe
        before retrying, so authoring an "Awaiting broker ack" row for it
        would be operator-misleading.

        Dedup is keyed on ``intent_id``; the in-memory set is pruned to
        the currently-unacked set on every tick so an intent that
        re-enters pending state (e.g. the engine restarted and the
        fold sees its PENDING_INTENT again) would re-author.

        Truthfulness contract: an intent whose ``order_spec`` is missing
        a field the pending template requires (symbol / action /
        quantity / order_type) is logged as a warning and skipped — the
        publisher never authors a partially-rendered string. The intent
        is NOT added to the dedup set, so the next tick will retry once
        the order_spec is fully populated.
        """
        try:
            events = self._intent_wal.read_tail()
        except (IntentWalCorruptError, OSError):
            # Same fall-back as ``_fold_intent_wal``: a corrupt WAL is
            # logged, but doesn't take down the publisher.
            logger.warning(
                "intent WAL unreadable; pending-intent tick skipped",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "intent_wal_path": str(self._intent_wal.path),
                },
            )
            return
        if not events:
            self._authored_pending_intent_ids.clear()
            return
        view = fold_intent_events(LedgerProjection(), events)

        currently_pending: set[str] = set()
        for intent_id, order_view in view.submitted_orders.items():
            if order_view.status not in _UNRESOLVED_STATUSES:
                # Either broker-acked (SUBMITTED / SUBMITTED_RECOVERED /
                # ADOPTED_BROKER_ORDER) or terminally-absent
                # (INTENT_NOT_ACCEPTED). Neither is pending.
                continue
            if order_view.intent_kind.value != "STRATEGY":
                # Operator-initiated flattens / recoveries surface
                # through their own broker events; the pending-intent
                # surface is for engine-emitted strategy orders.
                continue
            currently_pending.add(intent_id)
            if intent_id in self._authored_pending_intent_ids:
                continue
            self._author_pending_row_for_view(intent_id, order_view)

        # Prune dedup set: an intent no longer in the unacked set has
        # either been broker-acked (the live event path will handle its
        # fill / cancel row) or has disappeared from the WAL fold (a
        # corruption recovery, etc.). Either way, the dedup entry is
        # stale.
        self._authored_pending_intent_ids &= currently_pending

    def _author_pending_row_for_view(
        self, intent_id: str, order_view
    ) -> None:
        """Author one ``engine_only_pending`` row from a folded view.

        Splits out so the dedup bookkeeping above stays readable. The
        broadcast path reuses ``_author_and_broadcast``'s tail (WAL
        append + subscriber fan-out + cursor advance) by constructing
        the row manually, allocating its seq, and going through the
        same persistence helpers.
        """
        spec = order_view.order_spec or {}
        symbol = spec.get("symbol")
        action = spec.get("action")
        quantity = spec.get("quantity")
        order_type = spec.get("order_type")
        if not (symbol and action and quantity is not None and order_type):
            logger.warning(
                "pending intent missing order_spec fields; skipping",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "intent_id": intent_id,
                    "have": sorted(k for k, v in spec.items() if v is not None),
                },
            )
            return

        intent = EngineIntent(
            intent_id=intent_id,
            requested_qty=_safe_float(quantity),
            requested_price=_safe_float(spec.get("limit_price")),
        )
        seq = self._wal.allocate_seq()
        ctx = ReconciliationContext(
            seq=seq,
            ts_ms=_now_ms(),
            bot_order_namespace=self._bot_order_namespace,
            timing_policy=self._timing_policy,
            previously_seen_exec_ids=frozenset(self._seen_exec_ids),
            reconnect_recovery_active=self._reconnect_recovery_active,
        )
        try:
            row = author_pending_row(
                intent=intent,
                symbol=symbol,
                side=action,
                quantity=float(quantity),
                order_type=order_type,
                ctx=ctx,
            )
        except (UnauthorableEventError, ValueError):
            logger.exception(
                "pending intent could not be authored; skipping",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "intent_id": intent_id,
                },
            )
            return

        self._persist_and_broadcast(row)
        self._authored_pending_intent_ids.add(intent_id)

    async def _handle_event(self, event: IbkrOrderEvent) -> None:
        """Author at most one row from one event; fan out to subscribers."""
        # Filter: only events bearing OUR namespace OR truly foreign
        # events (no parseable order_ref) get authored. An event with a
        # parseable ``order_ref`` whose namespace belongs to a DIFFERENT
        # strategy instance is silently ignored — when multiple
        # instances share an IBKR account, ``stream_order_events`` yields
        # every same-account trade, and authoring it here would write
        # one ``unmatched_execution`` row per other instance per fill.
        # Intermediate status events for OUR orders (Submitted,
        # PreSubmitted) are skipped further down.
        parsed_ref = parse_order_ref(event.order_ref)
        if (
            parsed_ref is not None
            and parsed_ref[0] != self._bot_order_namespace
        ):
            return
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
        self._author_and_broadcast(event=event, intent=intent)

    def _author_and_broadcast(
        self,
        *,
        event: IbkrOrderEvent,
        intent: EngineIntent | None,
    ) -> BrokerActivityRow | None:
        """Allocate a seq, author the row, append to WAL, broadcast.

        Returns the authored row, or ``None`` when the event is
        unauthorable (logged + skipped per the truthfulness contract).
        Shared by the live event loop (``_handle_event``) and the
        reconnect-recovery sweep — both walk identical authoring rails so
        the only behavioural delta is the ``reconnect_recovery_active``
        flag carried on the context.
        """
        seq = self._wal.allocate_seq()
        ctx = ReconciliationContext(
            seq=seq,
            ts_ms=_now_ms(),
            bot_order_namespace=self._bot_order_namespace,
            timing_policy=self._timing_policy,
            previously_seen_exec_ids=frozenset(self._seen_exec_ids),
            reconnect_recovery_active=self._reconnect_recovery_active,
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
            return None

        self._persist_and_broadcast(row)
        return row

    def _persist_and_broadcast(self, row: BrokerActivityRow) -> None:
        """Shared persistence + fan-out tail.

        Called by both the live event loop and the pending-intent tick;
        a fresh fill's ``exec_id`` is added to the dedupe set so a
        subsequent reconnect-recovery sweep doesn't author the same
        execution twice. Pending rows have ``exec_id=None`` (no broker
        execution yet), so the dedupe-add is a no-op for them.
        """
        self._wal.append_row(row)
        if row.exec_id:
            self._seen_exec_ids.add(row.exec_id)
        # PR 5 — update the health-surface cursor so the composer can
        # detect stalled feeds without scanning the WAL.
        if self._latest_row_ms is None or row.ts_ms > self._latest_row_ms:
            self._latest_row_ms = row.ts_ms
        self._update_envelope_cursor(row.seq)
        self._broadcast(row)

    # ── helpers (engine state + broadcast + envelope) ─────────────

    def _read_submitted_orders(self) -> dict[str, dict]:
        """Return the union of (a) the sidecar's persisted ``submitted_orders``
        snapshot and (b) the intent WAL folded over that snapshot.

        The sidecar snapshot is the engine's last-flushed projection — it
        lags behind the WAL because the engine writes the WAL synchronously
        before ``placeOrder`` and only updates the sidecar later on its
        flush cycle. A fresh fill on this instance's own ``order_ref``
        therefore arrives while the sidecar's map is still empty; folding
        the WAL closes that window so ``match_identity`` recognises the
        intent and the row gets the engine overlay.

        Reuses ``app.engine.live.intent_ledger.fold`` — the canonical
        fold the rest of the system uses — so this view stays consistent
        with the engine's own cold-start projection.

        Caching: the fold is keyed on the WAL file's mtime. Each broker
        event triggers one stat; we re-fold only when the WAL has grown.
        """
        envelope = self._sidecar.read()
        try:
            wal_mtime = self._intent_wal.path.stat().st_mtime
        except FileNotFoundError:
            wal_mtime = 0.0
        cache = self._fold_cache
        if cache is not None and cache[0] == wal_mtime:
            wal_view = cache[1]
        else:
            wal_view = self._fold_intent_wal()
            self._fold_cache = (wal_mtime, wal_view)
        if envelope is None:
            return dict(wal_view)
        merged: dict[str, dict] = dict(envelope.submitted_orders)
        for intent_id, entry in wal_view.items():
            merged.setdefault(intent_id, entry)
        return merged

    def _fold_intent_wal(self) -> dict[str, dict]:
        """Fold the intent WAL into a ``{intent_id: dict}`` projection.

        Mirrors ``_read_submitted_orders``' shape — entries carry the
        broker-echoed identifiers (``order_id``, ``perm_id``, ``status``)
        plus the audit-only ``order_spec`` when present. Returns an empty
        dict when the WAL is missing, empty, or corrupt (a corrupt WAL is
        logged but does not crash the publisher; the sidecar snapshot
        remains authoritative).

        Always folds over an empty projection — the merge with the
        sidecar's existing snapshot happens in ``_read_submitted_orders``.
        We can't fold over ``projection_from_envelope(envelope)`` here
        because the sidecar's ``submitted_orders`` schema is an opaque
        ``dict[str, dict[str, Any]]`` that may carry IBKR status strings
        (e.g. ``"Submitted"``) the ``IntentEventType`` enum doesn't know.
        Since we only need key presence for ``match_identity``, an empty
        starting projection plus a downstream dict merge is sufficient.
        """
        try:
            events = self._intent_wal.read_tail()
        except (IntentWalCorruptError, OSError):
            logger.warning(
                "intent WAL unreadable; falling back to sidecar snapshot only",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "intent_wal_path": str(self._intent_wal.path),
                },
            )
            return {}
        if not events:
            return {}
        view = fold_intent_events(LedgerProjection(), events)
        out: dict[str, dict] = {}
        for intent_id, order_view in view.submitted_orders.items():
            entry: dict[str, object] = {
                "status": order_view.status.value,
                "order_ref": order_view.order_ref,
                "bot_order_namespace": order_view.bot_order_namespace,
            }
            if order_view.order_id is not None:
                entry["order_id"] = order_view.order_id
            if order_view.perm_id is not None:
                entry["perm_id"] = order_view.perm_id
            if order_view.order_spec is not None:
                entry.update(
                    {k: v for k, v in order_view.order_spec.items() if k not in entry}
                )
            out[intent_id] = entry
        return out

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

    # ── periodic sweep (PR 6 / operator-notice §11) ───────────────────

    async def _periodic_sweep_loop(self) -> None:
        """Immediate first sweep, then one per ``_sweep_interval_ms``.

        A crash in ``_run_periodic_sweep`` is logged and the loop
        continues — a transient factory error (e.g. broker hiccup)
        should not silence all future sweeps.
        """
        try:
            while not self._stopped.is_set():
                try:
                    await self._run_periodic_sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "broker-activity periodic sweep raised; continuing",
                        extra={"strategy_instance_id": self._strategy_instance_id},
                    )
                await asyncio.sleep(self._sweep_interval_ms / 1_000)
        except asyncio.CancelledError:
            raise

    async def _run_periodic_sweep(self) -> int:
        """Fetch executions and author/flag any that are new and unmatched.

        Uses the same ``_recovery_source_factory`` and dedup path as
        ``sweep_reconnect_recovery``.  Differs in two ways:

        1. **Does NOT set ``_reconnect_recovery_active``** — the
           periodic sweep is background bookkeeping, not a post-reconnect
           halt.  Submissions are not gated.
        2. **Emits a critical ``OperatorIncident``** when the sweep finds
           a fill that is (a) not in ``_seen_exec_ids`` and (b) carries
           no engine intent.  The engine remains authoritative; the
           publisher still authors the ``unmatched_execution`` row so the
           operator can see forensic detail, but NEVER silently corrects
           cockpit portfolio state.

        Returns the count of newly-authored rows (0 when the factory is
        not wired or every exec_id was already known).
        """
        if self._recovery_source_factory is None:
            return 0

        now_ms = _now_ms()
        cutoff_ms = now_ms - self._sweep_lookback_ms

        async with self._sweep_lock:
            events = await self._recovery_source_factory()
            submitted = self._read_submitted_orders()
            authored = 0
            for event in events:
                # Lookback filter: skip executions older than the window.
                if event.exec_time_ms is not None and event.exec_time_ms < cutoff_ms:
                    continue
                if event.exec_id and event.exec_id in self._seen_exec_ids:
                    continue
                # Namespace filter: skip fills owned by a different instance
                # (same logic as sweep_reconnect_recovery).
                parsed_ref = parse_order_ref(event.order_ref)
                if (
                    parsed_ref is not None
                    and parsed_ref[0] != self._bot_order_namespace
                ):
                    continue
                intent_id = match_identity(
                    event,
                    submitted_orders=submitted,
                    bot_order_namespace=self._bot_order_namespace,
                )
                if intent_id is None:
                    # Foreign execution — not initiated by this bot.
                    # Author the row for forensic visibility, then
                    # emit a critical incident (PR 6 §11).
                    self._author_and_broadcast(event=event, intent=None)
                    authored += 1
                    self._emit_cross_client_incident(event, now_ms=now_ms)
                else:
                    intent = self._build_engine_intent(intent_id)
                    row = self._author_and_broadcast(event=event, intent=intent)
                    if row is not None:
                        authored += 1

            logger.info(
                "broker-activity periodic sweep complete: authored %d row(s)",
                authored,
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "authored_count": authored,
                    "lookback_ms": self._sweep_lookback_ms,
                },
            )
            return authored

    def _emit_cross_client_incident(
        self, event: IbkrOrderEvent, *, now_ms: int
    ) -> None:
        """Emit a critical ``OperatorIncident`` for a foreign execution.

        If no ``IncidentStore`` is wired (legacy callers, tests), logs a
        structured WARN instead of raising — the contract is "surface the
        anomaly", not "crash the sweep".
        """
        exec_id = event.exec_id or "unknown"
        incident_id = f"cross-client-{exec_id}"
        symbol = event.symbol or "UNKNOWN"
        side = event.side or "UNKNOWN"
        qty = event.fill_quantity or 0.0
        price = event.last_fill_price or event.avg_fill_price or 0.0
        perm_id = event.perm_id
        order_ref = event.order_ref

        if self._incident_store is None:
            logger.warning(
                "cross-client execution discovered but no IncidentStore wired; "
                "incident not persisted",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "exec_id": exec_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "price": price,
                },
            )
            return

        incident = OperatorIncident(
            incident_id=incident_id,
            category="reconciliation",
            notice=OperatorNotice(
                code="reconciliation.discovered_execution_not_in_engine_state",
                tier="critical",
                title="Foreign execution discovered at broker",
                message=(
                    f"A {side} of {qty} {symbol} at {price} executed on this "
                    "account but was not initiated by this bot. Verify the "
                    "bot's positions against IBKR before resuming."
                ),
                forensic_facts={
                    "exec_id": exec_id,
                    "perm_id": perm_id,
                    "order_ref": order_ref,
                    "symbol": symbol,
                    "side": side,
                    "quantity": qty,
                    "price": price,
                    "discovered_at_ms": now_ms,
                },
                action=OperatorNoticeAction(
                    kind="external_manual_check",
                    label="Check positions in IBKR",
                    target="ibkr_positions",
                ),
                runbook_slug="cross-client-execution",
            ),
            started_at_ms=now_ms,
            evidence={
                "exec_id": exec_id,
                "perm_id": perm_id,
                "order_ref": order_ref,
                "symbol": symbol,
                "side": side,
                "quantity": qty,
                "price": price,
            },
        )
        try:
            self._incident_store.append(incident)
            logger.warning(
                "cross-client execution incident persisted",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "incident_id": incident_id,
                    "exec_id": exec_id,
                    "symbol": symbol,
                },
            )
        except Exception as exc:
            logger.critical(
                "[publisher] failed to persist cross-client incident; "
                "foreign execution will not surface in cockpit",
                extra={
                    "strategy_instance_id": self._strategy_instance_id,
                    "incident_id": incident_id,
                    "exec_id": exec_id,
                    "incident_payload": incident.model_dump_json(),
                    "exception": repr(exc),
                },
            )

    # ── reconnect recovery (slice 3 / ADR 0011 amendment) ─────────────

    @property
    def is_reconnect_recovery_active(self) -> bool:
        """True while ``sweep_reconnect_recovery`` is running.

        The registry's ``any_recovery_active`` ORs this across every
        publisher; ``place_paper_order`` consults the registry on the
        submit hot path so a new order placed mid-sweep is refused
        instead of racing the broker's execution replay.
        """
        return self._reconnect_recovery_active

    async def sweep_reconnect_recovery(self) -> int:
        """Fetch the day's executions, author rows for any unseen ones.

        Wired into the ``AutoReconnectMonitor.recovery_callbacks`` chain
        so it fires exactly once per successful reconnect. The lifecycle
        contract:

        1. Set ``_reconnect_recovery_active=True`` so the registry's
           cross-instance ``any_recovery_active`` flips True. From here
           until step 5, ``place_paper_order`` refuses new submissions
           (the broker is replaying history and a new order would race).
        2. Call the publisher's ``recovery_source_factory`` (the
           production wiring runs ``IB.reqExecutionsAsync`` and adapts
           each ``Fill`` into an ``IbkrOrderEvent``).
        3. For each event whose ``exec_id`` is not in
           ``_seen_exec_ids`` AND whose ``order_ref`` namespace matches
           this instance, author a row via the shared
           ``_author_and_broadcast`` path. The flag set in step 1 means
           ``classify_verdict`` promotes the verdict to
           ``expected_with_caveat`` and the ``reconnect_recovery``
           template fires.
        4. Foreign exec_ids (no namespace match) are NOT swept here —
           they are noise from other instances on the shared account
           and would be authored as ``UNMATCHED_EXECUTION`` by every
           publisher on the account if we did. The live event loop
           still picks them up as foreign rows when they arrive on
           ``stream_order_events``.
        5. Lift the flag in a ``finally`` so a crashing factory never
           pins the submission halt.

        Returns the count of newly-authored recovery rows (zero when no
        factory is wired, the sweep was a no-op, or every returned
        exec_id was already known).

        Idempotent under concurrent invocation via ``_recovery_lock`` —
        a flapping connection that triggers back-to-back reconnects
        serialises the sweeps so the dedupe set converges.
        """
        if self._recovery_source_factory is None:
            logger.debug(
                "broker-activity publisher has no recovery_source_factory; "
                "skipping reconnect sweep",
                extra={"strategy_instance_id": self._strategy_instance_id},
            )
            return 0

        async with self._recovery_lock:
            self._reconnect_recovery_active = True
            authored = 0
            try:
                events = await self._recovery_source_factory()
                submitted = self._read_submitted_orders()
                for event in events:
                    if event.exec_id and event.exec_id in self._seen_exec_ids:
                        # Already authored before the drop — IBKR
                        # redelivered it but we have a row for it.
                        continue
                    intent_id = match_identity(
                        event,
                        submitted_orders=submitted,
                        bot_order_namespace=self._bot_order_namespace,
                    )
                    if intent_id is None:
                        # Foreign exec under a shared paper account.
                        # The per-instance scope rule forbids authoring
                        # other instances' executions during our sweep.
                        # The live event loop already handles foreign
                        # rows that arrive after reconnect.
                        continue
                    intent = self._build_engine_intent(intent_id)
                    row = self._author_and_broadcast(event=event, intent=intent)
                    if row is not None:
                        authored += 1
                logger.info(
                    "broker-activity reconnect sweep complete: authored %d row(s)",
                    authored,
                    extra={
                        "strategy_instance_id": self._strategy_instance_id,
                        "authored_count": authored,
                    },
                )
                return authored
            finally:
                self._reconnect_recovery_active = False

    def _broadcast(self, row: BrokerActivityRow) -> None:
        """Push the row to every subscriber. A full queue means the
        subscriber is too slow — drop one stale row to make room for the
        ``None`` sentinel so the SSE handler unblocks and closes the
        connection (without the sentinel the handler stays blocked in
        ``queue.get()`` forever and silently misses all future rows).
        Other subscribers are unaffected."""
        dead: list[asyncio.Queue[BrokerActivityRow | None]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(row)
            except asyncio.QueueFull:
                logger.warning(
                    "dropping slow broker-activity subscriber",
                    extra={"strategy_instance_id": self._strategy_instance_id},
                )
                # Make room for the sentinel by draining one stale row.
                # The subscriber was already going to lose rows — better
                # to lose one and tell them so than to lose all future
                # rows silently.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    logger.debug(
                        "slow subscriber queue already empty during drain",
                        extra={"strategy_instance_id": self._strategy_instance_id},
                    )
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    # Truly stuck (e.g. a second producer concurrently
                    # re-filled the queue). The consumer will time out
                    # at the transport layer.
                    logger.debug(
                        "slow subscriber queue still full; closing subscriber",
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


__all__ = [
    "DEFAULT_SWEEP_INTERVAL_MS",
    "DEFAULT_SWEEP_LOOKBACK_MS",
    "BrokerActivityPublisher",
    "EventSourceFactory",
    "RecoverySourceFactory",
]
