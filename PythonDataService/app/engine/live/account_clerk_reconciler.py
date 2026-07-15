"""Clerk-owned reconciliation of durable intent receipts and broker state."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass

from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    append_account_event,
    read_account_events,
    read_account_freeze,
    write_account_freeze,
)
from app.engine.live.account_clerk import (
    ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S,
    AccountClerk,
    AccountClerkGenerationFencedError,
    AccountClerkJournalEntry,
    read_account_clerk_journal,
)
from app.engine.live.account_owner import AccountOwnerSubmitIntent
from app.engine.live.journal_exposure import project_journal_exposure
from app.engine.live.submit_state_machine import BrokerProbe, SubmitVerdict

_CADENCE_SECONDS = 5.0
_SUBMIT_IN_FLIGHT_TTL_MS = 30_000 + 30_000
_RECOVERY_IN_FLIGHT_TTL_MS = 120_000 + 30_000


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
        entries = await self._clerk.reconciliation_snapshot()
        self._reassert_missing_halt_freeze(entries)
        resolutions: list[ReconciliationResolution] = []
        for intent, retry_count in _unresolved_intents(entries, now_ms=self._now_ms()).values():
            if intent.intent_kind == "CANCEL_NAMESPACE" and self._clerk.cancel_namespace_in_progress:
                continue
            resolution = await self._resolve(intent, retry_count)
            if resolution is None:
                continue
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

    def _reassert_missing_halt_freeze(self, entries: list[AccountClerkJournalEntry]) -> None:
        """Repair the journal-to-freeze crash window without rewriting history."""

        if read_account_freeze(self._clerk._artifacts_root, self._clerk._account_id) is not None:
            return
        halt = _latest_unresolved_halt(entries)
        if halt is None:
            return
        events = read_account_events(self._clerk._artifacts_root, self._clerk._account_id)
        if _halt_freeze_was_cleared(events, halt):
            return
        self._write_halt_freeze()

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

    async def _resolve(self, intent, retry_count: int) -> ReconciliationResolution | None:
        if intent.intent_kind == "CANCEL_NAMESPACE":
            return await self._resolve_uncertain_cancel_namespace(intent)
        outcome = await self._clerk.reconcile_uncertain_intent(intent, retry_count=retry_count)
        if outcome is None:
            return None
        verdict = SubmitVerdict(outcome.verdict)
        if verdict is SubmitVerdict.HALT:
            self._write_halt_freeze()
        return ReconciliationResolution(outcome.intent_id, outcome.order_ref, verdict, outcome.reason)

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
            await self._clerk.finalize_adopted_cancel_namespace(intent)
            await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        elif probe is BrokerProbe.PRESENT:
            try:
                await self._clerk.resolve_uncertain_cancel_namespace(intent)
            except AccountClerkGenerationFencedError:
                raise
            except Exception as exc:
                verdict = SubmitVerdict.HALT
                reason = f"{reason}; cancel retry raised {type(exc).__name__}: {exc}"
                self._freeze_unprovable_namespace_cancel()
                await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
            else:
                verdict = SubmitVerdict.RECOVER_ADOPT
                reason = f"{reason}; cancel retry confirmed"
                await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        else:
            verdict = SubmitVerdict.HALT
            self._freeze_unprovable_namespace_cancel()
            await self._clerk.append_reconciliation_resolution(intent, verdict=verdict.value, reason=reason)
        return ReconciliationResolution(intent.intent_id, intent.order_ref, verdict, reason)

    async def _probe_namespace_cancel(self, namespace: str) -> BrokerProbe:
        probe_fn = getattr(self._clerk._broker, "probe_namespace_cancel_status", None)
        if not callable(probe_fn):
            return BrokerProbe.NOT_PROVABLE
        try:
            return BrokerProbe(
                await asyncio.wait_for(
                    probe_fn(namespace),
                    timeout=ACCOUNT_CLERK_CANCEL_NAMESPACE_TIMEOUT_S,
                )
            )
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

    def _write_halt_freeze(self) -> None:
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

def _latest_unresolved_halt(
    entries: list[AccountClerkJournalEntry],
) -> AccountClerkJournalEntry | None:
    """Return the newest HALT that no later adoption superseded."""

    halts: dict[str, AccountClerkJournalEntry] = {}
    for entry in entries:
        if entry.intent is None or entry.entry_kind != "reconciliation":
            continue
        if entry.reconciliation_verdict == "HALT":
            halts[entry.intent.intent_id] = entry
        elif entry.reconciliation_verdict == "RECOVER_ADOPT":
            halts.pop(entry.intent.intent_id, None)
    return max(halts.values(), key=lambda entry: entry.seq, default=None)


def _halt_freeze_was_cleared(
    account_events: list[dict],
    halt: AccountClerkJournalEntry,
) -> bool:
    """A later audited clear is not the crash window this repair owns."""

    return any(
        event.get("event_type") == "account_freeze_cleared"
        and event.get("source") == "account_clerk_reconciler"
        and event.get("reason") == "ACCOUNT_CLERK_RECONCILIATION_NOT_PROVABLE"
        and isinstance(event.get("cleared_at_ms"), int)
        and event["cleared_at_ms"] >= halt.recorded_at_ms
        for event in account_events
    )


def _unresolved_intents(
    entries: list[AccountClerkJournalEntry],
    *,
    now_ms: int,
) -> Mapping[str, tuple[AccountOwnerSubmitIntent, int]]:
    """Return crash-recovered or explicitly uncertain intents only.

    ``broker_submitting`` is written before the Clerk awaits the broker.  It
    prevents the cadence loop from racing an in-flight original attempt.
    """

    recorded: dict[str, AccountOwnerSubmitIntent] = {}
    terminal: set[str] = set()
    retries: dict[str, int] = defaultdict(int)
    submitting_at_ms: dict[str, int] = {}
    uncertain_at_ms: dict[str, int] = {}
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
            submitting_at_ms[intent_id] = entry.recorded_at_ms
        elif entry.entry_kind in {"broker_uncertain", "cancel_uncertain"}:
            uncertain_at_ms[intent_id] = entry.recorded_at_ms
        elif entry.entry_kind in {"broker_acked", "cancel_confirmed"}:
            terminal.add(intent_id)
        elif entry.entry_kind == "reconciliation":
            if entry.reconciliation_verdict in {"RECOVER_ADOPT", "HALT"}:
                terminal.add(intent_id)
            elif entry.reconciliation_verdict == "RETRY_ONCE":
                retries[intent_id] += 1
    candidates: dict[str, tuple[AccountOwnerSubmitIntent, int]] = {}
    for intent_id, intent in recorded.items():
        if intent_id in terminal:
            continue
        submitted_at_ms = submitting_at_ms.get(intent_id)
        uncertainty_at_ms = uncertain_at_ms.get(intent_id)
        if intent.intent_kind == "CANCEL_NAMESPACE":
            if uncertainty_at_ms is not None or submitted_at_ms is not None:
                candidates[intent_id] = (intent, retries[intent_id])
            continue
        if intent.intent_kind == "RECOVERY_FLATTEN" and submitted_at_ms is None and uncertainty_at_ms is None:
            continue
        if submitted_at_ms is None or (
            uncertainty_at_ms is not None and uncertainty_at_ms > submitted_at_ms
        ):
            candidates[intent_id] = (intent, retries[intent_id])
            continue
        ttl_ms = (
            _RECOVERY_IN_FLIGHT_TTL_MS
            if intent.intent_kind == "RECOVERY_FLATTEN"
            else _SUBMIT_IN_FLIGHT_TTL_MS
        )
        if now_ms - submitted_at_ms >= ttl_ms:
            candidates[intent_id] = (intent, retries[intent_id])
    return candidates


__all__ = [
    "AccountClerkReconciler",
    "NamespaceExposure",
    "ReconciliationResolution",
    "namespace_expected_exposure",
]
