"""Atomic, restart-safe custody-resolution orchestration for the Alpaca Clerk.

This mixin keeps the custody command state machine out of the Clerk facade. The
facade still owns the single intake lock, broker ports, journal, and primitive
recovery operations; this module composes those primitives into one guarded,
idempotent resolution transaction.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from app.broker.alpaca.clerk import derive, diagnosis, reconcile
from app.broker.alpaca.clerk.custody_resolution_store import CustodyResolutionStore
from app.broker.alpaca.clerk.journal import OrderJournal
from app.broker.alpaca.clerk.models import (
    ClerkEntryKind,
    OrderJournalEntry,
    ReconciliationVerdict,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerPosition


class InventoryBaselineRefusedError(Exception):
    """The account is not in a safe state for an inventory cutover."""

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


class ClerkCustodyResolutionOperations:
    """Custody diagnosis execution supplied to the account-rooted Clerk."""

    async def resolve_custody(
        self,
        *,
        operator: str,
        reason: str,
        snapshot_version: str,
        idempotency_key: str,
    ) -> diagnosis.CustodyResolutionReceipt:
        """Execute the diagnosed resolution plan, journaling the operator reason.

        Snapshot-guarded: rejects a stale token so the operator never resolves
        against evidence that changed after they looked. Idempotent: an already
        in-sync account resolves to a benign no-op.
        """
        store: CustodyResolutionStore | None = None
        reserved = False
        reconcile_only = False
        account_id = ""
        try:
            async with self._intake_lock:
                account_id, journal = await self._ensure_journal()
                store = self._resolution_store(journal)
                replay = await store.replay(idempotency_key)
                if replay is not None:
                    return replay

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
                    current = diagnosis.CustodyDiagnosis(
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
                    orders = []
                    positions = []
                else:
                    current = self._custody_diagnosis_from_snapshot(
                        account_id=account_id,
                        entries=entries,
                        orders=orders,
                        positions=positions,
                        namespaces=namespaces,
                        channel_fresh=channel_fresh,
                        bot_running=bot_running,
                    )

                if current.snapshot_version != snapshot_version:
                    raise diagnosis.CustodySnapshotChangedError()
                if not current.in_sync and not current.resolution_plan:
                    raise InventoryBaselineRefusedError(
                        "Custody resolution is blocked.",
                        detail=(
                            current.blocked_reason
                            or "Resolve the listed prerequisites and run diagnosis again."
                        ),
                    )

                await store.reserve(
                    key=idempotency_key, operator=operator, reason=reason
                )
                reserved = True

                if current.in_sync:
                    receipt = diagnosis.CustodyResolutionReceipt(
                        broker=self.broker_id,
                        account_id=account_id,
                        resolved=True,
                        receipt_id=uuid4().hex,
                        recorded_at_ms=self._clock(),
                        in_sync=True,
                    )
                    await store.complete(idempotency_key, receipt)
                    return receipt

                reconcile_only = bool(current.resolution_plan) and all(
                    not step.mutates for step in current.resolution_plan
                )
                if not reconcile_only:
                    receipt = await self._execute_custody_plan_locked(
                        current=current,
                        journal=journal,
                        account_id=account_id,
                        operator=operator,
                        reason=reason,
                        idempotency_key=idempotency_key,
                        orders=orders,
                        positions=positions,
                    )
                    await store.complete(idempotency_key, receipt)
                    return receipt

            if reconcile_only:
                verdict = await self.reconcile_once()
                after = await self.custody_diagnosis()
                async with self._intake_lock:
                    _, journal = await self._ensure_journal()
                    await self._append_resolution_audit_locked(
                        journal,
                        account_id=account_id,
                        operator=operator,
                        reason=reason,
                        idempotency_key=idempotency_key,
                        verdict=verdict,
                    )
                    receipt = diagnosis.CustodyResolutionReceipt(
                        broker=self.broker_id,
                        account_id=account_id,
                        resolved=after.in_sync,
                        receipt_id=uuid4().hex,
                        recorded_at_ms=self._clock(),
                        steps_executed=(
                            diagnosis.CustodyResolutionStepResult(
                                action_id="reconcile_now",
                                message=f"Reconciliation sweep: {verdict}.",
                            ),
                        ),
                        in_sync=after.in_sync,
                        remaining_divergences=after.divergences,
                    )
                    assert store is not None
                    await store.complete(idempotency_key, receipt)
                    return receipt
        except Exception as error:
            if reserved and store is not None:
                await store.fail(idempotency_key, str(error))
            raise

        raise RuntimeError("custody resolution produced no terminal receipt")

    def _channel_fresh(self) -> bool:
        return self._stream_health is None or not self._stream_health.broken()

    def _bot_running(self) -> bool:
        return self._bot_running_probe is not None and self._bot_running_probe()

    def _resolution_store(self, journal: OrderJournal) -> CustodyResolutionStore:
        if self._custody_resolution_store is None:
            self._custody_resolution_store = CustodyResolutionStore(journal.account_dir)
        return self._custody_resolution_store

    def _custody_diagnosis_from_snapshot(
        self,
        *,
        account_id: str,
        entries: list[OrderJournalEntry],
        orders: list[BrokerOrder],
        positions: list[BrokerPosition],
        namespaces: frozenset[str],
        channel_fresh: bool,
        bot_running: bool,
    ) -> diagnosis.CustodyDiagnosis:
        divergences = diagnosis.diagnose_custody(
            entries,
            orders=orders,
            positions=positions,
            namespaces=namespaces,
            channel_fresh=channel_fresh,
            bot_running=bot_running,
        )
        plan = diagnosis.resolution_plan(divergences)
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
            blocked_reason=next(
                (
                    item.prerequisite_detail
                    for item in divergences
                    if item.state == "blocked_on_prerequisite"
                ),
                None,
            ),
            divergences=divergences,
            resolution_plan=plan,
        )

    async def _execute_custody_plan_locked(
        self,
        *,
        current: diagnosis.CustodyDiagnosis,
        journal: OrderJournal,
        account_id: str,
        operator: str,
        reason: str,
        idempotency_key: str,
        orders: list[BrokerOrder],
        positions: list[BrokerPosition],
    ) -> diagnosis.CustodyResolutionReceipt:
        """Execute one mutating plan while the caller holds ``_intake_lock``."""

        steps: list[diagnosis.CustodyResolutionStepResult] = []
        for step in current.resolution_plan:
            if step.action_id == "reconcile_now":
                working_orders = [
                    order
                    for order in orders
                    if order.status.lower()
                    not in reconcile.RECONCILIATION_TERMINAL_ORDER_STATUSES
                ]
                plan = reconcile.plan(
                    journal.read_entries(),
                    working_orders,
                    positions,
                    self._known_namespaces(journal),
                    account_id=account_id,
                    now_ms=self._clock(),
                )
                verdict = await self._apply_reconcile_plan(journal, account_id, plan)
                steps.append(
                    diagnosis.CustodyResolutionStepResult(
                        action_id="reconcile_now",
                        message=f"Reconciliation sweep: {verdict}.",
                    )
                )
            elif step.action_id == "record_inventory_baseline":
                baseline = await self._record_inventory_baseline_locked(
                    journal=journal,
                    account_id=account_id,
                    operator=operator,
                    reason=reason,
                    strategy_instance_id=None,
                    orders=orders,
                    positions=positions,
                )
                position_summary = ", ".join(
                    f"{position.symbol} {position.signed_quantity:g}"
                    for position in baseline.positions
                )
                steps.append(
                    diagnosis.CustodyResolutionStepResult(
                        action_id="record_inventory_baseline",
                        message=(
                            "Adopted broker inventory as baseline: "
                            f"{position_summary or 'flat'}."
                        ),
                    )
                )
            elif step.action_id == "clear_hold":
                if not self._channel_fresh():
                    break
                await self._clear_hold_locked(
                    journal=journal,
                    account_id=account_id,
                    operator=operator,
                    reason=reason,
                )
                steps.append(
                    diagnosis.CustodyResolutionStepResult(
                        action_id="clear_hold", message="Exposure hold cleared."
                    )
                )

        final_entries = journal.read_entries()
        final_channel_fresh = self._channel_fresh()
        final_bot_running = self._bot_running()
        after = self._custody_diagnosis_from_snapshot(
            account_id=account_id,
            entries=final_entries,
            orders=orders,
            positions=positions,
            namespaces=self._known_namespaces(journal),
            channel_fresh=final_channel_fresh,
            bot_running=final_bot_running,
        )
        latest = derive.latest_reconciliation(final_entries)
        await self._append_resolution_audit_locked(
            journal,
            account_id=account_id,
            operator=operator,
            reason=reason,
            idempotency_key=idempotency_key,
            verdict=latest.verdict if latest is not None else "clean",
        )
        return diagnosis.CustodyResolutionReceipt(
            broker=self.broker_id,
            account_id=account_id,
            resolved=after.in_sync,
            receipt_id=uuid4().hex,
            recorded_at_ms=self._clock(),
            steps_executed=tuple(steps),
            in_sync=after.in_sync,
            remaining_divergences=after.divergences,
        )

    async def _append_resolution_audit_locked(
        self,
        journal: OrderJournal,
        *,
        account_id: str,
        operator: str,
        reason: str,
        idempotency_key: str,
        verdict: ReconciliationVerdict,
    ) -> None:
        await journal.append_async(
            OrderJournalEntry(
                kind=ClerkEntryKind.RECONCILIATION,
                account_id=account_id,
                operator=operator,
                reason=reason,
                verdict=verdict,
                effect_operation_id=f"custody-resolution:{idempotency_key}",
                recorded_at_ms=self._clock(),
            )
        )
