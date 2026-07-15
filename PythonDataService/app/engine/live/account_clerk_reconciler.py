"""Clerk-owned reconciliation of durable intent receipts and broker state."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass

from app.engine.live.account_artifacts import AccountFreezeEvidence, append_account_event, write_account_freeze
from app.engine.live.account_clerk import AccountClerk, AccountClerkJournalEntry, read_account_clerk_journal
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.journal_exposure import project_journal_exposure
from app.engine.live.submit_state_machine import BrokerProbe, SubmitVerdict, next_action

_CADENCE_SECONDS = 5.0


@dataclass(frozen=True)
class NamespaceExposure:
    """Journal-derived signed exposure for one namespace and symbol."""

    bot_order_namespace: str
    symbol: str
    quantity: float


@dataclass(frozen=True)
class ReconciliationResolution:
    intent_id: str
    order_ref: str
    verdict: SubmitVerdict
    reason: str


def namespace_expected_exposure(entries: list[AccountClerkJournalEntry]) -> tuple[NamespaceExposure, ...]:
    """Return the canonical journal projection grouped by namespace."""

    return tuple(
        NamespaceExposure(exposure.group_id, exposure.symbol, exposure.quantity)
        for exposure in project_journal_exposure(entries, group_by="namespace")
    )


class AccountClerkReconciler:
    """Resolve uncertain Clerk receipts without guessing or silently repairing."""

    def __init__(
        self,
        clerk: AccountClerk,
        *,
        cadence_seconds: float = _CADENCE_SECONDS,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._clerk = clerk
        self._cadence_seconds = cadence_seconds
        self._now_ms = now_ms if now_ms is not None else lambda: time.time_ns() // 1_000_000
        self._task: asyncio.Task[None] | None = None

    async def reconcile_once(self) -> tuple[ReconciliationResolution, ...]:
        entries = await asyncio.to_thread(
            read_account_clerk_journal, self._clerk._artifacts_root, self._clerk._account_id
        )
        resolutions: list[ReconciliationResolution] = []
        for intent, retry_count in _unresolved_intents(entries).values():
            if intent.intent_kind == "CANCEL_NAMESPACE" and self._clerk.cancel_namespace_in_progress:
                continue
            resolution = await self._resolve(intent, retry_count)
            resolutions.append(resolution)
            append_account_event(
                self._clerk._artifacts_root,
                self._clerk._account_id,
                {
                    "event_type": "account_clerk_reconciliation_resolved",
                    "ts_ms": self._now_ms(),
                    "intent_id": resolution.intent_id,
                    "order_ref": resolution.order_ref,
                    "verdict": resolution.verdict.value,
                    "reason": resolution.reason,
                    "namespace_exposure": [
                        exposure.__dict__ for exposure in namespace_expected_exposure(entries)
                    ],
                },
            )
        return tuple(resolutions)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="account-clerk-reconciler")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            await self.reconcile_once()
            await asyncio.sleep(self._cadence_seconds)

    async def _resolve(self, intent, retry_count: int) -> ReconciliationResolution:
        if intent.intent_kind == "CANCEL_NAMESPACE":
            return await self._resolve_uncertain_cancel_namespace(intent)
        probe = await self._probe(intent.intent_id, intent.order_ref)
        verdict = next_action(
            current_status=_ack_failed_uncertain_status(),
            probe=probe,
            retry_count=retry_count,
        )
        reason = f"probe={probe.value}; retry_count={retry_count}"
        await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        if verdict is SubmitVerdict.RETRY_ONCE:
            try:
                await self._clerk.retry_recorded_intent(intent)
            except Exception as exc:
                reason = f"{reason}; retry raised {type(exc).__name__}: {exc}"
        elif verdict is SubmitVerdict.HALT:
            write_account_freeze(
                self._clerk._artifacts_root,
                AccountFreezeEvidence(
                    account_id=self._clerk._account_id,
                    freeze_kind="exposure",
                    reason="ACCOUNT_CLERK_RECONCILIATION_NOT_PROVABLE",
                    source="account_clerk_reconciler",
                    recorded_at_ms=self._now_ms(),
                    operator_next_step="CHECK_IBKR",
                ),
            )
        return ReconciliationResolution(intent.intent_id, intent.order_ref, verdict, reason)

    async def _resolve_uncertain_cancel_namespace(
        self,
        intent: AccountOwnerSubmitIntent,
    ) -> ReconciliationResolution:
        """Resolve a fenced cancellation without allowing a blind submit.

        A namespace with no remaining broker orders is terminal for the
        cancellation because the Clerk gate has blocked all later writes to
        that namespace.  If orders remain, cancellation is idempotent and may
        be retried under the Clerk's write grant.  Any unprovable view halts.
        """

        probe = await self._probe_namespace_cancel(intent.bot_order_namespace)
        reason = f"cancel_probe={probe.value}"
        if probe is BrokerProbe.PROVABLY_ABSENT:
            verdict = SubmitVerdict.RECOVER_ADOPT
            await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        elif probe is BrokerProbe.PRESENT:
            try:
                await self._clerk.resolve_uncertain_cancel_namespace(intent)
            except Exception as exc:
                verdict = SubmitVerdict.HALT
                reason = f"{reason}; cancel retry raised {type(exc).__name__}: {exc}"
                await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
                self._freeze_unprovable_namespace_cancel()
            else:
                verdict = SubmitVerdict.RECOVER_ADOPT
                reason = f"{reason}; cancel retry confirmed"
                await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        else:
            verdict = SubmitVerdict.HALT
            await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
            self._freeze_unprovable_namespace_cancel()
        return ReconciliationResolution(intent.intent_id, intent.order_ref, verdict, reason)

    async def _probe(self, intent_id: str, order_ref: str) -> BrokerProbe:
        probe_fn = getattr(self._clerk._broker, "probe_intent_status", None)
        if not callable(probe_fn):
            return BrokerProbe.NOT_PROVABLE
        try:
            return BrokerProbe(await probe_fn(intent_id, order_ref))
        except Exception:
            return BrokerProbe.NOT_PROVABLE

    async def _probe_namespace_cancel(self, namespace: str) -> BrokerProbe:
        probe_fn = getattr(self._clerk._broker, "probe_namespace_cancel_status", None)
        if not callable(probe_fn):
            return BrokerProbe.NOT_PROVABLE
        try:
            return BrokerProbe(await probe_fn(namespace))
        except Exception:
            return BrokerProbe.NOT_PROVABLE

    def _freeze_unprovable_namespace_cancel(self) -> None:
        write_account_freeze(
            self._clerk._artifacts_root,
            AccountFreezeEvidence(
                account_id=self._clerk._account_id,
                freeze_kind="exposure",
                reason="ACCOUNT_CLERK_CANCEL_NAMESPACE_NOT_PROVABLE",
                source="account_clerk_reconciler",
                recorded_at_ms=self._now_ms(),
                operator_next_step="CHECK_IBKR",
            ),
        )


def _unresolved_intents(
    entries: list[AccountClerkJournalEntry],
) -> Mapping[str, tuple[AccountOwnerSubmitIntent, int]]:
    """Return crash-recovered or explicitly uncertain intents only.

    ``broker_submitting`` is written before the Clerk awaits the broker.  It
    prevents the cadence loop from racing an in-flight original attempt.
    """

    recorded: dict[str, AccountOwnerSubmitIntent] = {}
    terminal: set[str] = set()
    retries: dict[str, int] = defaultdict(int)
    submitting: set[str] = set()
    uncertain: set[str] = set()
    for entry in entries:
        # A callback with no durable Clerk intent is deliberately retained as
        # account truth, but it is never an intent-reconciliation candidate.
        # Skip it before dereferencing the optional attribution payload.
        if entry.intent is None:
            continue
        intent_id = entry.intent.intent_id
        if entry.entry_kind == "recorded":
            recorded[intent_id] = entry.intent
        elif entry.entry_kind == "broker_submitting":
            submitting.add(intent_id)
        elif entry.entry_kind in {"broker_uncertain", "cancel_uncertain"}:
            uncertain.add(intent_id)
        elif entry.entry_kind == "broker_acked":
            terminal.add(intent_id)
        elif entry.entry_kind == "reconciliation":
            if entry.reconciliation_verdict in {"RECOVER_ADOPT", "HALT"}:
                terminal.add(intent_id)
            elif entry.reconciliation_verdict == "RETRY_ONCE":
                retries[intent_id] += 1
    return {
        intent_id: (intent, retries[intent_id])
        for intent_id, intent in recorded.items()
        if intent_id not in terminal
        and (
            intent_id in uncertain
            or (intent.intent_kind == "CANCEL_NAMESPACE" and intent_id in submitting)
            or (intent.intent_kind != "CANCEL_NAMESPACE" and intent_id not in submitting)
        )
    }


def _ack_failed_uncertain_status():
    from app.engine.live.intent_events import IntentEventType

    return IntentEventType.ACK_FAILED_UNCERTAIN


__all__ = [
    "AccountClerkReconciler",
    "NamespaceExposure",
    "ReconciliationResolution",
    "namespace_expected_exposure",
]
