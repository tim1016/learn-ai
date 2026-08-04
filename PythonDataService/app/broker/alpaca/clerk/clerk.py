"""AlpacaClerk — in-process single-writer order submission (phase 2, S1).

The Clerk is the sole author of order submission for Alpaca. For each leg it:

1. **Mints identity** via the canonical, broker-agnostic order-identity module —
   ``build_manual_order_namespace`` + ``mint_intent_id`` + ``build_order_ref``,
   failing closed over the ``order_ref`` length cap — so
   ``client_order_id == order_ref == manual/{operator}/v1:{intent_id}``.
2. **Journals ``intent_recorded`` and ``fsync``'s it** (inbox + journal) BEFORE
   any broker call. No journal → no order.
3. **Calls the trade port** to submit.
4. **Journals ``submit_acked``** (with the ``BrokerOrder``) on success, or
   **``submit_failed``** on a ``BrokerError``, and returns a per-leg result.

Serialization: a single ``asyncio.Lock`` (the intake lock) makes submission
serial per account — combined with the single-uvicorn-worker deployment
constraint documented in this package's ``__init__``. A per-leg failure never
blocks the remaining legs; each leg is an independent journaled unit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from app.broker.alpaca.clerk import derive, diagnosis, reconcile, recovery
from app.broker.alpaca.clerk.activity_recovery import AlpacaActivityRecovery
from app.broker.alpaca.clerk.custody import (
    CustodyReconciliationResult,
    project_custody_snapshot,
    project_instance_custody_proof,
)
from app.broker.alpaca.clerk.custody_resolution import (
    ClerkCustodyResolutionOperations,
    InventoryBaselineRefusedError,
)
from app.broker.alpaca.clerk.custody_resolution_store import CustodyResolutionStore
from app.broker.alpaca.clerk.effects import ClerkEffectOperations
from app.broker.alpaca.clerk.exposure import (
    project_expected_account_exposure,
    project_instance_exposure,
    signed_broker_position_quantity,
    verify_flatten,
)
from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.leg_identity import Clock, LegIdentity, default_clock, leg_error
from app.broker.alpaca.clerk.models import (
    STREAM_HEALTH_HOLD_CODE,
    UNEXPLAINED_ORDER_HOLD_CODE,
    AlpacaEffectOperation,
    ClerkCustodySnapshot,
    ClerkEntryKind,
    ClerkStatus,
    EffectOperationReceipt,
    EffectPurpose,
    InstanceCustodyProof,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegError,
    OrderLegResult,
    OrderSubmitResult,
    ReconciliationVerdict,
)
from app.broker.alpaca.clerk.stream_health import StreamHealthGate, stream_health_refusal
from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.errors import (
    BrokerError,
    BrokerSubmissionHeld,
    BrokerUnavailable,
)
from app.broker.contract.models import (
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerOrderRequest,
    BrokerPosition,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.engine.live.account_clerk_journal_models import (
    AccountClerkBrokerEvidenceBaseline,
    AccountClerkPositionEvidence,
)
from app.engine.live.desired_state import DesiredState
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_manual_order_namespace,
    build_order_ref,
    mint_intent_id,
    order_ref_namespace_matches,
    parse_order_ref,
)

logger = logging.getLogger(__name__)


class ClerkAdmissionSnapshotChangedError(Exception):
    """Clerk evidence changed before the Start admission fence was acquired."""


class AlpacaClerk(ClerkCustodyResolutionOperations, ClerkEffectOperations):
    """Single-writer order-submission facade for one Alpaca account.

    ``read`` supplies ``get_account`` (to resolve + cache the account id used
    for the journal path); ``trade`` supplies ``submit``. The journal is
    constructed lazily on first submit, once the account id is known.
    """

    broker_id = BROKER_ID

    def __init__(
        self,
        *,
        read: BrokerReadPort,
        trade: BrokerTradePort,
        clock: Clock = default_clock,
        stream_health: StreamHealthGate | None = None,
        clerk_generation: str | None = None,
        bot_running_probe: Callable[[], bool] | None = None,
        desired_state_probe: Callable[[str], DesiredState] | None = None,
    ) -> None:
        self._read = read
        self._trade = trade
        self._clock = clock
        self._stream_health = stream_health
        self._clerk_generation = clerk_generation or uuid4().hex
        self._bot_running_probe = bot_running_probe
        self._desired_state_probe = desired_state_probe
        self._intake_lock = asyncio.Lock()
        self.activity_recovery = AlpacaActivityRecovery(
            intake_lock=self._intake_lock, ensure_journal=self._ensure_journal, clock=clock
        )
        # Recovery owns historical, already-minted refs. Keep concurrent sweep
        # replays serial without making a slow by-client-id lookup block cancel.
        self._recovery_lock = asyncio.Lock()
        self._account_id: str | None = None
        self._journal: OrderJournal | None = None
        self._custody_resolution_store: CustodyResolutionStore | None = None
        # Observable counter: unexplained (foreign/absent-coid) lifecycle
        # events; the hold path reads the UNEXPLAINED_ORDER lines themselves.
        self._unexplained_order_count = 0
        # Caller cancellation must not cancel a Clerk-accepted effect.  The
        # operation task is intentionally Clerk-owned and awaited through a
        # shield by ``execute_for_instance``.
        self._effect_tasks: dict[tuple[str, str], asyncio.Task[EffectOperationReceipt]] = {}

    async def _ensure_journal(self) -> tuple[str, OrderJournal]:
        """Resolve + cache the account id and its journal (once)."""
        if self._journal is not None and self._account_id is not None:
            return self._account_id, self._journal
        account = await self._read.get_account()
        journal = OrderJournal(
            account_id=account.account_id, root=get_clerk_settings().dir
        )
        self._account_id = account.account_id
        self._journal = journal
        return account.account_id, journal

    async def submit(self, request: BrokerOrderRequest) -> OrderSubmitResult:
        """Submit legs serially, stopping before any later leg after uncertainty.

        The holds are checked FIRST, under the intake lock, BEFORE any intent
        is minted or journaled — a refused submit records NO intent and raises
        ``BrokerSubmissionHeld`` (409). Cancel is a separate, never-held path.
        An ``uncertain`` leg outcome stops the batch (the previous leg may have
        landed); a definitive rejection does not.
        """
        return await self._submit_batch(
            operator=request.operator, namespace=None, legs=request.legs
        )

    async def submit_for_instance(
        self, *, strategy_instance_id: str, legs: list[BrokerOrderLeg]
    ) -> OrderSubmitResult:
        """Submit legs under the bot-namespace scheme (S3, #1261 / ADR 0008).

        Refs are ``learn-ai/{strategy_instance_id}/v1:{intent_id}``, a peer of
        ``manual/{operator}/v1``; same capture-first, hold-gated discipline as
        :meth:`submit`. A bad sid raises ``ValueError`` before any journaling.
        """
        namespace = build_bot_order_namespace(strategy_instance_id)
        return await self._submit_batch(
            operator=strategy_instance_id, namespace=namespace, legs=legs
        )

    async def _submit_batch(
        self,
        *,
        operator: str,
        namespace: str | None,
        legs: list[BrokerOrderLeg],
    ) -> OrderSubmitResult:
        """Shared hold-gated serial leg submission for both namespace schemes."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            hold = derive.hold_state(journal.read_entries())
            if hold.active:
                logger.warning(
                    "alpaca clerk refused a submit: exposure hold is active",
                    extra={
                        "action": "submit_refused_hold",
                        "account_id": account_id,
                        "reason_code": hold.reason_code,
                    },
                )
                raise BrokerSubmissionHeld(
                    "Order submission is paused while an exposure hold is active.",
                    reason_code=hold.reason_code or UNEXPLAINED_ORDER_HOLD_CODE,
                    broker=self.broker_id,
                    detail=hold.reason,
                )
            # S4 dual-health gate: either stream unhealthy -> durable hold +
            # typed refusal naming the broken stream. Never auto-cleared.
            refusal = stream_health_refusal(self._stream_health)
            if refusal is not None:
                reason, detail = refusal
                await self._set_hold(
                    journal,
                    account_id=account_id,
                    reason_code=STREAM_HEALTH_HOLD_CODE,
                    reason=f"{reason} {detail}",
                )
                raise BrokerSubmissionHeld(
                    reason,
                    reason_code=STREAM_HEALTH_HOLD_CODE,
                    broker=self.broker_id,
                    detail=detail,
                )
            results: list[OrderLegResult] = []
            for leg in legs:
                result = await self._submit_leg(
                    operator, leg, account_id, journal, namespace=namespace
                )
                results.append(result)
                if result.status == "uncertain":
                    logger.warning(
                        "alpaca clerk stopped multi-leg submit after uncertain outcome",
                        extra={
                            "action": "submit_batch_stopped_uncertain",
                            "account_id": account_id,
                            "order_ref": result.order_ref,
                            "remaining_legs": len(legs) - len(results),
                        },
                    )
                    break
        return OrderSubmitResult(
            broker=self.broker_id, account_id=account_id, results=results
        )

    async def flatten_instance(
        self, *, strategy_instance_id: str, symbol: str, quantity: float
    ) -> OrderSubmitResult:
        """Close (part of) one instance's journal-owned exposure (P3 invariant b).

        ``verify_flatten`` (see its docstring for the refusal rules) runs
        **inside the intake lock, immediately before submit**, so no concurrent
        fill — which journals under this same lock — can invalidate the verdict.
        Not gated by the exposure hold: a verified flatten only *reduces* the
        instance's own exposure (P6 — reductions are never blocked).
        """
        namespace = build_bot_order_namespace(strategy_instance_id)
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            side = verify_flatten(
                journal.read_entries(),
                namespace=namespace,
                symbol=symbol,
                quantity=quantity,
            )
            leg = BrokerOrderLeg(symbol=symbol, side=side, quantity=quantity)
            result = await self._submit_leg(
                strategy_instance_id, leg, account_id, journal, namespace=namespace
            )
        return OrderSubmitResult(
            broker=self.broker_id, account_id=account_id, results=[result]
        )

    async def read_journal_entries(self) -> list[OrderJournalEntry]:
        """Read-only journal snapshot for the pure projection folds (P3/P12).

        ``project_instance_exposure`` over this snapshot is the ONLY hydration
        source for a bot's owned exposure — never the broker account-net map
        (07-27 wave-one defect, regression-pinned); ``project_instance_timeline``
        over it is the P12 per-bot timeline.
        """
        _account_id, journal = await self._ensure_journal()
        return journal.read_entries()

    async def cancel(self, order_id: str) -> OrderCancelResult:
        """Cancel one working order by its broker-assigned id.

        This is a **first-class path, deliberately NOT routed through ``submit``
        or its per-leg gating.** The holds (unexplained-order, stream-health)
        block *new exposure* — submission — but canceling a working order
        *reduces* exposure and must never be blocked by them; cancel therefore
        stays reachable while any hold is active. (The hold does not exist yet; do
        not add it here — this comment records the intended seam.)

        Flow, sharing the intake lock (so a cancel and a submit never interleave)
        and the same fail-closed journal:

        1. Resolve ownership from the journal: an order this Clerk submitted has a
           ``submit_acked`` line mapping ``broker order_id → order_ref``. A
           foreign/unowned order is still cancelable (safe direction), journaled
           with honest ``owned=False`` attribution — never a fabricated intent.
        2. Journal ``cancel_recorded`` and ``fsync`` it BEFORE the broker call.
        3. Call the trade port's ``cancel``.
        4. Journal ``cancel_acked`` on success, or ``cancel_failed`` on a
           ``BrokerError`` (a non-cancelable order is a typed what/why, not 500).
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            owning = derive.order_owner(
                journal.read_entries(), order_id, kind=ClerkEntryKind.SUBMIT_ACKED
            )
            owned = owning is not None

            def _entry(
                kind: ClerkEntryKind, *, error: BrokerError | None = None
            ) -> OrderJournalEntry:
                return OrderJournalEntry.attributed_from(
                    owning,
                    kind=kind,
                    account_id=account_id,
                    client_order_id=owning.client_order_id if owning is not None else "",
                    broker_order_id=order_id,
                    owned=owned,
                    recorded_at_ms=self._clock(),
                    error_message=error.message if error is not None else None,
                    error_detail=error.detail if error is not None else None,
                )

            order_ref = owning.order_ref if owning is not None else None

            # No journal → no cancel: record + fsync BEFORE the broker call.
            await journal.append_async(_entry(ClerkEntryKind.CANCEL_RECORDED))

            try:
                await self._trade.cancel(order_id)
            except BrokerError as exc:
                await journal.append_async(
                    _entry(ClerkEntryKind.CANCEL_FAILED, error=exc)
                )
                return OrderCancelResult(
                    broker=self.broker_id,
                    account_id=account_id,
                    order_id=order_id,
                    status="failed",
                    owned=owned,
                    order_ref=order_ref,
                    error=OrderLegError(message=exc.message, why=exc.detail),
                )

            await journal.append_async(_entry(ClerkEntryKind.CANCEL_ACKED))
            return OrderCancelResult(
                broker=self.broker_id,
                account_id=account_id,
                order_id=order_id,
                status="acked",
                owned=owned,
                order_ref=order_ref,
            )

    # ── S4 live-lifecycle path (trade_updates websocket) ─────────────────────

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None = None,
        recovery_source: str | None = None,
        recovery_window_limit: int | None = None,
    ) -> ClerkEntryKind:
        """Journal one parsed ``trade_updates`` lifecycle event, with attribution.

        The consumer captures the raw frame verbatim, parses it to a
        ``BrokerOrderEvent`` (via the adapter), and hands it here with the
        wire's ``client_order_id`` and a stable ``event_key`` (the dedup key the
        consumer already resolved: ``execution_id`` for a fill, else a synthetic
        ``order_id|event|timestamp``).

        Attribution runs against **this Clerk's known namespaces** using the
        canonical ``order_ref_namespace_matches`` — exact namespace equality,
        never a prefix. OWNED (``client_order_id`` namespace is ours) → an
        ``ORDER_EVENT`` line; UNOWNED / foreign / absent / unparseable →
        an ``UNEXPLAINED_ORDER`` line plus the observable
        :pyattr:`unexplained_order_count` counter.

        An unexplained observation also raises the exposure hold (S6, below).
        Returns the kind journaled (test/observability seam).
        """
        resume_operation: AlpacaEffectOperation | None = None
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            owned = order_ref_namespace_matches(
                client_order_id, self._known_namespaces(journal)
            )
            kind = (
                ClerkEntryKind.ORDER_EVENT if owned else ClerkEntryKind.UNEXPLAINED_ORDER
            )
            owning = (
                derive.order_owner_by_ref(journal.read_entries(), client_order_id)
                if owned and client_order_id is not None
                else None
            )
            await journal.append_async(
                OrderJournalEntry.attributed_from(
                    owning,
                    kind=kind,
                    account_id=account_id,
                    client_order_id=client_order_id or "",
                    broker_order_id=order.order_id if order is not None else None,
                    owned=owned,
                    recorded_at_ms=self._clock(),
                    order=order,
                    event=event,
                    event_key=event_key,
                    recovery_source=recovery_source,
                    recovery_window_limit=recovery_window_limit,
                )
            )
            if not owned:
                self._unexplained_order_count += 1
                logger.warning(
                    "alpaca clerk observed an unexplained order lifecycle event",
                    extra={
                        "action": "unexplained_order",
                        "account_id": account_id,
                        "client_order_id": client_order_id,
                        "event": event.event_type,
                        "event_key": event_key,
                    },
                )
                # S6 seam: an unexplained order is a safety event — raise the
                # account exposure hold so new submits are refused until an
                # operator clears it. Idempotent: a second unexplained event does
                # not re-journal an already-active HOLD_SET.
                await self._set_hold(
                    journal,
                    account_id=account_id,
                    reason_code=UNEXPLAINED_ORDER_HOLD_CODE,
                    reason=(
                        "An order this account did not submit was observed at "
                        "Alpaca. Submission is paused until an operator confirms "
                        "the account is safe."
                    ),
                )
            elif owning is not None and owning.effect_operation_id is not None:
                resume_operation = self._effect_operation_for_decision(
                    journal.read_entries(), owning.operator, owning.effect_operation_id
                )
        if resume_operation is not None and resume_operation.purpose is EffectPurpose.EXIT:
            self._start_effect_task(resume_operation)
        return kind

    @property
    def unexplained_order_count(self) -> int:
        """Observable counter: lifecycle events on orders this Clerk did not own."""
        return self._unexplained_order_count

    def _known_namespaces(self, journal: OrderJournal) -> frozenset[str]:
        """The manual-order namespaces this Clerk has minted, from the journal.

        Every owned order's ``order_ref`` parses to ``manual/{operator}/v1``;
        the set of those namespaces is the allowlist attribution matches against
        (exact equality). Rebuilt from the ledger so it survives a restart —
        the journal is the durable source of what this Clerk owns.
        """
        namespaces: set[str] = set()
        for entry in journal.read_entries():
            if not entry.order_ref:
                continue
            try:
                namespace, _ = parse_order_ref(entry.order_ref)
            except ValueError:
                continue
            namespaces.add(namespace)
        return frozenset(namespaces)

    # ── S6 exposure hold (account-level, journal-derived) ────────────────────

    def is_on_hold(self) -> bool:
        """True when an account-level exposure hold is active (journal-derived)."""
        # A read-only accessor for observability; the authoritative gate is the
        # under-lock check inside :meth:`submit`.
        if self._journal is None:
            return False
        return derive.hold_state(self._journal.read_entries()).active

    async def status(self) -> ClerkStatus:
        """The clerk's observable state (hold + latest verdict + outstanding
        intents): a journal-derived read under the intake lock, so it survives a
        restart and never observes a torn mid-write ledger."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            return self._status_from(account_id, journal.read_entries())

    async def custody_diagnosis(self) -> diagnosis.CustodyDiagnosis:
        """Read-only Clerk↔broker custody diagnosis (no journal/broker mutation).

        Reads the journal and a fresh broker snapshot, then projects the
        structured divergences, the resolution plan, and the snapshot guard.
        """
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            entries = journal.read_entries()
            namespaces = self._known_namespaces(journal)
            channel_fresh = self._channel_fresh()
            bot_running = self._bot_running()
        try:
            orders, positions = await asyncio.gather(
                self._read.list_orders(status="all", limit=500),
                self._read.list_positions(),
            )
        except BrokerUnavailable:
            return diagnosis.CustodyDiagnosis(
                broker=self.broker_id,
                account_id=account_id,
                in_sync=False,
                observed_at_ms=self._clock(),
                snapshot_version=diagnosis.stale_custody_snapshot_version(entries),
                resolvable=True,
                divergences=(diagnosis.stale_reconciliation_divergence(),),
                resolution_plan=(
                    diagnosis.CustodyResolutionStep(
                        action_id="reconcile_now", scope="account", mutates=False
                    ),
                ),
            )
        divergences = diagnosis.diagnose_custody(
            entries,
            orders=orders,
            positions=positions,
            namespaces=namespaces,
            channel_fresh=channel_fresh,
            bot_running=bot_running,
        )
        plan = diagnosis.resolution_plan(divergences)
        blocked = next(
            (d.prerequisite_detail for d in divergences if d.state == "blocked_on_prerequisite"),
            None,
        )
        return diagnosis.CustodyDiagnosis(
            broker=self.broker_id,
            account_id=account_id,
            in_sync=not divergences,
            observed_at_ms=self._clock(),
            snapshot_version=diagnosis.custody_snapshot_version(
                entries,
                orders,
                positions,
                namespaces=namespaces,
                channel_fresh=channel_fresh,
                bot_running=bot_running,
            ),
            resolvable=bool(plan),
            blocked_reason=blocked,
            divergences=divergences,
            resolution_plan=plan,
        )

    async def clear_hold(self, *, operator: str, reason: str) -> ClerkStatus:
        """Clear the active hold (operator exit) with reason-specific proof.

        Idempotent: clearing with no active hold is a journal-free NO-OP.
        The required proof depends on why the hold exists — a generic
        channel-health check is not sufficient for an ``UNEXPLAINED_ORDER``
        hold, which requires a fresh reconciliation proving the foreign
        order (and any other unresolved custody work) is actually gone.
        Unregistered reason codes refuse by default (fail closed).
        Returns the updated status for a one-round-trip render.
        """
        account_id, journal = await self._ensure_journal()
        entries = journal.read_entries()
        hold = derive.hold_state(entries)
        if not hold.active:
            return self._status_from(account_id, entries)

        proof_verdict: ReconciliationVerdict | None = None
        if hold.reason_code == STREAM_HEALTH_HOLD_CODE:
            if not self._channel_fresh():
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared while submission channels are unhealthy.",
                    detail="Restore both channels and reconcile before clearing the hold.",
                )
            proof_observed_at_ms = self._clock()
        elif hold.reason_code == UNEXPLAINED_ORDER_HOLD_CODE:
            proof_observed_at_ms = self._clock()
            proof_verdict = await self.reconcile_once()
            if proof_verdict != "clean":
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared: reconciliation is not clean.",
                    detail=f"The reconciliation verdict is '{proof_verdict}'.",
                )
        else:
            raise InventoryBaselineRefusedError(
                "Exposure hold cannot be cleared: no proof is registered for this hold reason.",
                detail=f"Unrecognized hold reason code '{hold.reason_code}'.",
            )

        async with self._intake_lock:
            entries = journal.read_entries()
            current_hold = derive.hold_state(entries)
            if (
                current_hold.active
                and current_hold.since_ms is not None
                and current_hold.since_ms > proof_observed_at_ms
            ):
                raise InventoryBaselineRefusedError(
                    "Exposure hold cannot be cleared: new evidence arrived after the proof was obtained.",
                    detail="A new hold condition was observed during the reconciliation; retry the clear.",
                )
            entries = await self._clear_hold_locked(
                journal=journal,
                account_id=account_id,
                operator=operator,
                reason=reason,
                verdict=proof_verdict,
            )
            return self._status_from(account_id, entries)

    async def _clear_hold_locked(
        self,
        *,
        journal: OrderJournal,
        account_id: str,
        operator: str,
        reason: str,
        verdict: ReconciliationVerdict | None = None,
    ) -> list[OrderJournalEntry]:
        entries = journal.read_entries()
        hold = derive.hold_state(entries)
        if hold.active:
            await journal.append_async(
                OrderJournalEntry(
                    kind=ClerkEntryKind.HOLD_CLEARED,
                    account_id=account_id,
                    operator=operator,
                    reason_code=hold.reason_code or UNEXPLAINED_ORDER_HOLD_CODE,
                    reason=reason,
                    recorded_at_ms=self._clock(),
                    verdict=verdict,
                )
            )
            logger.info(
                "alpaca clerk cleared the exposure hold",
                extra={
                    "action": "hold_cleared",
                    "account_id": account_id,
                    "operator": operator,
                    "reason_code": hold.reason_code,
                },
            )
            return journal.read_entries()
        logger.info(
            "alpaca clerk clear-hold was a no-op: no active hold",
            extra={"action": "hold_clear_noop", "account_id": account_id},
        )
        return entries

    async def record_inventory_baseline(
        self,
        *,
        operator: str,
        reason: str,
        strategy_instance_id: str | None = None,
    ) -> AccountClerkBrokerEvidenceBaseline:
        """Record a confirmed broker-inventory cutover and reconcile it.

        This recovery is deliberately operator initiated. It is available only
        for a ``missing_intent`` freeze, or for a stopped bot whose historical
        exposure remains attributed while the reconciled account is flat. No
        unresolved intents or working orders may exist. The intake lock spans
        the fresh broker reads and durable append, so no submit or lifecycle
        callback can race into the snapshot.

        Earlier order rows remain immutable audit history. The baseline starts
        account-level expected exposure from the freshly observed positions and
        never assigns those positions to a bot or manual namespace. It also
        retires pre-cutover instance exposure, without deleting fill history.
        """

        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            orders, positions = await asyncio.gather(
                self._read.list_orders(status="open", limit=500),
                self._read.list_positions(),
            )
            return await self._record_inventory_baseline_locked(
                journal=journal,
                account_id=account_id,
                operator=operator,
                reason=reason,
                strategy_instance_id=strategy_instance_id,
                orders=orders,
                positions=positions,
            )

    async def _record_inventory_baseline_locked(
        self,
        *,
        journal: OrderJournal,
        account_id: str,
        operator: str,
        reason: str,
        strategy_instance_id: str | None,
        orders: list[BrokerOrder],
        positions: list[BrokerPosition],
    ) -> AccountClerkBrokerEvidenceBaseline:
        if self._bot_running():
            raise InventoryBaselineRefusedError(
                "Inventory baseline recovery is blocked while a bot is running.",
                detail="Stop every running bot before adopting broker inventory.",
            )

        entries = journal.read_entries()
        latest = derive.latest_reconciliation(entries)
        missing_intent_recovery = latest is not None and latest.verdict == "missing_intent"
        stale_attribution_recovery = False
        if (
            latest is not None
            and latest.verdict == "clean"
            and strategy_instance_id is not None
            and not project_expected_account_exposure(entries)
        ):
            namespace = build_bot_order_namespace(strategy_instance_id)
            stale_attribution_recovery = bool(
                project_instance_exposure(entries, namespace=namespace)
            )
        if not missing_intent_recovery and not stale_attribution_recovery:
            raise InventoryBaselineRefusedError(
                "Inventory baseline recovery is not available.",
                detail=(
                    "Run reconciliation first. Recovery requires a current "
                    "missing-intent freeze or stale bot attribution on a "
                    "reconciled flat account."
                ),
            )
        if derive.unresolved_intents(entries):
            raise InventoryBaselineRefusedError(
                "Inventory baseline recovery is blocked by unresolved intents.",
                detail="Resolve every uncertain submit before adopting broker inventory.",
            )

        working_orders = [
            order
            for order in orders
            if order.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ]
        if working_orders:
            raise InventoryBaselineRefusedError(
                "Inventory baseline recovery is blocked by working orders.",
                detail=(
                    "Cancel or settle every working order, then reconcile before "
                    "recording a baseline."
                ),
            )
        if stale_attribution_recovery and any(
            position.quantity != 0 for position in positions
        ):
            raise InventoryBaselineRefusedError(
                "Inventory baseline recovery requires a freshly flat account.",
                detail=(
                    "Broker inventory changed after the clean reconciliation. "
                    "Reconcile again before retiring bot attribution."
                ),
            )

        observed_at_ms = self._clock()
        baseline = AccountClerkBrokerEvidenceBaseline(
            account_id=account_id,
            observed_at_ms=observed_at_ms,
            positions=tuple(
                AccountClerkPositionEvidence(
                    symbol=position.symbol.upper(),
                    signed_quantity=signed_broker_position_quantity(position),
                    evidence_observed_at_ms=position.observed_at_ms,
                )
                for position in positions
                if position.quantity != 0
            ),
        )
        await journal.append_async(
            OrderJournalEntry(
                kind=ClerkEntryKind.BROKER_EVIDENCE_BASELINE,
                account_id=account_id,
                operator=operator,
                reason=reason,
                recorded_at_ms=observed_at_ms,
                broker_evidence_baseline=baseline,
            )
        )
        plan = reconcile.plan(
            journal.read_entries(),
            [],
            positions,
            self._known_namespaces(journal),
            account_id=account_id,
            now_ms=observed_at_ms,
        )
        if plan.verdict != "clean":
            raise RuntimeError(
                "the durable inventory baseline did not reconcile to its broker snapshot"
            )
        await self._apply_reconcile_plan(journal, account_id, plan)
        logger.info(
            "alpaca clerk recorded an operator-confirmed inventory baseline",
            extra={
                "action": "broker_evidence_baseline",
                "account_id": account_id,
                "operator": operator,
                "position_count": len(baseline.positions),
            },
        )
        return baseline

    def _status_from(
        self, account_id: str, entries: list[OrderJournalEntry]
    ) -> ClerkStatus:
        """The clerk's ``ClerkStatus`` from a pre-read ledger (adds the intent scan)."""
        return derive.build_status(
            entries,
            broker_id=self.broker_id,
            account_id=account_id,
            outstanding_intents=len(derive.unresolved_intents(entries)),
            observed_at_ms=self._clock(),
            channel_healths=(
                list(self._stream_health.snapshot())
                if self._stream_health is not None
                else None
            ),
        )

    async def _set_hold(
        self,
        journal: OrderJournal,
        *,
        account_id: str,
        reason_code: str,
        reason: str,
    ) -> None:
        """Raise the hold; idempotent (never double-journal ``HOLD_SET``).

        Callers already hold the intake lock; a ``HOLD_SET`` is appended only
        when no explicit hold receipt is already active.
        """
        last_explicit_hold: ClerkEntryKind | None = None
        for entry in journal.read_entries():
            if entry.kind in (ClerkEntryKind.HOLD_SET, ClerkEntryKind.HOLD_CLEARED):
                last_explicit_hold = entry.kind
        # An unexplained observation itself derives a fail-closed hold, but it
        # still needs the companion HOLD_SET audit receipt. Only an un-cleared
        # explicit receipt makes this append redundant.
        if last_explicit_hold is ClerkEntryKind.HOLD_SET:
            return
        await journal.append_async(
            OrderJournalEntry(
                kind=ClerkEntryKind.HOLD_SET,
                account_id=account_id,
                reason_code=reason_code,
                reason=reason,
                recorded_at_ms=self._clock(),
            )
        )
        logger.warning(
            "alpaca clerk set an exposure hold; new submits refused",
            extra={
                "action": "hold_set",
                "account_id": account_id,
                "reason_code": reason_code,
            },
        )

    async def _submit_leg(
        self,
        operator: str,
        leg: BrokerOrderLeg,
        account_id: str,
        journal: OrderJournal,
        *,
        namespace: str | None = None,
        effect_operation_id: str | None = None,
    ) -> OrderLegResult:
        # Mint identity — fail closed. Two failure modes, both surfaced as a
        # typed failed leg with NO journal write and NO broker call:
        #   * a bad ``operator`` (space, '/', '\\', NUL, '.'/'..') → a
        #     ``ValueError`` from ``validate_strategy_instance_id``. The router
        #     boundary rejects this as a 422, but the clerk defends in depth so
        #     a bad value reaching it directly still fails typed, never a 500.
        #   * an ``order_ref`` over the length cap → ``OrderRefError``. A
        #     too-long id is a caller error, never truncated.
        # ``OrderRefError`` subclasses ``ValueError``, so the single ``ValueError``
        # catch covers both the bad-operator and over-cap paths.
        # ``namespace`` (bot-namespace path, S3) arrives prebuilt + validated by
        # ``build_bot_order_namespace``; when absent the manual scheme is minted
        # from ``operator`` exactly as before.
        intent_id = mint_intent_id()
        fallback_namespace = namespace if namespace is not None else f"manual/{operator}/v1"
        try:
            leg_namespace = (
                namespace if namespace is not None else build_manual_order_namespace(operator)
            )
            order_ref = build_order_ref(leg_namespace, intent_id)
        except ValueError as exc:
            logger.warning(
                "alpaca clerk rejected order identity",
                extra={"operator": operator, "symbol": leg.symbol},
            )
            return OrderLegResult(
                status="failed",
                order_ref=f"{fallback_namespace}:{intent_id}",
                intent_id=intent_id,
                error=OrderLegError(
                    message="Could not build a durable order identity for this leg.",
                    why=str(exc),
                ),
            )
        identity = LegIdentity(
            account_id, operator, intent_id, order_ref, leg, self._clock, effect_operation_id
        )

        # No journal → no order: record + fsync the intent BEFORE the broker call.
        await journal.append_async(identity.entry(ClerkEntryKind.INTENT_RECORDED))

        try:
            order = await self._trade.submit(leg, client_order_id=order_ref)
        except BrokerUnavailable as exc:
            # S5 UNCERTAIN: the response may have been lost (timeout / 5xx /
            # network), so the order MAY have landed. The intent is already
            # durable; journal the uncertainty, then resolve by asking the vendor
            # whether the order actually exists. A resolution that is itself
            # uncertain leaves the intent at ``submit_uncertain`` for startup
            # replay / a later sweep to finish — never a fabricated terminal.
            await journal.append_async(
                identity.entry(ClerkEntryKind.SUBMIT_UNCERTAIN, error=leg_error(exc))
            )
            logger.warning(
                "alpaca clerk submit outcome uncertain; resolving by client_order_id",
                extra={
                    "action": "submit_uncertain",
                    "account_id": account_id,
                    "order_ref": order_ref,
                    "symbol": leg.symbol,
                    "why": exc.detail,
                },
            )
            return await self._resolve_intent(
                identity, journal, terminal_on_absence=False
            )
        except BrokerError as exc:
            # Every other BrokerError (invalid 4xx, rejected 409, auth, rate
            # limit) is a DEFINITIVE failure — the order did not land.
            failure = leg_error(exc)
            await journal.append_async(
                identity.entry(ClerkEntryKind.SUBMIT_FAILED, error=failure)
            )
            return OrderLegResult(
                status="failed",
                order_ref=order_ref,
                intent_id=intent_id,
                error=failure,
            )

        await journal.append_async(
            identity.entry(ClerkEntryKind.SUBMIT_ACKED, order=order)
        )
        return OrderLegResult(
            status="acked", order_ref=order_ref, intent_id=intent_id, order=order
        )

    # ── S5 uncertain-submit resolution + startup replay (recovery.py) ───────

    async def recover(self) -> None:
        """Replay the journal and resolve every unfinished intent (S5).

        Delegates to :mod:`app.broker.alpaca.clerk.recovery`; see that module
        for the resolution contract and the absence-grace rules.
        """
        await recovery.recover(self)

    async def _resolve_intent(
        self,
        identity: LegIdentity,
        journal: OrderJournal,
        *,
        terminal_outcomes: dict[str, OrderLegResult] | None = None,
        terminal_on_absence: bool = True,
        uncertain_recorded_at_ms: int | None = None,
    ) -> OrderLegResult:
        """Resolve one intent by ``client_order_id`` (see ``recovery.resolve_intent``)."""
        return await recovery.resolve_intent(
            self,
            identity,
            journal,
            terminal_outcomes=terminal_outcomes,
            terminal_on_absence=terminal_on_absence,
            uncertain_recorded_at_ms=uncertain_recorded_at_ms,
        )

    # ── S6 reconciliation sweep ──────────────────────────────────────────────

    async def reconcile_once(self) -> ReconciliationVerdict:
        """Run one reconciliation pass; journal a named verdict and return it.

        First replay unresolved S5 submits so a long-running process does not
        leave terminal outcomes stranded until restart. Then read Alpaca
        *without* the intake lock so cancels remain reachable during a slow
        sweep, and reacquire only to derive and append the latest durable
        reconciliation result.
        """
        return (await self._reconcile_with_proof()).verdict

    async def prove_instance_custody(
        self, strategy_instance_id: str
    ) -> InstanceCustodyProof:
        """Read fresh broker truth and return an exact instance custody proof."""
        proof = (await self._reconcile_with_proof(
            strategy_instance_id=strategy_instance_id
        )).proof
        assert proof is not None
        return proof

    async def custody_snapshot(
        self, strategy_instance_id: str
    ) -> ClerkCustodySnapshot:
        """Return one fresh, typed Clerk custody answer for an instance."""
        result = await self._reconcile_with_proof(
            strategy_instance_id=strategy_instance_id
        )
        return project_custody_snapshot(
            broker=self.broker_id,
            clerk_generation=self._clerk_generation,
            result=result,
            observed_at_ms=self._clock(),
        )

    @asynccontextmanager
    async def start_admission_snapshot(
        self, strategy_instance_id: str
    ) -> AsyncIterator[ClerkCustodySnapshot]:
        """Yield one custody snapshot while its exact journal cut stays fenced."""
        for _attempt in range(3):
            snapshot = await self.custody_snapshot(strategy_instance_id)
            await self._intake_lock.acquire()
            try:
                account_id, journal = await self._ensure_journal()
                if (
                    account_id == snapshot.account_id
                    and len(journal.read_entries()) == snapshot.journal_sequence
                ):
                    yield snapshot
                    return
            finally:
                self._intake_lock.release()
        raise ClerkAdmissionSnapshotChangedError(
            "Clerk custody evidence kept changing before Start could be fenced."
        )

    async def _reconcile_with_proof(
        self, *, strategy_instance_id: str | None = None
    ) -> CustodyReconciliationResult:
        """Shared fresh reconciliation pass with an optional instance proof."""
        await self.recover()

        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()

        try:
            # Independent reads; each is a full Alpaca REST round-trip
            # (~5-15s against the paper API), so run them concurrently.
            orders, positions = await asyncio.gather(
                self._read.list_orders(status="all", limit=500),
                self._read.list_positions(),
            )
        except BrokerError as exc:
            logger.warning(
                "alpaca clerk reconciliation could not read the broker; stale",
                extra={
                    "action": "reconcile_stale",
                    "account_id": account_id,
                    "why": exc.detail,
                },
            )
            async with self._intake_lock:
                _, journal = await self._ensure_journal()
                observed_at_ms = self._clock()
                plan = reconcile.plan_stale(
                    journal.read_entries(),
                    account_id=account_id,
                    now_ms=observed_at_ms,
                )
                verdict = await self._apply_reconcile_plan(journal, account_id, plan)
                final_entries = journal.read_entries()
                proof = project_instance_custody_proof(
                    strategy_instance_id,
                    account_id=account_id,
                    entries=final_entries,
                    working_orders=[],
                    verdict=verdict,
                    observed_at_ms=observed_at_ms,
                )
                return CustodyReconciliationResult(
                    verdict=verdict,
                    proof=proof,
                    entries=tuple(final_entries),
                    broker_facts_complete=False,
                )

        working_orders = [
            order
            for order in orders
            if order.status.lower() not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
        ]
        async with self._intake_lock:
            _, journal = await self._ensure_journal()
            # The current ledger is authoritative because submits/cancels can
            # have completed while Alpaca was being read.
            current_entries = journal.read_entries()
            observed_at_ms = self._clock()
            plan = reconcile.plan(
                current_entries,
                working_orders,
                positions,
                self._known_namespaces(journal),
                account_id=account_id,
                now_ms=observed_at_ms,
            )
            # A non-clean verdict is operationally notable (WARNING); clean is INFO.
            logger.log(
                logging.INFO if plan.verdict == "clean" else logging.WARNING,
                "alpaca clerk reconciliation: %s",
                plan.verdict,
                extra={
                    "action": "reconcile",
                    "account_id": account_id,
                    "verdict": plan.verdict,
                    "new_unexplained": plan.new_unexplained_count,
                },
            )
            verdict = await self._apply_reconcile_plan(journal, account_id, plan)
            final_entries = journal.read_entries()
            proof = project_instance_custody_proof(
                strategy_instance_id,
                account_id=account_id,
                entries=final_entries,
                working_orders=working_orders,
                verdict=verdict,
                observed_at_ms=observed_at_ms,
            )
            return CustodyReconciliationResult(
                verdict=verdict,
                proof=proof,
                entries=tuple(final_entries),
                broker_facts_complete=(
                    verdict == "clean"
                    and not derive.has_inflight_position_evidence(
                        current_entries, positions
                    )
                ),
            )

    async def _apply_reconcile_plan(
        self, journal: OrderJournal, account_id: str, plan: reconcile.ReconcilePlan
    ) -> ReconciliationVerdict:
        """Apply a pure :class:`reconcile.ReconcilePlan` under the intake lock:
        append its (deduped, verdict-on-change) entries, advance the counter by
        the *new*-unexplained count only, and raise the hold when it calls for one
        (``_set_hold`` is idempotent, so a persistent foreign order does not
        re-journal HOLD_SET)."""
        for entry in plan.entries_to_append:
            await journal.append_async(entry)
        self._unexplained_order_count += plan.new_unexplained_count
        if plan.set_hold:
            await self._set_hold(
                journal,
                account_id=account_id,
                reason_code=UNEXPLAINED_ORDER_HOLD_CODE,
                reason=(
                    "The reconciliation sweep found an order this account did not "
                    "submit at Alpaca. Submission is paused until an operator "
                    "confirms the account is safe."
                ),
            )
        return plan.verdict

_clerk: AlpacaClerk | None = None

def get_alpaca_clerk() -> AlpacaClerk | None:
    """Return the process-wide Alpaca clerk, or ``None`` when unconfigured.

    The clerk is installed in the app lifespan only when Alpaca keys are
    present; a ``None`` return means the router surfaces "not configured".
    """
    return _clerk

def set_alpaca_clerk(clerk: AlpacaClerk | None) -> None:
    """Install (or clear) the process-wide Alpaca clerk — lifespan wiring."""
    global _clerk
    _clerk = clerk


def reset_alpaca_clerk_for_testing() -> None:
    """Drop the process-wide clerk so a test starts clean."""
    global _clerk
    _clerk = None
