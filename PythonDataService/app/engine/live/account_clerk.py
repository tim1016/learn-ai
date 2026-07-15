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
import logging
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from app.broker.ibkr.models import IbkrOrderEvent, IbkrOrderSpec
from app.engine.live.account_artifacts import (
    AccountClerkLease,
    AccountFreezeEvidence,
    account_artifacts_root,
    append_account_event,
    read_account_clerk_generation,
    read_account_freeze,
    write_account_clerk_lease,
    write_account_freeze,
)
from app.engine.live.account_clerk_journal import (
    ACCOUNT_CLERK_INBOX_FILENAME,
    ACCOUNT_CLERK_JOURNAL_FILENAME,
    AccountClerkBrokerAckReceipt,
    AccountClerkBrokerEventReceipt,
    AccountClerkCancelNamespaceReceipt,
    AccountClerkInboxEntry,
    AccountClerkIntentRejected,
    AccountClerkJournal,
    AccountClerkJournalCorruptError,
    AccountClerkJournalEntry,
    AccountClerkRecordedReceipt,
    AccountClerkRecoveryFlattenReceipt,
    _now_ms,
    account_clerk_inbox_path,
    account_clerk_journal_path,
    read_account_clerk_inbox,
    read_account_clerk_journal,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.account_owner_fence import (
    AccountClerkWriteFenceError,
    account_clerk_write_grant,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    latest_account_instance_binding,
    read_account_instance_registry,
)
from app.engine.live.broker_callbacks import broker_callback_idempotency_key
from app.utils.advisory_lock import advisory_file_lock

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.engine.live.account_clerk_rpc import AccountClerkRpcClient, AccountClerkRpcServer
    from app.engine.live.submit_state_machine import BrokerProbe

ACCOUNT_CLERK_AUTHORITY_LOCK_TARGET_FILENAME = "clerk_authority"
_CLERK_LEASE_TTL_MS = 5_000
# The Clerk must release its account-wide intake lock before the bot-side RPC
# budget expires.  A timed-out broker write is ambiguous, so its durable
# ``broker_uncertain`` row is deliberately left for reconciliation.
_BROKER_SUBMIT_TIMEOUT_S = 25.0
ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S = 25.0


class AccountClerkGenerationFencedError(RuntimeError):
    """A Clerk observed that its durable write generation is no longer active."""

    def __init__(
        self,
        *,
        account_id: str,
        expected_generation: int,
        observed_generation: int | None,
        boundary: str,
    ) -> None:
        super().__init__("CLERK_GENERATION_STALE")
        self.account_id = account_id
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        self.boundary = boundary


class AccountClerkCancelNamespaceUncertainError(RuntimeError):
    """The Clerk cannot prove that a namespace cancellation reached terminal state."""


@dataclass(frozen=True)
class AccountClerkReconciliationOutcome:
    """One stable, Clerk-owned reconciliation decision."""

    intent_id: str
    order_ref: str
    verdict: Literal["RECOVER_ADOPT", "RETRY_ONCE", "HALT"]
    reason: str


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
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._broker = broker
        self._clerk_generation = clerk_generation
        self._durable_generation_provider = durable_generation_provider
        self._on_generation_fenced = on_generation_fenced
        self._now_ms = now_ms if now_ms is not None else _now_ms
        self._intake_lock = asyncio.Lock()
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
        self._cancel_namespace_in_progress = False
        # A recovery liquidation is deliberately two-phase: cancellation is
        # confirmed first, then callbacks already emitted by that cancellation
        # are durably folded before the exact journal-derived liquidation is
        # admitted.  The marker keeps normal submits and reconciliation retries
        # out during that callback-drain window.
        self._recovery_flatten_namespace: str | None = None
        self._callback_drain: Callable[[], Awaitable[None]] | None = None

    @property
    def recovery_flatten_in_progress(self) -> bool:
        """Whether a namespace recovery owns the Clerk's submit lane."""

        return self._recovery_flatten_namespace is not None

    def set_callback_drain(self, drain: Callable[[], Awaitable[None]]) -> None:
        """Install the RPC callback queue drain used by recovery flatten.

        Direct Clerk tests and non-RPC callers have no callback relay, so they
        intentionally leave this unset.  The production RPC server supplies a
        drain after it has established the sole broker callback sink.
        """

        self._callback_drain = drain

    async def record_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecordedReceipt:
        """Validate, durably record, and acknowledge one intent without I/O to IBKR."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._record_intent_locked, intent)

    async def submit_intent(
        self,
        intent: AccountOwnerSubmitIntent,
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
        async with self._intake_lock:
            await self._require_normal_submit_intake(intent)
            recorded = await asyncio.to_thread(self._record_intent_locked, intent)
            existing_ack = await asyncio.to_thread(self._journal.ack_for_intent, intent)
            if existing_ack is not None:
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
        """Cancel this namespace's open orders then place one liquidation.

        The Clerk, not a fenced bot, owns both broker writes.  A bot may only
        name its own complete binding; the operator cure is deliberately
        restricted to a retired binding so it cannot become another submit
        lane for active bots.
        """

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "RECOVERY_FLATTEN":
            self._reject(intent, "CLERK_RECOVERY_INTENT_KIND_REQUIRED")
        self._validate_recovery_actor(
            intent,
            actor=actor,
            actor_strategy_instance_id=actor_strategy_instance_id,
            actor_run_id=actor_run_id,
            actor_bot_order_namespace=actor_bot_order_namespace,
        )
        self._validate_recovery_order(intent)
        async with self._cancel_operation_lock, self._intake_lock:
            recorded = await asyncio.to_thread(self._record_intent_locked, intent)
            existing_ack = await asyncio.to_thread(self._journal.ack_for_intent, intent)
            if existing_ack is not None:
                cancelled = await asyncio.to_thread(self._journal.recovery_cancelled_for_intent, intent)
                return AccountClerkRecoveryFlattenReceipt(
                    recorded=recorded,
                    broker_acked=existing_ack,
                    cancelled_order_ids=cancelled,
                )
            if self._recovery_flatten_namespace is not None:
                self._reject(intent, "CLERK_RECOVERY_FLATTEN_IN_PROGRESS")
            if await asyncio.to_thread(self._journal.recovery_operation_started_for_intent, intent):
                self._reject(intent, "CLERK_RECOVERY_REQUIRES_OPERATOR_RECONCILIATION")
            self._recovery_flatten_namespace = intent.bot_order_namespace
            try:
                self._require_paper_broker()
                await asyncio.to_thread(self._journal.append_recovery_cancelling, intent)
                cancelled = await self._cancel_namespace_open_orders(intent.bot_order_namespace)
                await asyncio.to_thread(self._journal.append_recovery_cancelled, intent, cancelled)
            except Exception:
                self._recovery_flatten_namespace = None
                raise

        try:
            # Cancellation is terminal before this point, but its last fill
            # callback can still be queued by the broker adapter.  Releasing
            # the intake lock lets the sole callback worker fsync that event;
            # only then may recovery size from the canonical journal fold.
            if self._callback_drain is not None:
                await self._callback_drain()
            async with self._intake_lock:
                spec = IbkrOrderSpec.model_validate(intent.order_spec)
                await asyncio.to_thread(self._validate_recovery_exposure, intent, spec)
                await asyncio.to_thread(self._journal.append_broker_submitting, intent)
                try:
                    ack = await self._place_under_clerk_grant(spec)
                except Exception as exc:
                    await asyncio.to_thread(self._journal.append_broker_uncertain, intent, exc)
                    raise
                broker_ack = await asyncio.to_thread(self._journal.append_broker_ack, intent, ack)
                return AccountClerkRecoveryFlattenReceipt(
                    recorded=recorded,
                    broker_acked=broker_ack,
                    cancelled_order_ids=tuple(cancelled),
                )
        finally:
            async with self._intake_lock:
                if self._recovery_flatten_namespace == intent.bot_order_namespace:
                    self._recovery_flatten_namespace = None

    async def cancel_namespace(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCancelNamespaceReceipt:
        """Durably cancel one active run's namespace through the Clerk.

        ``intent`` is a cancellation receipt identity, never an order to
        submit.  The journaled receipt precedes the broker call and the same
        identity returns the prior terminal confirmation without reissuing a
        broker cancellation.
        """

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "CANCEL_NAMESPACE":
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_INTENT_KIND_REQUIRED")
        async with self._cancel_operation_lock:
            try:
                async with self._intake_lock:
                    recorded = await asyncio.to_thread(self._record_intent_locked, intent)
                    existing = await asyncio.to_thread(self._journal.cancel_confirmed_for_intent, intent)
                    if existing is not None:
                        return existing
                    # Never repeat a cancellation after a process died in its
                    # broker-write window.  The terminal state is unknowable,
                    # so record that uncertainty and leave the namespace frozen.
                    if await asyncio.to_thread(self._journal.cancel_submitting_for_intent, intent):
                        uncertainty = RuntimeError("prior cancel attempt may have reached broker")
                        await asyncio.to_thread(self._journal.append_cancel_uncertain, intent, uncertainty)
                        raise AccountClerkCancelNamespaceUncertainError(
                            "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN"
                        )
                    self._require_paper_broker()
                    self._cancel_namespace_in_progress = True
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    cancelled = await self._cancel_namespace_open_orders(
                        intent.bot_order_namespace,
                        before_broker_write=lambda: asyncio.to_thread(
                            self._journal.append_cancel_submitting,
                            intent,
                        ),
                    )
                    # The broker's terminal cancel may have emitted final fills.
                    # Do not acknowledge cancellation until the RPC callback
                    # worker has fsynced all callbacks already queued by it.
                    if self._callback_drain is not None:
                        await self._callback_drain()
            except AccountClerkGenerationFencedError:
                # Fencing proves this process never owned the broker write;
                # preserve the typed stale-generation response instead of
                # downgrading it to ambiguous cancellation.
                raise
            except AccountClerkCancelNamespaceUncertainError:
                raise
            except Exception as exc:
                async with self._intake_lock:
                    await asyncio.to_thread(self._journal.append_cancel_uncertain, intent, exc)
                raise AccountClerkCancelNamespaceUncertainError(
                    "ACCOUNT_CLERK_CANCEL_NAMESPACE_UNCERTAIN"
                ) from exc
            else:
                async with self._intake_lock:
                    receipt = await asyncio.to_thread(
                        self._journal.append_cancel_confirmed,
                        intent,
                        cancelled,
                    )
                    if receipt.recorded != recorded:
                        raise RuntimeError("cancel receipt recorded identity mismatch")
                    return receipt
            finally:
                self._cancel_namespace_in_progress = False

    @property
    def cancel_namespace_in_progress(self) -> bool:
        return self._cancel_namespace_in_progress

    async def resolve_uncertain_cancel_namespace(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCancelNamespaceReceipt:
        """Retry a fenced cancel after reconciliation observes surviving orders."""

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        if intent.intent_kind != "CANCEL_NAMESPACE":
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_INTENT_KIND_REQUIRED")
        async with self._cancel_operation_lock:
            async with self._intake_lock:
                existing = await asyncio.to_thread(self._journal.cancel_confirmed_for_intent, intent)
                if existing is not None:
                    return existing
                self._require_paper_broker()
                self._cancel_namespace_in_progress = True
            try:
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    cancelled = await self._cancel_namespace_open_orders(intent.bot_order_namespace)
                    if self._callback_drain is not None:
                        await self._callback_drain()
            except AccountClerkGenerationFencedError:
                raise
            except Exception as exc:
                async with self._intake_lock:
                    await asyncio.to_thread(self._journal.append_cancel_uncertain, intent, exc)
                raise
            else:
                async with self._intake_lock:
                    return await asyncio.to_thread(
                        self._journal.append_cancel_confirmed,
                        intent,
                        cancelled,
                    )
            finally:
                self._cancel_namespace_in_progress = False

    async def finalize_adopted_cancel_namespace(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> AccountClerkCancelNamespaceReceipt:
        """Persist a terminal cancel receipt after reconciliation proves no order remains."""

        async with self._cancel_operation_lock:
            async with self._intake_lock:
                existing = await asyncio.to_thread(self._journal.cancel_confirmed_for_intent, intent)
                if existing is not None:
                    return existing
                self._cancel_namespace_in_progress = True
            try:
                async with asyncio.timeout(ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S):
                    if self._callback_drain is not None:
                        await self._callback_drain()
                async with self._intake_lock:
                    return await asyncio.to_thread(
                        self._journal.append_cancel_confirmed,
                        intent,
                        (),
                    )
            finally:
                self._cancel_namespace_in_progress = False

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
            for event, callback_key in unattributed_events:
                await asyncio.to_thread(
                    self._assert_unattributed_broker_event_guardrail,
                    event,
                    callback_key,
                )

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
        return receipt

    async def mark_event_stream_down(self, failure: BaseException | None = None) -> None:
        """Close normal submit intake and durably alarm a dead broker stream."""

        if self._normal_submit_intake_reason is not None:
            return
        # Set before the fsync so concurrent submit callers fail closed
        # immediately, rather than racing an alarm write.
        self._normal_submit_intake_reason = "CLERK_EVENT_STREAM_DOWN"
        await asyncio.to_thread(self._record_event_stream_down_locked, failure)

    async def recover_inbox(self) -> list[AccountClerkRecordedReceipt]:
        """Replay an inbox row left durable by a crash before journal fsync."""

        async with self._intake_lock:
            return await asyncio.to_thread(self._recover_inbox_locked)

    def _recover_inbox_locked(self) -> list[AccountClerkRecordedReceipt]:
        """Compatibility seam for proving recovery stays off the event loop."""

        return self._journal.recover_inbox()

    def _record_intent_locked(self, intent: AccountOwnerSubmitIntent) -> AccountClerkRecordedReceipt:
        if intent.account_id != self._account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")

        return self._journal.record_intent(intent, validate_intent=self._validate_intent_identity)

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
        """Probe, decide, record, and (only when safe) retry under one lock."""

        from app.engine.live.submit_state_machine import SubmitVerdict, next_action

        async with self._intake_lock:
            if self.recovery_flatten_in_progress or self._intent_is_terminal(intent):
                return None
            probe = await self._probe_intent_status(intent)
            verdict = next_action(
                current_status=_ack_failed_uncertain_status(),
                probe=probe,
                retry_count=retry_count,
            )
            reason = f"probe={probe.value}; retry_count={retry_count}"
            # Recovery retry would repeat an ambiguous cancellation/flatten
            # sequence.  It remains fail-closed even when the broker says the
            # final order is absent; an operator must inspect the journal.
            if intent.intent_kind == "RECOVERY_FLATTEN" and verdict is SubmitVerdict.RETRY_ONCE:
                verdict = SubmitVerdict.HALT
                reason = f"{reason}; recovery retry requires operator reconciliation"
            await asyncio.to_thread(
                self._journal.append_reconciliation_resolution,
                intent,
                verdict=verdict.value,
                reason=reason,
            )
            if verdict is SubmitVerdict.RETRY_ONCE:
                try:
                    self._require_paper_broker()
                    await self._retry_recorded_intent_locked(intent)
                except Exception as exc:
                    reason = f"{reason}; retry raised {type(exc).__name__}: {exc}"
            return AccountClerkReconciliationOutcome(
                intent_id=intent.intent_id,
                order_ref=intent.order_ref,
                verdict=verdict.value,
                reason=reason,
            )

    async def retry_recorded_intent(self, intent: AccountOwnerSubmitIntent) -> AccountClerkBrokerAckReceipt:
        """Re-place one Clerk-owned intent after a provably-absent probe."""

        if self._broker is None:
            raise RuntimeError("ACCOUNT_CLERK_BROKER_UNAVAILABLE")
        await self._require_normal_submit_intake(intent)
        self._require_paper_broker()
        async with self._intake_lock:
            await self._require_normal_submit_intake(intent)
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
        return await asyncio.to_thread(self._journal.append_broker_ack, intent, ack)

    def _intent_is_terminal(self, intent: AccountOwnerSubmitIntent) -> bool:
        """Re-check the current tail immediately before a reconciliation act."""

        for entry in self._journal.snapshot():
            if entry.intent != intent:
                continue
            if entry.entry_kind == "broker_acked":
                return True
            if entry.entry_kind == "reconciliation" and entry.reconciliation_verdict in {
                "RECOVER_ADOPT",
                "HALT",
            }:
                return True
        return False

    async def _probe_intent_status(self, intent: AccountOwnerSubmitIntent) -> BrokerProbe:
        """Use the Clerk's public broker seam and preserve probe diagnostics."""

        from app.engine.live.submit_state_machine import BrokerProbe

        probe_fn = getattr(self._broker, "probe_intent_status", None)
        if not callable(probe_fn):
            return BrokerProbe.NOT_PROVABLE
        try:
            return BrokerProbe(await probe_fn(intent.intent_id, intent.order_ref))
        except Exception:
            logger.exception(
                "Account Clerk reconciliation probe failed",
                extra={"account_id": self._account_id, "intent_id": intent.intent_id},
            )
            return BrokerProbe.NOT_PROVABLE

    def _require_paper_broker(self) -> None:
        client = getattr(self._broker, "_client", None)
        settings = getattr(client, "settings", None)
        if getattr(settings, "mode", None) != "paper":
            raise RuntimeError("ACCOUNT_CLERK_PAPER_MODE_REQUIRED")

    async def _require_normal_submit_intake(self, intent: AccountOwnerSubmitIntent) -> None:
        if self._normal_submit_intake_reason is not None:
            self._reject(intent, self._normal_submit_intake_reason)
        if self._cancel_namespace_in_progress:
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_IN_PROGRESS")
        if self.recovery_flatten_in_progress:
            self._reject(intent, "CLERK_RECOVERY_FLATTEN_IN_PROGRESS")
        unresolved = await asyncio.to_thread(
            self._journal.has_unresolved_namespace_cancellation,
            intent.bot_order_namespace,
        )
        if unresolved:
            self._reject(intent, "CLERK_CANCEL_NAMESPACE_UNRESOLVED")

    def _validate_intent_identity(self, intent: AccountOwnerSubmitIntent) -> None:
        binding = latest_account_instance_binding(
            read_account_instance_registry(self._artifacts_root, self._account_id),
            account_id=self._account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            self._reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        if intent.intent_kind == "RECOVERY_FLATTEN":
            self._validate_recovery_binding(intent, binding)
            return
        self._validate_binding(intent, binding)

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

    def _validate_recovery_binding(
        self,
        intent: AccountOwnerSubmitIntent,
        binding: AccountInstanceBinding,
    ) -> None:
        if binding.account_id != intent.account_id:
            self._reject(intent, "CLERK_ACCOUNT_MISMATCH")
        if binding.run_id != intent.run_id:
            self._reject(intent, "CLERK_STALE_RUN")
        if binding.bot_order_namespace != intent.bot_order_namespace:
            self._reject(intent, "CLERK_NAMESPACE_MISMATCH")
        if binding.lifecycle_state not in ("ACTIVE", "RETIRED"):
            self._reject(intent, "CLERK_INACTIVE_BINDING")

    def _validate_recovery_actor(
        self,
        intent: AccountOwnerSubmitIntent,
        *,
        actor: Literal["bot", "operator"],
        actor_strategy_instance_id: str | None,
        actor_run_id: str | None,
        actor_bot_order_namespace: str | None,
    ) -> None:
        bindings = read_account_instance_registry(self._artifacts_root, self._account_id)
        binding = latest_account_instance_binding(
            bindings,
            account_id=self._account_id,
            strategy_instance_id=intent.strategy_instance_id,
        )
        if binding is None:
            self._reject(intent, "CLERK_UNKNOWN_INSTANCE")
        assert binding is not None
        self._validate_recovery_binding(intent, binding)
        if actor == "operator":
            if binding.lifecycle_state != "RETIRED":
                self._reject(intent, "CLERK_OPERATOR_RECOVERY_REQUIRES_RETIRED_BINDING")
            return
        if (
            actor_strategy_instance_id != intent.strategy_instance_id
            or actor_run_id != intent.run_id
            or actor_bot_order_namespace != intent.bot_order_namespace
        ):
            self._reject(intent, "CLERK_RECOVERY_ACTOR_MISMATCH")

    def _validate_recovery_order(self, intent: AccountOwnerSubmitIntent) -> None:
        try:
            spec = IbkrOrderSpec.model_validate(intent.order_spec)
        except ValidationError as exc:
            self._reject(intent, f"CLERK_INVALID_RECOVERY_ORDER:{exc}")
        if spec.order_ref != intent.order_ref or spec.order_type != "MKT" or not spec.confirm_paper:
            self._reject(intent, "CLERK_INVALID_RECOVERY_ORDER")

    def _validate_recovery_exposure(
        self,
        intent: AccountOwnerSubmitIntent,
        spec: IbkrOrderSpec,
    ) -> None:
        """Require the recovery order to match this namespace's journal fill fold.

        This runs after terminal cancellation while the Clerk intake lock is
        held, making the durable journal rather than account-net positions the
        sole sizing authority for bot recovery.
        """

        from app.engine.live.journal_exposure import project_journal_exposure

        exposures = project_journal_exposure(
            read_account_clerk_journal(self._artifacts_root, self._account_id),
            account_id=self._account_id,
            group_by="namespace",
        )
        exposure = next(
            (
                row
                for row in exposures
                if row.group_id == intent.bot_order_namespace and row.symbol == spec.symbol.upper()
            ),
            None,
        )
        if exposure is None:
            self._reject(intent, "CLERK_RECOVERY_SYMBOL_NOT_OWNED")
        assert exposure is not None
        expected_action = "SELL" if exposure.quantity > 0 else "BUY"
        if spec.action != expected_action:
            self._reject(intent, "CLERK_RECOVERY_DIRECTION_MISMATCH")
        if float(spec.quantity) != abs(exposure.quantity):
            self._reject(intent, "CLERK_RECOVERY_QUANTITY_MISMATCH")

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

    async def _place_under_clerk_grant(self, spec: IbkrOrderSpec) -> Any:
        return await asyncio.wait_for(
            self._run_broker_write(
                "account_clerk.broker.place_order",
                lambda: self._broker.place_order(spec),
            ),
            timeout=_BROKER_SUBMIT_TIMEOUT_S,
        )

    async def _run_broker_write(
        self,
        boundary: str,
        write: Callable[[], Any],
        *,
        before_broker_write: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
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

    def _fence_stale_generation(self) -> None:
        if self._on_generation_fenced is not None:
            self._on_generation_fenced()

    def _reject(self, intent: AccountOwnerSubmitIntent, reason: str) -> None:
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


class AccountClerkLeaseWriter:
    """Renew one supervised clerk lease until the daemon reaps the process."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        account_id: str,
        generation: int,
        pid: int,
        ibkr_client_id: int | None = None,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._account_id = account_id
        self._generation = generation
        self._pid = pid
        self._ibkr_client_id = ibkr_client_id
        self._now_ms = now_ms
        self._started_at_ms = now_ms()

    def renew(self, *, draining: bool = False) -> AccountClerkLease:
        now_ms = self._now_ms()
        lease = AccountClerkLease(
            account_id=self._account_id,
            generation=self._generation,
            pid=self._pid,
            ibkr_client_id=self._ibkr_client_id,
            status="DRAINING" if draining else "RUNNING",
            started_at_ms=self._started_at_ms,
            renewed_at_ms=now_ms,
            valid_until_ms=now_ms if draining else now_ms + _CLERK_LEASE_TTL_MS,
        )
        write_account_clerk_lease(self._artifacts_root, lease)
        return lease


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
        )

        def on_callback_persistence_failure(_failure: BaseException) -> None:
            stream_failed.set()
            stop.set()

        server = AccountClerkRpcServer(
            clerk,
            on_callback_persistence_failure=on_callback_persistence_failure,
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
