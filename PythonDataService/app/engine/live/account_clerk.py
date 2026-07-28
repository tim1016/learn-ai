"""Account Clerk durable intake and recorded-receipt journal.

The clerk accepts the existing ``AccountOwnerSubmitIntent`` wire model,
validates its *individual* account instance binding, and writes it through a
durable inbox into the account journal.  It is the only component that may
then contact the paper broker; acknowledgement, callback, and reconciliation
receipts stay in the same serial account journal.

The inbox is the crash boundary.  A process can fail after the inbox fsync and
before the journal fsync.  The next intake replays that inbox row into the
journal before accepting new work, so an accepted intent cannot be silently
lost.  The journal is the canonical receipt #1 ledger and is replayable by a
new clerk process.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import inspect
import logging
import os
import signal
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountFreezeEvidence,
    account_artifacts_root,
    append_account_event,
    read_account_clerk_generation,
    read_account_freeze,
    write_account_freeze,
)
from app.engine.live.account_clerk_journal import (
    ACCOUNT_CLERK_INBOX_FILENAME,
    ACCOUNT_CLERK_JOURNAL_FILENAME,
    AccountClerkBrokerAckReceipt,
    AccountClerkBrokerEventReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkEmergencyFlattenReceipt,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournal,
    AccountClerkJournalCorruptError,
    AccountClerkJournalEntry,
    AccountClerkOperatorAdjustment,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    _now_ms,
    account_clerk_inbox_path,
    account_clerk_journal_path,
    read_account_clerk_durability_spine,
    read_account_clerk_inbox,
    read_account_clerk_journal,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkAsyncCustodyConfig,
    AccountClerkAsyncCustodyHealth,
    AccountClerkCustodyStatus,
    AccountClerkEmergencyAuthorization,
)
from app.engine.live.account_clerk_lease import AccountClerkLeaseWriter
from app.engine.live.account_clerk_operations import (
    ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S,
    AccountClerkCancelNamespaceUncertainError,
    AccountClerkEmergencyFlattenIncompleteError,
    AccountClerkGenerationFencedError,
    AccountClerkOperationCoordinator,
    AccountClerkOperationDependencies,
    AccountClerkReconciliationOutcome,
)
from app.engine.live.account_owner import (
    MANUAL_OPERATOR_RUN_ID,
    MANUAL_OPERATOR_STRATEGY_INSTANCE_ID,
    MANUAL_ORDER_INTENT_KIND,
    AccountOwnerSubmitIntent,
)
from app.engine.live.account_owner_fence import (
    AccountClerkWriteFenceError,
    account_clerk_write_grant,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    BindingRetirementFoldResult,
    account_binding_ledger_parity,
    fold_account_binding_retirements,
    latest_account_instance_binding,
    pending_account_binding_retirements,
    read_account_instance_registry,
    write_account_instance_binding,
)
from app.engine.live.broker_callbacks import broker_callback_idempotency_key
from app.engine.live.daemon_auth import CLERK_HOST_BINDING_CAPABILITY_ENV
from app.engine.live.journal_recovery_state import (
    JournalRecoveryStateCorruptError,
    assess_journal_recovery_fence,
    journal_recovery_admission_lock,
)
from app.engine.live.order_identity import (
    build_bot_order_namespace,
    build_manual_order_namespace,
    emergency_flatten_strategy_instance_id,
)
from app.services.bot_deletion import (
    BotDeletionCorruptError,
    bot_lifecycle_operation_fence,
    bot_retirement_is_pending,
)
from app.services.clerk_transaction_projection import project_account_journal_best_effort
from app.utils.advisory_lock import advisory_file_lock

if TYPE_CHECKING:
    from app.engine.live.account_clerk_rpc import AccountClerkRpcClient, AccountClerkRpcServer

ACCOUNT_CLERK_AUTHORITY_LOCK_TARGET_FILENAME = "clerk_authority"
# The Clerk must release its account-wide intake lock before the bot-side RPC
# budget expires.  A timed-out broker write is ambiguous, so its durable
# ``broker_uncertain`` row is deliberately left for reconciliation.
_BROKER_SUBMIT_TIMEOUT_S = 25.0
# At most two custody lanes can each admit 256 durable A0 intents. Notification
# delivery is best-effort, so it is deliberately bounded below that retained
# ledger capacity and never becomes a second source of backpressure.
_ASYNC_CUSTODY_NOTIFICATION_QUEUE_CAPACITY = 256
_ASYNC_CUSTODY_NOTIFICATION_TIMEOUT_S = 1.0
# Production defaults deliberately bound account-wide unacknowledged work.
# The per-strategy-instance admission fence below is stricter for normal
# entries; this is the account-level backpressure limit that protects the
# Clerk process if many independent strategies are active at once.
_PRODUCTION_ASYNC_CUSTODY_ENTRY_CAPACITY = 64
_PRODUCTION_ASYNC_CUSTODY_RISK_REDUCING_CAPACITY = 32
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _journal_recovery_broker_write_lock(
    artifacts_root: Path,
    account_id: str,
) -> AsyncIterator[None]:
    """Hold recovery admission off-loop across a Clerk broker invocation."""

    lock = journal_recovery_admission_lock(artifacts_root, account_id)
    await asyncio.to_thread(lock.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(lock.__exit__, None, None, None)
_RECOVERY_PERM_ID_WAIT_S = 2.0


class AccountClerk:
    """Per-account concurrent intake backed by one serialized durable journal.

    ``broker`` is the Clerk-owned paper broker boundary.  Tests may omit it
    when exercising only durable journal behavior.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        broker: object | None = None,
        clerk_generation: int | None = None,
        durable_generation_provider: Callable[[], int | None] | None = None,
        on_generation_fenced: Callable[[], None] | None = None,
        now_ms: Callable[[], int] | None = None,
        host_binding_capability: str | None = None,
        async_custody_config: AccountClerkAsyncCustodyConfig | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._clerk_generation = clerk_generation
        self._durable_generation_provider = durable_generation_provider
        self._on_generation_fenced = on_generation_fenced
        self._now_ms = now_ms if now_ms is not None else _now_ms
        self._host_binding_capability = host_binding_capability
        self._intake_lock = asyncio.Lock()
        self._broker_write_lock = asyncio.Lock()
        self._journal = AccountClerkJournal(
            artifacts_root=artifacts_root,
            account_id=account_id,
            now_ms=self._now_ms,
        )
        # Kept as a direct alias for focused compatibility tests that model a
        # process restart by clearing only volatile callback attribution.
        self._intents_by_order_ref = self._journal.intents_by_order_ref
        self._normal_submit_intake_reason: str | None = None
        self._cancel_operation_lock = asyncio.Lock()
        self._callback_drain: Callable[[], Awaitable[None]] | None = None
        self._operations = AccountClerkOperationCoordinator(
            AccountClerkOperationDependencies(
                artifacts_root=artifacts_root,
                account_id=account_id,
                broker=broker,
                journal=self._journal,
                intake_lock=self._intake_lock,
                cancel_operation_lock=self._cancel_operation_lock,
                callback_drain=lambda: self._callback_drain,
                record_intent_locked=self._record_intent_locked,
                reject=self._reject,
                require_paper_broker=self._require_paper_broker,
                require_rpc_write_intake=self._require_rpc_write_intake,
                cancel_namespace_open_orders=lambda namespace, before_broker_write: (
                    self._cancel_namespace_open_orders(
                        namespace,
                        before_broker_write=before_broker_write,
                    )
                ),
                cancel_order_refs_open_orders=lambda order_refs, before_broker_write: (
                    self._cancel_order_refs_open_orders(
                        order_refs,
                        before_broker_write=before_broker_write,
                    )
                ),
                place_under_clerk_grant=lambda spec, wait_for_perm_id: (
                    self._place_under_clerk_grant(spec, wait_for_perm_id=wait_for_perm_id)
                ),
                retry_recorded_intent_locked=self._retry_recorded_intent_locked,
                now_ms=self._now_ms,
            )
        )
        self._rpc_write_intake_closed = False
        self._event_stream_down_recorded = False
        self._projection_task: asyncio.Task[None] | None = None
        self._projection_requested = False
        # The asynchronous path is an explicit, disabled-by-default shadow
        # capability. Existing callers keep the synchronous submit operation.
        self._async_custody_config = async_custody_config
        self._async_custody_admission_lock = asyncio.Lock()
        self._async_custody_queues: dict[str, asyncio.Queue[AccountOwnerSubmitIntent]] = (
            {
                "entry": asyncio.Queue(maxsize=async_custody_config.entry_capacity),
                "risk_reducing": asyncio.Queue(maxsize=async_custody_config.risk_reducing_capacity),
            }
            if async_custody_config is not None
            else {}
        )
        self._async_custody_workers: list[asyncio.Task[None]] = []
        self._async_custody_enqueued_ids: set[str] = set()
        self._async_custody_started = False
        self._async_custody_stopping = False
        self._custody_notification_sink: Callable[[AccountClerkCustodyStatus], Awaitable[None]] | None = None
        self._custody_notification_queue: asyncio.Queue[AccountOwnerSubmitIntent] = asyncio.Queue(
            maxsize=_ASYNC_CUSTODY_NOTIFICATION_QUEUE_CAPACITY
        )
        self._custody_notification_pending_ids: set[str] = set()
        self._custody_notification_worker: asyncio.Task[None] | None = None
        self._custody_notification_observer: asyncio.Task[None] | None = None

    @property
    def recovery_flatten_in_progress(self) -> bool:
        """Whether a namespace recovery owns the Clerk's submit lane."""

        return self._operations.recovery_flatten_in_progress

    def set_callback_drain(self, drain: Callable[[], Awaitable[None]]) -> None:
        """Install the RPC callback queue drain used by recovery flatten.

        Direct Clerk tests and non-RPC callers have no callback relay, so they
        intentionally leave this unset.  The production RPC server supplies a
        drain after it has established the sole broker callback sink.
        """

        self._callback_drain = drain

    def set_custody_notification_sink(
        self,
        sink: Callable[[AccountClerkCustodyStatus], Awaitable[None]],
    ) -> None:
        """Install the optional originator-notification seam for A0–A3 folds.

        The journal remains authoritative if a consumer is disconnected or its
        notification handler fails; Slice 3 will attach bot-side replay to this
        seam rather than create another ledger.
        """

        self._custody_notification_sink = sink
        self._ensure_custody_notification_worker()

    @property
    def async_custody_enabled(self) -> bool:
        return self._async_custody_config is not None

    async def start_async_custody(self) -> None:
        """Start disabled-by-default A0 workers after Clerk recovery completes."""

        if self._async_custody_config is None or self._async_custody_started:
            return
        self._async_custody_started = True
        self._async_custody_stopping = False
        self._ensure_custody_notification_worker()
        # A process restart is an explicit recovery boundary. Entry work that
        # never crossed A1 cannot silently resume; risk-reducing work stays
        # visible for an explicit policy decision instead of being auto-voided.
        pending = await asyncio.to_thread(self._journal.queued_async_custody_intents)
        for intent, lane in pending:
            if lane == "entry":
                await asyncio.to_thread(
                    self._journal.append_custody_expired_before_submit,
                    intent,
                )
            else:
                await asyncio.to_thread(
                    self._journal.append_custody_recovery_action_required,
                    intent,
                )
            self._schedule_custody_notification(intent)
        self._async_custody_workers = [
            asyncio.create_task(
                self._run_async_custody_lane("entry"),
                name="account-clerk-async-custody-entry",
            ),
            asyncio.create_task(
                self._run_async_custody_lane("risk_reducing"),
                name="account-clerk-async-custody-risk-reducing",
            ),
        ]

    async def stop_async_custody(self) -> None:
        """Stop workers only after A1 work is resolved or durably uncertain."""

        if self._async_custody_workers:
            self._async_custody_stopping = True
            # A worker may be in a bounded broker await. Let it finish under
            # the Clerk grant before cancellation can touch a queued-only job.
            async with self._broker_write_lock:
                pass
            for worker in self._async_custody_workers:
                worker.cancel()
            for worker in self._async_custody_workers:
                with suppress(asyncio.CancelledError):
                    await worker
            self._async_custody_workers = []
        if self._custody_notification_worker is not None:
            self._custody_notification_worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._custody_notification_worker
            self._custody_notification_worker = None
        if self._custody_notification_observer is not None:
            # An observer is not part of the Clerk authority path. Its task
            # may ignore cancellation, so never let it block account shutdown.
            self._custody_notification_observer.cancel()

    def async_custody_health(self) -> AccountClerkAsyncCustodyHealth:
        """Return the bounded lane depths exposed by the versioned read API."""

        if self._async_custody_config is None:
            return AccountClerkAsyncCustodyHealth(
                enabled=False,
                entry_depth=0,
                entry_capacity=0,
                risk_reducing_depth=0,
                risk_reducing_capacity=0,
            )
        return AccountClerkAsyncCustodyHealth(
            enabled=True,
            entry_depth=self._async_custody_queues["entry"].qsize(),
            entry_capacity=self._async_custody_config.entry_capacity,
            risk_reducing_depth=self._async_custody_queues["risk_reducing"].qsize(),
            risk_reducing_capacity=self._async_custody_config.risk_reducing_capacity,
        )

    async def submit_async_custody(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        clerk_request_received_at_ms: int | None = None,
        lane: Literal["entry", "risk_reducing"] = "entry",
    ) -> tuple[AccountClerkRecordedReceipt, AccountClerkCustodyStatus]:
        """Transfer custody at A0 and resolve broker work in a Clerk task.

        ``lane`` exists for trusted Clerk-owned callers only. The public RPC
        always admits the entry lane until Slice 7 supplies server-derived
        effect classification for risk-reducing requests.
        """

        if self._async_custody_config is None:
            self._reject(intent, "CLERK_ASYNC_CUSTODY_DISABLED")
        if not self._async_custody_started or self._async_custody_stopping:
            self._reject(intent, "CLERK_ASYNC_CUSTODY_UNAVAILABLE")
        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind in {"RECOVERY_FLATTEN", "CANCEL_NAMESPACE"}:
            self._reject(intent, "CLERK_ASYNC_CUSTODY_INTENT_KIND_UNSUPPORTED")
        try:
            self._require_paper_broker()
            IbkrOrderSpec.model_validate(intent.order_spec)
        except (RuntimeError, ValueError) as exc:
            # Specification and paper-mode checks are deterministic. They
            # must fail before durable A0, never strand an accepted request in
            # a worker queue with no possible broker write.
            self._reject(intent, f"CLERK_ASYNC_CUSTODY_QUALIFICATION_FAILED:{type(exc).__name__}")

        async with self._async_custody_admission_lock, self._intake_lock:
            await self._require_normal_submit_intake(intent)
            existing_identity = await asyncio.to_thread(self._journal.intent_for_id, intent.intent_id)
            if existing_identity is not None and existing_identity != intent:
                self._reject(intent, "CLERK_INTENT_ID_COLLISION")
            existing = await asyncio.to_thread(self._journal.custody_status_for_intent, intent)
            if existing is None and existing_identity is not None:
                # An ordinary synchronous receipt cannot be retroactively
                # converted into async custody: its original A0 did not carry
                # a lane or the corresponding restart policy.
                self._reject(intent, "CLERK_ASYNC_CUSTODY_REQUIRES_NEW_INTENT")
            is_existing_queued = existing is not None and existing.lifecycle_state == "queued"
            if existing is None:
                if lane == "entry":
                    pending_entry = await asyncio.to_thread(
                        self._journal.nonterminal_async_entry_for_strategy_instance,
                        strategy_instance_id=intent.strategy_instance_id,
                        excluding_intent_id=intent.intent_id,
                    )
                    if pending_entry is not None:
                        self._reject(
                            intent,
                            "CLERK_ASYNC_ENTRY_PENDING",
                            pending_intent_id=pending_entry.intent_id,
                            pending_order_ref=pending_entry.order_ref,
                        )
                self._require_async_custody_capacity(intent, lane)
                intake_admitted_at_ms = self._now_ms()
                recorded = await asyncio.to_thread(
                    self._record_intent_locked,
                    intent,
                    clerk_request_received_at_ms=clerk_request_received_at_ms,
                    clerk_intake_admitted_at_ms=intake_admitted_at_ms,
                    async_custody_lane=lane,
                )
            else:
                recorded = existing.recorded
                if existing.lifecycle_state == "recorded":
                    self._reject(intent, "CLERK_ASYNC_CUSTODY_REQUIRES_NEW_INTENT")
                elif existing.lifecycle_state != "queued":
                    return recorded, existing
                else:
                    assert existing.lane is not None
                    lane = existing.lane

            status = await asyncio.to_thread(self._journal.custody_status_for_intent, intent)
            if status is None:
                raise RuntimeError("CLERK_ASYNC_CUSTODY_STATUS_MISSING")
            if status.lifecycle_state == "queued":
                self._enqueue_async_custody(intent, lane, reject_if_full=not is_existing_queued)
        self._schedule_custody_notification(intent)
        return recorded, status

    async def async_custody_status(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCustodyStatus | None:
        """Read one idempotent durable custody fold by immutable intent identity."""

        return await asyncio.to_thread(self._journal.custody_status_for_intent, intent)

    def _require_async_custody_capacity(
        self,
        intent: AccountOwnerSubmitIntent,
        lane: Literal["entry", "risk_reducing"],
    ) -> None:
        queue = self._async_custody_queues[lane]
        if not queue.full():
            return
        reason = "CLERK_ASYNC_ENTRY_QUEUE_FULL" if lane == "entry" else "CLERK_ASYNC_RISK_QUEUE_FULL"
        self._reject(intent, reason)

    def _enqueue_async_custody(
        self,
        intent: AccountOwnerSubmitIntent,
        lane: Literal["entry", "risk_reducing"],
        *,
        reject_if_full: bool,
    ) -> bool:
        if intent.intent_id in self._async_custody_enqueued_ids:
            return True
        if self._async_custody_queues[lane].full():
            if reject_if_full:
                self._require_async_custody_capacity(intent, lane)
            return False
        self._async_custody_queues[lane].put_nowait(intent)
        self._async_custody_enqueued_ids.add(intent.intent_id)
        return True

    async def _run_async_custody_lane(self, lane: Literal["entry", "risk_reducing"]) -> None:
        queue = self._async_custody_queues[lane]
        while True:
            intent = await queue.get()
            try:
                if not self._async_custody_stopping:
                    await self._advance_async_custody_intent(intent)
            except asyncio.CancelledError:
                raise
            except AccountClerkIntentRejected as exc:
                await self._hold_async_custody_before_a1(intent, reason=exc.reason)
                logger.info(
                    "asynchronous custody work durably held before broker write",
                    extra={
                        "account_id": intent.account_id,
                        "intent_id": intent.intent_id,
                        "reason": exc.reason,
                    },
                )
            except Exception as exc:
                await self._hold_async_custody_before_a1(
                    intent,
                    reason=f"ASYNC_CUSTODY_PRE_A1_FAILURE:{type(exc).__name__}",
                )
                logger.exception(
                    "asynchronous custody worker failed before advancing an intent",
                    extra={"account_id": intent.account_id, "intent_id": intent.intent_id},
                )
            finally:
                self._async_custody_enqueued_ids.discard(intent.intent_id)
                queue.task_done()

    async def _advance_async_custody_intent(self, intent: AccountOwnerSubmitIntent) -> None:
        """Cross A1 under the Clerk grant, then retain uncertainty durably."""

        async with self._broker_write_lock:
            async with self._intake_lock:
                await self._require_normal_submit_intake(intent)
                status = await asyncio.to_thread(self._journal.custody_status_for_intent, intent)
                if status is None or status.lifecycle_state != "queued":
                    return
                self._require_paper_broker()
                spec = IbkrOrderSpec.model_validate(intent.order_spec)
                # This durable A1 receipt is written before the broker call and
                # after the durable A0 row that the caller can query by identity.
                await asyncio.to_thread(self._journal.append_broker_submitting, intent)
            self._schedule_custody_notification(intent)
            try:
                ack = await self._place_under_clerk_grant(spec)
            except Exception as exc:
                await asyncio.to_thread(self._journal.append_broker_uncertain, intent, exc)
                self._schedule_custody_notification(intent)
                return
            receipt = await asyncio.to_thread(self._journal.append_broker_ack, intent, ack)
            await self._publish_manual_order_ack_event(intent, receipt)
            self._schedule_custody_notification(intent)

    async def _hold_async_custody_before_a1(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        reason: str,
    ) -> None:
        """Persist a visible A0 hold when dynamic policy blocks the worker."""

        try:
            await asyncio.to_thread(
                self._journal.append_custody_submission_hold,
                intent,
                reason=reason,
            )
        except Exception:
            logger.exception(
                "failed to persist asynchronous custody pre-submit hold",
                extra={"account_id": intent.account_id, "intent_id": intent.intent_id},
            )
            raise
        self._schedule_custody_notification(intent)

    def _schedule_custody_notification(self, intent: AccountOwnerSubmitIntent) -> None:
        """Coalesce best-effort observer work without delaying Clerk custody."""

        if self._custody_notification_sink is None:
            return
        self._ensure_custody_notification_worker()
        if intent.intent_id in self._custody_notification_pending_ids:
            return
        try:
            self._custody_notification_queue.put_nowait(intent)
        except asyncio.QueueFull:
            logger.warning(
                "asynchronous custody notification queue is full; durable status remains queryable",
                extra={"account_id": intent.account_id, "intent_id": intent.intent_id},
            )
            return
        self._custody_notification_pending_ids.add(intent.intent_id)

    def _ensure_custody_notification_worker(self) -> None:
        """Start the single bounded observer worker only for active custody."""

        if (
            self._custody_notification_sink is None
            or not self._async_custody_started
            or self._custody_notification_stopping()
            or self._custody_notification_worker is not None
        ):
            return
        self._custody_notification_worker = asyncio.create_task(
            self._run_custody_notification_worker(),
            name="account-clerk-custody-notification",
        )

    def _custody_notification_stopping(self) -> bool:
        return self._async_custody_stopping

    async def _run_custody_notification_worker(self) -> None:
        """Deliver one coalesced durable fold at a time with a strict budget."""

        while True:
            intent = await self._custody_notification_queue.get()
            try:
                await self._notify_custody_status(intent)
            finally:
                self._custody_notification_pending_ids.discard(intent.intent_id)
                self._custody_notification_queue.task_done()

    async def _notify_custody_status(self, intent: AccountOwnerSubmitIntent) -> None:
        """Best-effort observer delivery after durable state is already available."""

        if self._custody_notification_sink is None or self._custody_notification_observer is not None:
            return
        status = await asyncio.to_thread(self._journal.custody_status_for_intent, intent)
        if status is None:
            return
        observer = asyncio.create_task(
            self._custody_notification_sink(status),
            name=f"account-clerk-custody-observer:{intent.intent_id}",
        )
        self._custody_notification_observer = observer
        observer.add_done_callback(self._complete_custody_notification_observer)
        done, _ = await asyncio.wait({observer}, timeout=_ASYNC_CUSTODY_NOTIFICATION_TIMEOUT_S)
        if observer not in done:
            logger.warning(
                "asynchronous custody observer exceeded its delivery budget; durable status remains queryable",
                extra={"account_id": intent.account_id, "intent_id": intent.intent_id},
            )
            return
        try:
            observer.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "asynchronous custody notification sink failed",
                extra={"account_id": intent.account_id, "intent_id": intent.intent_id},
            )

    def _complete_custody_notification_observer(self, task: asyncio.Task[None]) -> None:
        """Release the single observer slot and consume detached task failures."""

        if self._custody_notification_observer is task:
            self._custody_notification_observer = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            # The awaited path logs immediate failures. This branch consumes an
            # observer detached after the strict timeout without duplicating
            # operator noise on an optional notification channel.
            logger.debug("detached asynchronous custody observer completed with an error", exc_info=True)

    async def record_intent(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        clerk_request_received_at_ms: int | None = None,
    ) -> AccountClerkRecordedReceipt:
        """Validate, durably record, and acknowledge one intent without I/O to IBKR."""

        async with self._intake_lock:
            if clerk_request_received_at_ms is None:
                return await asyncio.to_thread(self._record_intent_locked, intent)
            clerk_intake_admitted_at_ms = self._now_ms()
            return await asyncio.to_thread(
                self._record_intent_locked,
                intent,
                clerk_request_received_at_ms=clerk_request_received_at_ms,
                clerk_intake_admitted_at_ms=clerk_intake_admitted_at_ms,
            )

    async def emergency_flatten_account(
        self,
        *,
        operation_id: str,
        authorization_id: str,
        host_capability: str,
    ) -> AccountClerkEmergencyFlattenReceipt:
        """Run the operator-confirmed account emergency through the held Clerk session."""

        self._require_host_capability(host_capability)
        if self._normal_submit_intake_reason not in (
            None,
            "CLERK_EMERGENCY_FLATTEN_PREPARED",
            "CLERK_EMERGENCY_FLATTEN_IN_PROGRESS",
            "CLERK_EMERGENCY_RECOVERY_REQUIRED",
        ):
            raise RuntimeError("CLERK_EMERGENCY_FLATTEN_INTAKE_UNAVAILABLE")
        self._normal_submit_intake_reason = "CLERK_EMERGENCY_FLATTEN_IN_PROGRESS"
        try:
            receipt = await self._operations.emergency_flatten_account(
                operation_id=operation_id,
                authorization_id=authorization_id,
            )
        except AccountClerkEmergencyFlattenIncompleteError:
            # Do not reopen an account whose final broker snapshot was not flat.
            raise
        else:
            self._normal_submit_intake_reason = None
            return receipt

    async def prepare_emergency_flatten(
        self,
        *,
        operation_id: str,
        authorization_id: str,
        host_capability: str,
    ) -> None:
        """Durably close intake before the daemon pauses on-duty bot processes."""

        self._require_host_capability(host_capability)
        if self._normal_submit_intake_reason not in (
            None,
            "CLERK_EMERGENCY_FLATTEN_PREPARED",
            "CLERK_EMERGENCY_RECOVERY_REQUIRED",
        ):
            raise RuntimeError("CLERK_EMERGENCY_FLATTEN_INTAKE_UNAVAILABLE")
        self._normal_submit_intake_reason = "CLERK_EMERGENCY_FLATTEN_PREPARED"
        await self._operations.prepare_emergency_flatten(
            operation_id=operation_id,
            authorization_id=authorization_id,
        )

    async def mark_emergency_bots_paused(
        self,
        *,
        operation_id: str,
        authorization_id: str,
        host_capability: str,
    ) -> None:
        """Record host-proven bot quiescence before an emergency broker action."""

        self._require_host_capability(host_capability)
        await self._operations.mark_emergency_bots_paused(
            operation_id=operation_id,
            authorization_id=authorization_id,
        )

    async def mark_emergency_requires_reconciliation(
        self,
        *,
        operation_id: str,
        reason: str,
        host_capability: str,
    ) -> None:
        """Persist a host-side pause failure without allowing a broker write."""

        self._require_host_capability(host_capability)
        await self._operations.mark_emergency_requires_reconciliation(
            operation_id=operation_id,
            reason=reason,
        )

    async def authorize_emergency_flatten(
        self,
        *,
        operation_id: str,
        confirmation_token: Literal["FLATTEN"],
        reconciliation_evidence_version: str,
        no_exact_recovery_candidate: Literal[True],
    ) -> AccountClerkEmergencyAuthorization:
        """Issue the sole receipt that may authorize an account emergency write."""

        return await self._operations.authorize_emergency_flatten(
            operation_id=operation_id,
            confirmation_token=confirmation_token,
            reconciliation_evidence_version=reconciliation_evidence_version,
            no_exact_recovery_candidate=no_exact_recovery_candidate,
        )

    async def submit_intent(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        clerk_request_received_at_ms: int | None = None,
    ) -> tuple[AccountClerkRecordedReceipt, AccountClerkBrokerAckReceipt]:
        """Serially record then submit one paper intent through the clerk broker.

        The durable recorded receipt is always produced before the broker call.
        Repeating an acknowledged intent returns its existing receipt #2 rather
        than issuing a duplicate placement.
        """

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind == "RECOVERY_FLATTEN":
            raise AccountClerkIntentRejected(
                reason="CLERK_RECOVERY_OPERATION_REQUIRED",
                diagnostics={"intent_id": intent.intent_id, "order_ref": intent.order_ref},
            )
        if intent.intent_kind == "CANCEL_NAMESPACE":
            raise AccountClerkIntentRejected(
                reason="CLERK_CANCEL_NAMESPACE_OPERATION_REQUIRED",
                diagnostics={"intent_id": intent.intent_id, "order_ref": intent.order_ref},
            )
        await self._require_normal_submit_intake(intent)
        async with self._broker_write_lock, self._intake_lock:
            # The fence spans identity validation, journal admission, and the
            # broker write. A retirement cannot turn PENDING between those
            # steps and let an already-admitted bot place one more order.
            # Manual tickets are account-owned, so the Clerk's account-wide
            # intake lock is their sole serialization boundary.
            operation_fence = (
                nullcontext()
                if intent.intent_kind == MANUAL_ORDER_INTENT_KIND
                else bot_lifecycle_operation_fence(self._artifacts_root, intent.strategy_instance_id)
            )
            with operation_fence:
                await self._require_normal_submit_intake(intent)
                if clerk_request_received_at_ms is None:
                    recorded = await asyncio.to_thread(self._record_intent_locked, intent)
                else:
                    clerk_intake_admitted_at_ms = self._now_ms()
                    recorded = await asyncio.to_thread(
                        self._record_intent_locked,
                        intent,
                        clerk_request_received_at_ms=clerk_request_received_at_ms,
                        clerk_intake_admitted_at_ms=clerk_intake_admitted_at_ms,
                    )
                existing_ack = await asyncio.to_thread(self._journal.ack_for_intent, intent)
                if existing_ack is not None:
                    await self._publish_manual_order_ack_event(intent, existing_ack)
                    return recorded, existing_ack
                # A stream can die while the inbox/journal fsync is in flight.
                # Never start the broker write after that closed the submit lane.
                await self._require_normal_submit_intake(intent)
                self._require_paper_broker()
                spec = IbkrOrderSpec.model_validate(intent.order_spec)
                await asyncio.to_thread(self._journal.append_broker_submitting, intent)
                try:
                    ack = await self._place_under_clerk_grant(spec)
                except Exception as exc:
                    await asyncio.to_thread(self._journal.append_broker_uncertain, intent, exc)
                    raise
                broker_ack = await asyncio.to_thread(self._journal.append_broker_ack, intent, ack)
                await self._publish_manual_order_ack_event(intent, broker_ack)
                return recorded, broker_ack

    async def submit_recovery_flatten(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None = None,
        actor_run_id: str | None = None,
        actor_bot_order_namespace: str | None = None,
    ) -> AccountClerkRecoveryFlattenReceipt:
        return await self._operations.submit_recovery_flatten(
            intent,
            actor=actor,
            actor_strategy_instance_id=actor_strategy_instance_id,
            actor_run_id=actor_run_id,
            actor_bot_order_namespace=actor_bot_order_namespace,
        )

    async def submit_recovery_flatten_batch(
        self,
        intents: tuple[AccountOwnerSubmitIntent, ...],
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None = None,
        actor_run_id: str | None = None,
        actor_bot_order_namespace: str | None = None,
    ) -> tuple[AccountClerkRecoveryFlattenReceipt, ...]:
        return await self._operations.submit_recovery_flatten_batch(
            intents,
            actor=actor,
            actor_strategy_instance_id=actor_strategy_instance_id,
            actor_run_id=actor_run_id,
            actor_bot_order_namespace=actor_bot_order_namespace,
        )

    async def cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        return await self._operations.cancel_namespace(intent)

    @property
    def cancel_namespace_in_progress(self) -> bool:
        return self._operations.state.cancel_namespace_in_progress

    async def resolve_uncertain_cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        return await self._operations.resolve_uncertain_cancel_namespace(intent)

    async def finalize_adopted_cancel_namespace(
        self, intent: AccountOwnerSubmitIntent
    ) -> AccountClerkCancelNamespaceReceipt:
        return await self._operations.finalize_adopted_cancel_namespace(intent)

    def replay_recorded_receipts(self) -> list[AccountClerkRecordedReceipt]:
        """Return receipt #1 values from the journal after a clerk restart."""

        return [
            AccountClerkRecordedReceipt.from_journal_entry(entry)
            for entry in read_account_clerk_journal(self._artifacts_root, self._account_id)
            if entry.entry_kind == "recorded"
        ]

    async def rebuild_attribution(self) -> None:
        """Rebuild order-ref attribution from durable receipt-#1 rows.

        This must complete before the production broker stream starts. A Clerk
        restart otherwise sees a real callback but has only an empty process
        map and would wrongly classify it as foreign.
        """

        async with self._intake_lock:
            unattributed_events = await asyncio.to_thread(self._journal.rebuild_attribution)
            entries = await asyncio.to_thread(self._journal.snapshot)
            emergency_events = [
                entry.emergency_operation
                for entry in entries
                if entry.emergency_operation is not None
            ]
            if emergency_events and emergency_events[-1].phase not in {
                "observed_flat",
                "flag_and_hold",
                "foreign_exposure_freeze",
                "cancel_only_confirmed",
            }:
                self._normal_submit_intake_reason = "CLERK_EMERGENCY_RECOVERY_REQUIRED"
            for event, callback_key in unattributed_events:
                await asyncio.to_thread(
                    self._assert_unattributed_broker_event_guardrail,
                    event,
                    callback_key,
                )

    async def recover_account_state(self) -> None:
        """Classify fresh broker exposure before this Clerk publishes readiness."""

        result = await self._operations.recover_account_state()
        if result.freezes_admission:
            self._normal_submit_intake_reason = "CLERK_BROKER_EVIDENCE_ONLY_FREEZE"
            freeze = AccountFreezeEvidence(
                account_id=self._account_id,
                freeze_kind="exposure",
                reason="Broker exposure has no unique Clerk order-ref attribution.",
                source="account_clerk.startup_recovery",
                recorded_at_ms=self._now_ms(),
                operator_next_step="Reconcile the account exposure before admitting new bots.",
            )
            await asyncio.to_thread(write_account_freeze, self._artifacts_root, freeze)
        elif self._normal_submit_intake_reason == "CLERK_EMERGENCY_RECOVERY_REQUIRED":
            self._normal_submit_intake_reason = None
        # Existing canonical journals must become discoverable at startup even
        # if no new manual order or callback arrives after a database outage.
        self._schedule_transaction_projection()

    async def record_broker_event(self, event: IbkrOrderEvent) -> AccountClerkBrokerEventReceipt:
        """Persist one callback off-loop before any downstream relay.

        The serial intake lock preserves journal order with submitting/ack
        markers. Disk fsync stays in the worker thread, so receipt-time broker
        handling cannot stall the event loop.
        """

        async with self._intake_lock:
            receipt = await asyncio.to_thread(self._journal.record_broker_event, event)
            if receipt.intent is None:
                await asyncio.to_thread(
                    self._assert_unattributed_broker_event_guardrail,
                    event,
                    broker_callback_idempotency_key(event),
                )
        if receipt.intent is not None:
            self._schedule_custody_notification(receipt.intent)
        # The callback receipt is durable before this best-effort rebuildable
        # projection starts; never stall the callback relay on Postgres.
        self._schedule_transaction_projection()
        return receipt

    def _schedule_transaction_projection(self) -> None:
        """Coalesce best-effort transaction projection after durable journal work."""

        self._projection_requested = True
        if self._projection_task is None or self._projection_task.done():
            self._projection_task = asyncio.create_task(
                self._drain_transaction_projection_requests(),
                name="ibkr-transaction-projection",
            )

    async def _drain_transaction_projection_requests(self) -> None:
        while self._projection_requested:
            self._projection_requested = False
            await project_account_journal_best_effort(
                artifacts_root=self._artifacts_root,
                account_id=self._account_id,
            )

    async def mark_event_stream_down(self, failure: BaseException | None = None) -> None:
        """Close normal submit intake and durably alarm a dead broker stream."""

        # Set before the fsync so concurrent submit callers fail closed
        # immediately, rather than racing an alarm write.
        self._normal_submit_intake_reason = "CLERK_EVENT_STREAM_DOWN"
        async with self._intake_lock:
            if self._event_stream_down_recorded:
                return
            await asyncio.to_thread(self._record_event_stream_down_locked, failure)
            self._event_stream_down_recorded = True

    def close_normal_submit_intake(self) -> None:
        """Fence all broker writes before this Clerk's RPC transport shuts down."""

        self._rpc_write_intake_closed = True
        if self._normal_submit_intake_reason is None:
            self._normal_submit_intake_reason = "CLERK_RPC_CLOSED"

    async def wait_for_broker_writes_quiesced(self) -> None:
        """Wait until every broker-write handler accepted before shutdown exits."""

        # Cancellation and recovery release the intake lock while they wait on
        # the broker, but retain this operation lock.  Preserve that acquisition
        # order so shutdown cannot return while an admitted cancel is still able
        # to write to the broker or enqueue a callback.
        async with self._broker_write_lock, self._cancel_operation_lock, self._intake_lock:
            return

    async def recover_inbox(self) -> list[AccountClerkRecordedReceipt]:
        """Replay an inbox row left durable by a crash before journal fsync."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._recover_inbox_locked)

    async def fold_binding_retirement_proposals(self) -> BindingRetirementFoldResult:
        """Apply daemon liveness proposals before this Clerk accepts bot traffic."""

        async with self._intake_lock:
            return await asyncio.to_thread(
                fold_account_binding_retirements,
                self._artifacts_root,
                account_id=self._account_id,
            )

    async def record_binding_decision(
        self,
        binding: AccountInstanceBinding,
        *,
        host_capability: str,
    ) -> AccountInstanceBinding:
        """Serialize one daemon lifecycle transition through the Clerk authority."""

        self._require_host_capability(host_capability)
        if binding.account_id != self._account_id:
            raise AccountClerkIntentRejected(
                reason="CLERK_BINDING_ACCOUNT_MISMATCH",
                diagnostics={
                    "account_id": self._account_id,
                    "binding_account_id": binding.account_id,
                    "strategy_instance_id": binding.strategy_instance_id,
                    "run_id": binding.run_id,
                },
            )
        async with self._intake_lock:
            self._assert_current_generation("record_binding_decision")
            if binding.lifecycle_state == "ACTIVE" and self._normal_submit_intake_reason is not None:
                raise AccountClerkIntentRejected(
                    reason="CLERK_EMERGENCY_ADMISSION_CLOSED",
                    diagnostics={
                        "account_id": self._account_id,
                        "strategy_instance_id": binding.strategy_instance_id,
                        "run_id": binding.run_id,
                        "intake_reason": self._normal_submit_intake_reason,
                    },
                )
            await asyncio.to_thread(
                write_account_instance_binding,
                self._artifacts_root,
                binding,
            )
            return binding

    async def append_operator_adjustment(
        self,
        adjustment: AccountClerkOperatorAdjustment,
        *,
        validate_adjustment: Callable[[list[AccountClerkJournalEntry]], None],
    ) -> AccountClerkJournalEntry:
        """Serialize an operator cure with every other Clerk journal writer."""

        async with self._intake_lock:
            self._assert_current_generation("append_operator_adjustment")
            return await asyncio.to_thread(
                self._journal.append_operator_adjustment,
                adjustment,
                validate_adjustment=validate_adjustment,
            )

    def _recover_inbox_locked(self) -> list[AccountClerkRecordedReceipt]:
        """Compatibility seam for proving recovery stays off the event loop."""

        return self._journal.recover_inbox()

    def _record_intent_locked(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        clerk_request_received_at_ms: int | None = None,
        clerk_intake_admitted_at_ms: int | None = None,
        async_custody_lane: Literal["entry", "risk_reducing"] | None = None,
    ) -> AccountClerkRecordedReceipt:
        if intent.account_id != self._account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")

        return self._journal.record_intent(
            intent,
            validate_intent=self._validate_intent_identity,
            clerk_request_received_at_ms=clerk_request_received_at_ms,
            clerk_intake_admitted_at_ms=clerk_intake_admitted_at_ms,
            async_custody_lane=async_custody_lane,
        )

    def append_broker_event(self, intent: AccountOwnerSubmitIntent, event: IbkrOrderEvent) -> None:
        """Compatibility helper for synchronous test-only callback injection.

        Production callback delivery goes through :meth:`record_broker_event`,
        which offloads this fsync path. Keeping this small legacy seam avoids
        changing older focused journal tests while making the Clerk's durable
        attribution map the sole callback owner.
        """

        self._journal.register_attribution(intent)
        receipt = self._journal.record_broker_event(event)
        if receipt.intent is None:
            self._assert_unattributed_broker_event_guardrail(
                event,
                broker_callback_idempotency_key(event),
            )

    def _assert_unattributed_broker_event_guardrail(
        self,
        event: IbkrOrderEvent,
        callback_key: str,
    ) -> None:
        """Make unknown broker flow visible and block further account starts.

        The Clerk calls this after every duplicate observation and after
        rebuilding a journal, not just on the first append. The opaque broker
        event retains its own account/symbol/side data, while no fake bot
        namespace or strategy id is invented for it.
        """

        append_account_event(
            self._artifacts_root,
            self._account_id,
            {
                "event_type": "account_clerk_unattributed_broker_event",
                "ts_ms": event.ts_ms,
                "reason": "BROKER_EVENT_WITHOUT_DURABLE_CLERK_INTENT",
                "status": "consumed",
                "source": "account_clerk",
                "receipt_id": f"account-clerk-callback:{callback_key}",
                "order_ref": event.order_ref,
                "order_id": event.order_id,
                "perm_id": event.perm_id,
                "exec_id": event.exec_id,
                "event_account_id": event.account_id,
            },
            only_if_receipt_absent=True,
        )
        if read_account_freeze(self._artifacts_root, self._account_id) is None:
            write_account_freeze(
                self._artifacts_root,
                AccountFreezeEvidence(
                    account_id=self._account_id,
                    freeze_kind="exposure",
                    reason="ACCOUNT_CLERK_UNATTRIBUTED_BROKER_EVENT",
                    source="account_clerk",
                    recorded_at_ms=self._now_ms(),
                    operator_next_step="RECONCILE_UNATTRIBUTED_BROKER_EVENT",
                ),
            )

    def _record_event_stream_down_locked(self, failure: BaseException | None) -> None:
        append_account_event(
            self._artifacts_root,
            self._account_id,
            {
                "event_type": "account_clerk_event_stream_down",
                "ts_ms": self._now_ms(),
                "reason": "CLERK_EVENT_STREAM_DOWN",
                "source": "account_clerk",
                "failure_type": type(failure).__name__ if failure is not None else "STREAM_EXITED",
            },
        )

    async def append_reconciliation_resolution(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        verdict: Literal["RECOVER_ADOPT", "RETRY_ONCE", "HALT"],
        reason: str,
    ) -> bool:
        """Durably record a Clerk reconciliation decision before its effect."""

        async with self._intake_lock:
            if self.recovery_flatten_in_progress:
                return False
            await asyncio.to_thread(
                self._journal.append_reconciliation_resolution,
                intent,
                verdict=verdict,
                reason=reason,
            )
            return True

    async def reconciliation_snapshot(self) -> list[AccountClerkJournalEntry]:
        """Return a stable recovered journal tail without disk-wide rescans."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._journal.snapshot)

    async def reconcile_uncertain_intent(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        retry_count: int,
    ) -> AccountClerkReconciliationOutcome | None:
        return await self._operations.reconcile_uncertain_intent(intent, retry_count=retry_count)

    async def retry_recorded_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt:
        """Re-place one Clerk-owned intent after a provably-absent probe."""

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        await self._require_normal_submit_intake(intent)
        self._require_paper_broker()
        async with self._intake_lock:
            operation_fence = (
                nullcontext()
                if intent.intent_kind == MANUAL_ORDER_INTENT_KIND
                else bot_lifecycle_operation_fence(self._artifacts_root, intent.strategy_instance_id)
            )
            with operation_fence:
                await self._require_normal_submit_intake(intent)
                await asyncio.to_thread(self._validate_intent_identity, intent)
                return await self._retry_recorded_intent_locked(intent)

    async def _retry_recorded_intent_locked(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt:
        """Retry an ordinary intent while the Clerk intake lock is held."""

        await asyncio.to_thread(self._journal.register_attribution, intent)
        spec = IbkrOrderSpec.model_validate(intent.order_spec)
        await asyncio.to_thread(self._journal.append_broker_submitting, intent)
        try:
            ack = await self._place_under_clerk_grant(spec)
        except Exception as exc:
            await asyncio.to_thread(self._journal.append_broker_uncertain, intent, exc)
            raise
        receipt = await asyncio.to_thread(self._journal.append_broker_ack, intent, ack)
        await self._publish_manual_order_ack_event(intent, receipt)
        return receipt

    async def _publish_manual_order_ack_event(
        self,
        intent: AccountOwnerSubmitIntent,
        receipt: AccountClerkBrokerAckReceipt,
    ) -> None:
        """Mirror an already-durable manual acknowledgement into Account Desk history.

        The Clerk journal is canonical. This smaller account event exists solely
        to feed the cursor-paginated operator view, so its local write failure
        must not change the already-known broker acknowledgement into a failed
        order submission. A retry of the same intent attempts the idempotent
        mirror again.
        """

        if intent.intent_kind != MANUAL_ORDER_INTENT_KIND:
            return
        try:
            await asyncio.to_thread(self._append_manual_order_ack_event, intent, receipt)
        except (AccountArtifactError, OSError) as exc:
            logger.error(
                "manual order receipt was durable but Account Desk history mirror failed",
                extra={
                    "account_id": intent.account_id,
                    "intent_id": intent.intent_id,
                    "order_ref": intent.order_ref,
                    "order_id": receipt.order_id,
                    "exception": repr(exc),
                },
            )

    def _append_manual_order_ack_event(
        self,
        intent: AccountOwnerSubmitIntent,
        receipt: AccountClerkBrokerAckReceipt,
    ) -> None:
        spec = IbkrOrderSpec.model_validate(intent.order_spec)
        ack = receipt.broker_ack
        acknowledged_at_ms = ack.placed_at_ms if ack is not None else receipt.recorded_at_ms
        append_account_event(
            self._artifacts_root,
            self._account_id,
            {
                "event_type": "account_clerk_manual_order_acked",
                "ts_ms": acknowledged_at_ms,
                "receipt_id": f"account-clerk-manual-order-ack:{intent.order_ref}",
                "broker": "ibkr",
                "intent_id": intent.intent_id,
                "order_ref": intent.order_ref,
                "order_id": receipt.order_id,
                "perm_id": receipt.perm_id,
                "exec_id": receipt.exec_id,
                "symbol": ack.symbol if ack is not None else spec.symbol,
                "action": ack.action if ack is not None else spec.action,
                "quantity": ack.quantity if ack is not None else spec.quantity,
                "order_type": ack.order_type if ack is not None else spec.order_type,
                "limit_price": ack.limit_price if ack is not None else spec.limit_price,
                "status": ack.status if ack is not None else "Acknowledged",
                "acknowledged_at_ms": acknowledged_at_ms,
            },
            only_if_receipt_absent=True,
        )

    def _require_paper_broker(self) -> None:
        client = getattr(self._broker, "_client", None)
        settings = getattr(client, "settings", None)
        if getattr(settings, "mode", None) != "paper":
            raise RuntimeError("ACCOUNT_CLERK_PAPER_MODE_REQUIRED")

    async def _require_normal_submit_intake(self, intent: AccountOwnerSubmitIntent) -> None:
        recovery_reason = await self._journal_recovery_write_reason()
        if recovery_reason is not None:
            self._reject(intent, recovery_reason)
        if self._normal_submit_intake_reason is not None:
            self._reject(intent, self._normal_submit_intake_reason)
        if self.cancel_namespace_in_progress:
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_IN_PROGRESS")
        if self.recovery_flatten_in_progress:
            self._reject(intent, "CLERK_RECOVERY_FLATTEN_IN_PROGRESS")
        unresolved = await asyncio.to_thread(
            self._journal.has_unresolved_namespace_cancellation,
            intent.bot_order_namespace,
        )
        if unresolved:
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_UNRESOLVED")

    def _require_rpc_write_intake(self, intent: AccountOwnerSubmitIntent) -> None:
        if not self._rpc_write_intake_closed:
            return
        self._reject(intent, "CLERK_RPC_CLOSED")

    def _validate_intent_identity(self, intent: AccountOwnerSubmitIntent) -> None:
        if intent.intent_kind == MANUAL_ORDER_INTENT_KIND:
            self._validate_manual_order_intent(intent)
            return
        if intent.intent_kind == "EMERGENCY_FLATTEN":
            expected_strategy_instance_id = emergency_flatten_strategy_instance_id(self._account_id)
            if (
                intent.account_id != self._account_id
                or intent.strategy_instance_id != expected_strategy_instance_id
                or intent.bot_order_namespace != build_bot_order_namespace(expected_strategy_instance_id)
                or not intent.run_id.startswith("clerk-emergency-")
            ):
                self._reject(intent, "CLERK_EMERGENCY_IDENTITY_MISMATCH")
            return
        try:
            if bot_retirement_is_pending(
                self._artifacts_root,
                intent.strategy_instance_id,
            ):
                self._reject(intent, "CLERK_RETIREMENT_PENDING")
        except (BotDeletionCorruptError, ValueError):
            self._reject(intent, "CLERK_RETIREMENT_TRANSITION_UNREADABLE")
        try:
            if pending_account_binding_retirements(
                self._artifacts_root,
                account_id=self._account_id,
                strategy_instance_id=intent.strategy_instance_id,
            ):
                self._reject(intent, "CLERK_BINDING_RETIREMENT_PENDING")
        except ValueError:
            self._reject(intent, "CLERK_BINDING_LEDGER_UNREADABLE")
        try:
            if not account_binding_ledger_parity(
                self._artifacts_root,
                account_id=self._account_id,
            ).is_clean:
                self._reject(intent, "CLERK_BINDING_LEDGER_PARITY_DIRTY")
        except ValueError:
            self._reject(intent, "CLERK_BINDING_LEDGER_UNREADABLE")
        binding = latest_account_instance_binding(
            read_account_instance_registry(self._artifacts_root, self._account_id),
            account_id=self._account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            self._reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        if intent.intent_kind == "RECOVERY_FLATTEN":
            self._operations.validate_recovery_binding(intent, binding)
            return
        self._validate_binding(intent, binding)

    def _validate_manual_order_intent(self, intent: AccountOwnerSubmitIntent) -> None:
        """Accept only the server-authored account-owned manual-ticket shape."""

        if (
            intent.account_id != self._account_id
            or intent.strategy_instance_id != MANUAL_OPERATOR_STRATEGY_INSTANCE_ID
            or intent.run_id != MANUAL_OPERATOR_RUN_ID
            or intent.bot_order_namespace != build_manual_order_namespace("operator")
        ):
            self._reject(intent, "CLERK_MANUAL_IDENTITY_MISMATCH")
        try:
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
        except ValueError:
            self._reject(intent, "CLERK_MANUAL_ORDER_SPEC_INVALID")
        if (
            not spec.manual_order
            or spec.order_ref != intent.order_ref
            or spec.order_ref is None
        ):
            self._reject(intent, "CLERK_MANUAL_ORDER_SPEC_MISMATCH")

    def _require_host_capability(self, host_capability: str) -> None:
        if (
            self._host_binding_capability is None
            or not hmac.compare_digest(host_capability, self._host_binding_capability)
        ):
            raise AccountClerkIntentRejected(
                reason="CLERK_HOST_BINDING_CAPABILITY_REQUIRED",
                diagnostics={"account_id": self._account_id},
            )

    def _validate_binding(
        self,
        intent: AccountOwnerSubmitIntent,
        binding: AccountInstanceBinding,
    ) -> None:
        if binding.lifecycle_state != "ACTIVE":
            self._reject(intent, "CLERK_INACTIVE_BINDING")
        if binding.account_id != intent.account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")
        if binding.run_id != intent.run_id:
            self._reject(intent, "CLERK_STALE_RUN")
        if binding.bot_order_namespace != intent.bot_order_namespace:
            self._reject(intent, "CLERK_NAMESPACE_MISMATCH")

    async def _cancel_namespace_open_orders(
        self,
        namespace: str,
        *,
        before_broker_write: Callable[[], Awaitable[None]] | None = None,
    ) -> list[int]:
        cancel_namespace = getattr(self._broker, "cancel_open_orders_for_namespace", None)
        if not callable(cancel_namespace):
            raise RuntimeError("ACCOUNT_CLERK_CANCEL_NAMESPACE_UNSUPPORTED")
        return await self._run_broker_write(
            "account_clerk.broker.cancel_open_orders_for_namespace",
            lambda: cancel_namespace(namespace),
            before_broker_write=before_broker_write,
        )

    async def _cancel_order_refs_open_orders(
        self,
        order_refs: tuple[str, ...],
        *,
        before_broker_write: Callable[[], Awaitable[None]] | None = None,
    ) -> list[int]:
        """Cancel only the exact durable references selected by startup fold."""

        cancel_refs = getattr(self._broker, "cancel_open_orders_for_refs", None)
        if not callable(cancel_refs):
            raise RuntimeError("ACCOUNT_CLERK_CANCEL_EXACT_REFS_UNSUPPORTED")
        return await self._run_broker_write(
            "account_clerk.broker.cancel_open_orders_for_refs",
            lambda: cancel_refs(order_refs),
            before_broker_write=before_broker_write,
        )

    async def _place_under_clerk_grant(
        self,
        spec: IbkrOrderSpec,
        *,
        wait_for_perm_id: bool = False,
    ) -> Any:
        place_order = self._broker.place_order
        accepts_perm_id_wait = "perm_id_wait_s" in inspect.signature(place_order).parameters
        kwargs = {"perm_id_wait_s": _RECOVERY_PERM_ID_WAIT_S} if wait_for_perm_id and accepts_perm_id_wait else {}
        return await asyncio.wait_for(
            self._run_broker_write(
                "account_clerk.broker.place_order",
                lambda: place_order(spec, **kwargs),
            ),
            timeout=_BROKER_SUBMIT_TIMEOUT_S,
        )

    async def _journal_recovery_write_reason(self) -> str | None:
        """Return the durable account-wide broker-write fence, if any.

        This is deliberately re-read at each admission boundary.  A recovery
        can complete while this long-lived Clerk process is still serving, so
        recording the temporary condition in normal intake would make a safe
        re-baseline look permanently unavailable until a process restart.
        """

        try:
            await asyncio.to_thread(
                read_account_clerk_durability_spine,
                self._artifacts_root,
                self._account_id,
            )
        except AccountClerkJournalCorruptError:
            return "CLERK_JOURNAL_CORRUPT"
        try:
            recovery_fence = await asyncio.to_thread(
                assess_journal_recovery_fence,
                self._artifacts_root,
                self._account_id,
            )
        except JournalRecoveryStateCorruptError:
            return "CLERK_JOURNAL_RECOVERY_STATE_CORRUPT"
        return recovery_fence.reason_code if recovery_fence.blocks_broker_writes else None

    async def _run_broker_write(
        self,
        boundary: str,
        write: Callable[[], Any],
        *,
        before_broker_write: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        """Exclude a recovery claim from final broker-write admission onward."""

        async with _journal_recovery_broker_write_lock(self._artifacts_root, self._account_id):
            return await self._run_broker_write_under_recovery_admission(
                boundary,
                write,
                before_broker_write=before_broker_write,
            )

    async def _run_broker_write_under_recovery_admission(
        self,
        boundary: str,
        write: Callable[[], Any],
        *,
        before_broker_write: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        # A torn journal is a global account-write fence.  This final shared
        # boundary covers normal placements, recovery actions, cancellations,
        # and emergencies, so no exceptional path can reach IBKR on corrupt
        # durable intent evidence.
        recovery_reason = await self._journal_recovery_write_reason()
        if recovery_reason is not None:
            raise RuntimeError(recovery_reason)
        if self._clerk_generation is None:
            if before_broker_write is not None:
                await before_broker_write()
            return await write()
        try:
            observed_generation = self._read_active_generation()
        except (OSError, ValueError) as exc:
            self._fence_stale_generation()
            raise AccountClerkGenerationFencedError(
                account_id=self._account_id,
                expected_generation=self._clerk_generation,
                observed_generation=None,
                boundary=boundary,
            ) from exc
        if observed_generation != self._clerk_generation:
            self._fence_stale_generation()
            raise AccountClerkGenerationFencedError(
                account_id=self._account_id,
                expected_generation=self._clerk_generation,
                observed_generation=observed_generation,
                boundary=boundary,
            )
        try:
            with account_clerk_write_grant(
                account_id=self._account_id,
                clerk_generation=self._clerk_generation,
                boundary=boundary,
                clerk_generation_provider=self._read_active_generation,
            ):
                if before_broker_write is not None:
                    await before_broker_write()
                return await write()
        except AccountClerkWriteFenceError as exc:
            if exc.reason not in {
                "CLERK_LEASE_UNAVAILABLE_AT_BROKER_WRITE",
                "OWNER_GENERATION_STALE_AT_BROKER_WRITE",
            }:
                raise
            self._fence_stale_generation()
            raise AccountClerkGenerationFencedError(
                account_id=self._account_id,
                expected_generation=self._clerk_generation,
                observed_generation=exc.current_clerk_generation,
                boundary=boundary,
            ) from exc

    def _read_active_generation(self) -> int | None:
        if self._durable_generation_provider is None:
            return self._clerk_generation
        return self._durable_generation_provider()

    def _assert_current_generation(self, boundary: str) -> None:
        """Fence non-broker journal mutations when this Clerk was superseded."""

        if self._clerk_generation is None:
            return
        observed_generation = self._read_active_generation()
        if observed_generation == self._clerk_generation:
            return
        self._fence_stale_generation()
        raise AccountClerkGenerationFencedError(
            account_id=self._account_id,
            expected_generation=self._clerk_generation,
            observed_generation=observed_generation,
            boundary=boundary,
        )

    def _fence_stale_generation(self) -> None:
        if self._on_generation_fenced is not None:
            self._on_generation_fenced()

    def _reject(self, intent: AccountOwnerSubmitIntent, reason: str, **extra_diagnostics: object) -> None:
        raise AccountClerkIntentRejected(
            reason=reason,
            diagnostics={
                "trace_id": intent.trace_id,
                "account_id": intent.account_id,
                "strategy_instance_id": intent.strategy_instance_id,
                "run_id": intent.run_id,
                "bot_order_namespace": intent.bot_order_namespace,
                "intent_id": intent.intent_id,
                "order_ref": intent.order_ref,
                **extra_diagnostics,
            },
        )


def account_clerk_authority_lock_target(artifacts_root: Path, account_id: str) -> Path:
    """Return the non-generation target used for this account's lifetime lock.

    ``advisory_file_lock`` derives its lock file from this stable account-local
    target. The durable generation record is fencing evidence only and is
    never used as the exclusion primitive.
    """

    return account_artifacts_root(artifacts_root, account_id) / ACCOUNT_CLERK_AUTHORITY_LOCK_TARGET_FILENAME


@contextmanager
def account_clerk_authority_lock(artifacts_root: Path, account_id: str) -> Iterator[None]:
    """Hold the same-host Clerk authority lock for one account's full lifetime."""

    with advisory_file_lock(account_clerk_authority_lock_target(artifacts_root, account_id)):
        yield


def account_clerk_socket_path(artifacts_root: Path, account_id: str) -> Path:
    """Short private Unix socket path (macOS caps AF_UNIX paths at 104 bytes)."""

    # The account artifact root can exceed the platform's AF_UNIX pathname
    # limit in temp-backed test and desktop workspaces.  The hash preserves a
    # stable one-account mapping without exposing the account id in /tmp.
    digest = hashlib.sha256(
        f"{account_artifacts_root(artifacts_root, account_id)}\0{account_id}".encode()
    ).hexdigest()[:32]
    return Path(tempfile.gettempdir()) / "learn-ai-clerk" / f"{digest}.sock"


def _active_durable_clerk_generation(artifacts_root: Path, account_id: str) -> int | None:
    generation = read_account_clerk_generation(artifacts_root, account_id)
    if generation is None or generation.phase != "accepting":
        return None
    return generation.generation


async def _run_clerk_process(args: argparse.Namespace) -> int:
    artifacts_root = Path(args.artifacts_root)
    stop = asyncio.Event()
    stream_failed = asyncio.Event()
    configured_client_id = getattr(args, "ibkr_client_id", os.environ.get("IBKR_CLIENT_ID"))
    try:
        ibkr_client_id = int(configured_client_id) if configured_client_id is not None else None
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ACCOUNT_CLERK_IBKR_CLIENT_ID_INVALID") from exc

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    from app.broker.ibkr.client import IbkrClient
    from app.engine.live.account_clerk_rpc import AccountClerkRpcServer
    from app.engine.live.live_portfolio import IbkrBrokerAdapter
    from app.services.journal_cures import JournalCureService

    with account_clerk_authority_lock(artifacts_root, args.account_id):
        def durable_generation_provider() -> int | None:
            return _active_durable_clerk_generation(artifacts_root, args.account_id)

        observed_generation = durable_generation_provider()
        if observed_generation != args.generation:
            raise AccountClerkGenerationFencedError(
                account_id=args.account_id,
                expected_generation=args.generation,
                observed_generation=observed_generation,
                boundary="account_clerk.startup",
            )

        client = IbkrClient()
        if client.settings.mode != "paper":
            raise RuntimeError("ACCOUNT_CLERK_PAPER_MODE_REQUIRED")
        await client.connect()
        broker = IbkrBrokerAdapter(client)
        broker.require_account_owner_write_fence(durable_generation_provider)
        clerk = AccountClerk(
            artifacts_root=artifacts_root,
            account_id=args.account_id,
            broker=broker,
            clerk_generation=args.generation,
            durable_generation_provider=durable_generation_provider,
            on_generation_fenced=stop.set,
            host_binding_capability=os.environ.get(CLERK_HOST_BINDING_CAPABILITY_ENV),
            async_custody_config=AccountClerkAsyncCustodyConfig(
                entry_capacity=_PRODUCTION_ASYNC_CUSTODY_ENTRY_CAPACITY,
                risk_reducing_capacity=_PRODUCTION_ASYNC_CUSTODY_RISK_REDUCING_CAPACITY,
            ),
        )

        def on_callback_persistence_failure(_failure: BaseException) -> None:
            stream_failed.set()
            stop.set()

        journal_cures = JournalCureService(artifacts_root=artifacts_root)
        server = AccountClerkRpcServer(
            clerk,
            on_callback_persistence_failure=on_callback_persistence_failure,
            operator_adjustment_handler=journal_cures.handler_for_clerk(
                account_id=args.account_id,
                append_operator_adjustment=clerk.append_operator_adjustment,
            ),
        )
        from app.engine.live.account_clerk_reconciler import AccountClerkReconciler

        reconciler = AccountClerkReconciler(clerk, on_unhealthy=stop.set)
        writer = AccountClerkLeaseWriter(
            artifacts_root=artifacts_root,
            account_id=args.account_id,
            generation=args.generation,
            pid=os.getpid(),
            ibkr_client_id=ibkr_client_id,
        )
        event_stream_started = False
        event_stream_supervisor: asyncio.Task[None] | None = None
        try:
            await server.start()
            await broker.start_event_stream()
            event_stream_started = True
            await asyncio.to_thread(
                append_account_event,
                artifacts_root,
                args.account_id,
                {
                    "event_type": "account_clerk_event_stream_recovered",
                    "ts_ms": _now_ms(),
                    "reason": "CLERK_EVENT_STREAM_STARTED",
                    "source": "account_clerk",
                    "generation": args.generation,
                },
            )
            event_stream_supervisor = asyncio.create_task(
                _supervise_broker_event_stream(
                    broker=broker,
                    clerk=clerk,
                    stop=stop,
                    stream_failed=stream_failed,
                ),
                name="account-clerk-broker-event-stream-supervisor",
            )
            await reconciler.start()
            while not stop.is_set():
                await asyncio.to_thread(writer.renew)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1)
        finally:
            if event_stream_supervisor is not None:
                event_stream_supervisor.cancel()
                with suppress(asyncio.CancelledError):
                    await event_stream_supervisor
            if event_stream_started:
                await broker.stop_event_stream()
            await asyncio.to_thread(writer.renew, draining=True)
            await reconciler.close()
            await server.close()
            await client.disconnect()
        return 1 if stream_failed.is_set() or reconciler.unhealthy else 0


async def _supervise_broker_event_stream(
    *,
    broker: object,
    clerk: AccountClerk,
    stop: asyncio.Event,
    stream_failed: asyncio.Event,
) -> None:
    """Terminate the Clerk when its callback stream stops unexpectedly.

    ``IbkrBrokerAdapter`` records a thrown stream exception and lets its task
    return, so both an exception and an otherwise clean task exit are failure
    while the Clerk is not stopping. The adapter task is intentionally read
    through its adapter-owned task here: the Clerk owns this adapter for its
    full process lifetime and needs a supervision handle, not a polling guess.
    """

    stream_task = getattr(broker, "_event_task", None)
    if isinstance(stream_task, asyncio.Task):
        try:
            await asyncio.shield(stream_task)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc
        else:
            observed = getattr(broker, "stream_failure", None)
            failure = observed if isinstance(observed, BaseException) else RuntimeError(
                "CLERK_EVENT_STREAM_EXITED"
            )
    else:
        # The production IbkrBrokerAdapter exposes its task above. Keep the
        # Clerk testable against a narrower event-adapter seam too: a custom
        # adapter that cannot expose a task must still surface a recorded
        # ``stream_failure`` rather than leaving normal submits blind.
        while not stop.is_set():
            observed = getattr(broker, "stream_failure", None)
            if isinstance(observed, BaseException):
                failure = observed
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - retained for type-checker exhaustiveness.
            return
        if stop.is_set():
            return

    if stop.is_set():
        return
    await clerk.mark_event_stream_down(failure)
    stream_failed.set()
    stop.set()


def _ack_failed_uncertain_status():
    """Avoid importing the intent event graph during Clerk module import."""

    from app.engine.live.intent_events import IntentEventType

    return IntentEventType.ACK_FAILED_UNCERTAIN


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one account clerk lease process.")
    parser.add_argument("--artifacts-root", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--ibkr-client-id", required=True, type=int)
    return asyncio.run(_run_clerk_process(parser.parse_args()))


def __getattr__(name: str):
    """Keep the #1020 RPC import surface while avoiding a transport cycle."""

    if name in {"AccountClerkRpcClient", "AccountClerkRpcServer"}:
        from app.engine.live import account_clerk_rpc

        return getattr(account_clerk_rpc, name)
    raise AttributeError(name)


__all__ = [
    "ACCOUNT_CLERK_AUTHORITY_LOCK_TARGET_FILENAME",
    "ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S",
    "ACCOUNT_CLERK_INBOX_FILENAME",
    "ACCOUNT_CLERK_JOURNAL_FILENAME",
    "AccountClerk",
    "AccountClerkBrokerAckReceipt",
    "AccountClerkCancelNamespaceUncertainError",
    "AccountClerkGenerationFencedError",
    "AccountClerkInboxEntry",
    "AccountClerkIntentRejected",
    "AccountClerkJournalCorruptError",
    "AccountClerkJournalEntry",
    "AccountClerkLeaseWriter",
    "AccountClerkReconciliationOutcome",
    "AccountClerkRecordedReceipt",
    "AccountClerkRecoveryFlattenReceipt",
    "AccountClerkRpcClient",
    "AccountClerkRpcServer",
    "account_clerk_authority_lock",
    "account_clerk_authority_lock_target",
    "account_clerk_inbox_path",
    "account_clerk_journal_path",
    "account_clerk_socket_path",
    "read_account_clerk_inbox",
    "read_account_clerk_journal",
]


if __name__ == "__main__":
    raise SystemExit(main())
