"""Durable Clerk-owned Alpaca effect-operation coordination.

This mixin keeps semantic ENTER/EXIT custody separate from the Clerk facade's
generic submission, cancellation, lifecycle, and reconciliation routes.  It
uses the same account journal and intake lock supplied by ``AlpacaClerk``;
there is no effect sidecar or second execution coordinator.
"""

from __future__ import annotations

import asyncio

from app.broker.alpaca.clerk import derive
from app.broker.alpaca.clerk.exposure import project_instance_exposure
from app.broker.alpaca.clerk.journal import OrderJournal
from app.broker.alpaca.clerk.models import (
    AlpacaEffectOperation,
    ClerkEntryKind,
    EffectOperationReceipt,
    EffectOperationState,
    EffectPurpose,
    OrderCancelResult,
    OrderJournalEntry,
    OrderLegError,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from app.engine.live.order_identity import build_bot_order_namespace, order_ref_namespace_matches
from app.schemas.action_plan import ActionPlan


class ClerkEffectOperations:
    """Effect-operation behavior supplied to the account-rooted Clerk.

    ``AlpacaClerk`` supplies the journal, lock, clock, broker id, submission,
    and cancellation primitives.  This class only turns durable semantic
    decisions into those existing primitives.
    """

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
    ) -> EffectOperationReceipt:
        """Accept one semantic ``ENTER``/``EXIT`` operation for a bot instance.

        A runtime provides only its restart-stable decision identity, semantic
        purpose, and immutable deployment plan.  It cannot supply a broker
        side, an order reference, fill state, or a claimed position.  Once this
        method reaches its durable acceptance row, a Clerk-owned task resolves
        the operation behind :func:`asyncio.shield`; cancelling the caller
        cannot cancel the Clerk's already accepted custody.
        """
        operation = AlpacaEffectOperation(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
        )
        return await self._await_effect(operation)

    async def _await_effect(self, operation: AlpacaEffectOperation) -> EffectOperationReceipt:
        key = (operation.strategy_instance_id, operation.decision_id)
        return await asyncio.shield(self._start_effect_task(operation, key))

    def _start_effect_task(
        self, operation: AlpacaEffectOperation, key: tuple[str, str] | None = None
    ) -> asyncio.Task[EffectOperationReceipt]:
        key = key or (operation.strategy_instance_id, operation.decision_id)
        task = self._effect_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._resolve_effect(operation),
                name=f"alpaca-effect:{operation.strategy_instance_id}:{operation.decision_id}",
            )
            self._effect_tasks[key] = task
            task.add_done_callback(
                lambda completed: self._clear_effect_task(key, completed)
            )
        return task

    def _clear_effect_task(
        self, key: tuple[str, str], completed: asyncio.Task[EffectOperationReceipt]
    ) -> None:
        """Forget only the task that completed, never a newer replay task."""
        if self._effect_tasks.get(key) is completed:
            self._effect_tasks.pop(key)

    async def _resolve_effect(self, operation: AlpacaEffectOperation) -> EffectOperationReceipt:
        """Advance one operation from journal facts available under the intake lock."""
        async with self._intake_lock:
            account_id, journal = await self._ensure_journal()
            entries = journal.read_entries()
            latest = self._latest_effect_receipt(entries, operation)
            if latest is None:
                accepted = EffectOperationReceipt(
                    operation=operation,
                    state=EffectOperationState.ACCEPTED,
                    explanation="The Account Clerk accepted this strategy decision for durable custody.",
                    next_step="Resolving the configured broker effect.",
                )
                await self._append_effect_receipt(journal, account_id, accepted, accepted=True)
                latest = accepted
            elif latest.state is EffectOperationState.UNCERTAIN:
                refreshed = self._refresh_uncertain_effect(entries, operation)
                if refreshed is not None:
                    await self._append_effect_receipt(journal, account_id, refreshed)
                    latest = refreshed
            if operation.purpose is EffectPurpose.ENTER:
                return await self._resolve_enter(operation, account_id, journal, entries, latest)
            return await self._resolve_exit(operation, account_id, journal, entries, latest)

    @staticmethod
    def _refresh_uncertain_effect(
        entries: list[OrderJournalEntry], operation: AlpacaEffectOperation
    ) -> EffectOperationReceipt | None:
        """Turn a recovered child outcome into a fresh effect receipt.

        A response-lost child intent is never re-submitted. S5 recovery owns
        resolving that child by its durable client order id; this method only
        reflects an already-journaled terminal child fact at the operation
        boundary.
        """
        child_outcome: ClerkEntryKind | None = None
        child_error: str | None = None
        for entry in entries:
            if entry.effect_operation_id != operation.decision_id:
                continue
            if entry.kind is ClerkEntryKind.SUBMIT_ACKED:
                child_outcome = entry.kind
                child_error = None
            elif entry.kind is ClerkEntryKind.SUBMIT_FAILED:
                child_outcome = entry.kind
                child_error = entry.error_detail
        if child_outcome is None:
            return None
        if child_outcome is ClerkEntryKind.SUBMIT_FAILED:
            return EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.REJECTED,
                explanation="The Clerk resolved the uncertain broker submission as rejected.",
                next_step=child_error,
                child_order_refs=ClerkEffectOperations._effect_child_refs(entries, operation),
            )
        state = (
            EffectOperationState.SUBMITTED
            if operation.purpose is EffectPurpose.ENTER
            else EffectOperationState.EXIT_PENDING
        )
        return EffectOperationReceipt(
            operation=operation,
            state=state,
            explanation="The Clerk recovered the previously uncertain broker submission.",
            next_step=(
                "Await the broker lifecycle callback."
                if state is EffectOperationState.SUBMITTED
                else "Await terminal fill evidence."
            ),
            child_order_refs=ClerkEffectOperations._effect_child_refs(entries, operation),
        )

    @staticmethod
    def _latest_effect_receipt(
        entries: list[OrderJournalEntry], operation: AlpacaEffectOperation
    ) -> EffectOperationReceipt | None:
        latest: EffectOperationReceipt | None = None
        for entry in entries:
            receipt = entry.effect_receipt
            if (
                receipt is not None
                and receipt.operation.strategy_instance_id == operation.strategy_instance_id
                and receipt.operation.decision_id == operation.decision_id
            ):
                latest = receipt
        return latest

    @staticmethod
    def _effect_operation_for_decision(
        entries: list[OrderJournalEntry], strategy_instance_id: str, decision_id: str
    ) -> AlpacaEffectOperation | None:
        latest: EffectOperationReceipt | None = None
        for entry in entries:
            receipt = entry.effect_receipt
            if (
                receipt is not None
                and receipt.operation.strategy_instance_id == strategy_instance_id
                and receipt.operation.decision_id == decision_id
            ):
                latest = receipt
        return latest.operation if latest is not None else None

    async def _append_effect_receipt(
        self,
        journal: OrderJournal,
        account_id: str,
        receipt: EffectOperationReceipt,
        *,
        accepted: bool = False,
    ) -> None:
        await journal.append_async(
            OrderJournalEntry(
                kind=(ClerkEntryKind.EFFECT_ACCEPTED if accepted else ClerkEntryKind.EFFECT_RECEIPT),
                account_id=account_id,
                recorded_at_ms=self._clock(),
                effect_operation_id=receipt.operation.decision_id,
                effect_receipt=receipt,
            )
        )

    async def _resolve_enter(
        self,
        operation: AlpacaEffectOperation,
        account_id: str,
        journal: OrderJournal,
        entries: list[OrderJournalEntry],
        latest: EffectOperationReceipt,
    ) -> EffectOperationReceipt:
        if latest.state is not EffectOperationState.ACCEPTED:
            return latest
        hold = derive.hold_state(entries)
        if hold.active:
            receipt = EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.REJECTED,
                explanation="Entry was blocked because the Account Clerk has an active account safety hold.",
                next_step=hold.reason,
            )
            await self._append_effect_receipt(journal, account_id, receipt)
            return receipt
        namespace = build_bot_order_namespace(operation.strategy_instance_id)
        side = OrderSide.BUY if operation.entry_leg.position == "long" else OrderSide.SELL
        exposure = project_instance_exposure(entries, namespace=namespace)
        configured_exposure = next(
            (row for row in exposure if row.symbol == operation.entry_leg.instrument.underlying), None
        )
        same_direction = configured_exposure is not None and (
            configured_exposure.quantity > 0 if side is OrderSide.BUY else configured_exposure.quantity < 0
        )
        if same_direction or self._has_working_entry(
            entries, namespace, side, operation.entry_leg.instrument.underlying
        ):
            receipt = EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.NOOP,
                explanation="Entry was already satisfied by Clerk-attributed exposure or a working entry order.",
                child_order_refs=self._effect_child_refs(entries, operation),
            )
            await self._append_effect_receipt(journal, account_id, receipt)
            return receipt
        leg = BrokerOrderLeg(
            symbol=operation.entry_leg.instrument.underlying,
            side=side,
            quantity=float(operation.quantity * operation.entry_leg.qty_ratio),
        )
        result = await self._submit_leg(
            operation.strategy_instance_id,
            leg,
            account_id,
            journal,
            namespace=namespace,
            effect_operation_id=operation.decision_id,
        )
        state = {
            "acked": EffectOperationState.SUBMITTED,
            "failed": EffectOperationState.REJECTED,
            "uncertain": EffectOperationState.UNCERTAIN,
        }[result.status]
        receipt = EffectOperationReceipt(
            operation=operation,
            state=state,
            explanation=(
                "The Clerk submitted the configured entry and is waiting for broker lifecycle evidence."
                if state is EffectOperationState.SUBMITTED
                else "The Clerk could not prove an entry submission outcome."
            ),
            next_step=(
                "Await the broker lifecycle callback."
                if state is EffectOperationState.SUBMITTED
                else result.error.why if result.error else None
            ),
            child_order_refs=(result.order_ref,),
        )
        await self._append_effect_receipt(journal, account_id, receipt)
        return receipt

    async def _resolve_exit(
        self,
        operation: AlpacaEffectOperation,
        account_id: str,
        journal: OrderJournal,
        entries: list[OrderJournalEntry],
        latest: EffectOperationReceipt,
    ) -> EffectOperationReceipt:
        if latest.state in {
            EffectOperationState.FLAT,
            EffectOperationState.REJECTED,
            EffectOperationState.UNPROVABLE,
            EffectOperationState.UNCERTAIN,
        }:
            return latest
        namespace = build_bot_order_namespace(operation.strategy_instance_id)
        working = self._working_entries(entries, namespace, operation.entry_leg.instrument.underlying)
        uncancelled = [
            entry
            for entry in working
            if entry.order is not None and not self._cancel_was_acked(entries, entry.order.order_id)
        ]
        if uncancelled:
            cancellations = [
                await self._cancel_owned_entry(journal, account_id, entry)
                for entry in uncancelled
            ]
            failed_cancel = next(
                (result for result in cancellations if result.status == "failed"), None
            )
            if failed_cancel is not None:
                receipt = EffectOperationReceipt(
                    operation=operation,
                    state=EffectOperationState.UNPROVABLE,
                    explanation=(
                        "The Clerk could not prove that every working entry was cancelled, "
                        "so it did not derive a reducing quantity."
                    ),
                    next_step=(
                        failed_cancel.error.why
                        if failed_cancel.error is not None
                        else "Inspect the working broker order before retrying the exit."
                    ),
                    child_order_refs=tuple(entry.order_ref for entry in uncancelled),
                )
                await self._append_effect_receipt(journal, account_id, receipt)
                return receipt
            receipt = EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.EXIT_PENDING,
                explanation="The Clerk cancelled working entry orders before deriving the final reducing quantity.",
                next_step="Await terminal cancellation or fill evidence, then the Clerk will re-evaluate exposure.",
                child_order_refs=tuple(entry.order_ref for entry in uncancelled),
            )
            await self._append_effect_receipt(journal, account_id, receipt)
            return receipt
        if working:
            return latest
        exposure = next(
            (
                row
                for row in project_instance_exposure(entries, namespace=namespace)
                if row.symbol == operation.entry_leg.instrument.underlying
            ),
            None,
        )
        if exposure is None:
            receipt = EffectOperationReceipt(
                operation=operation,
                state=EffectOperationState.FLAT,
                explanation="The Clerk verified this instance has zero attributed exposure.",
            )
            await self._append_effect_receipt(journal, account_id, receipt)
            return receipt
        leg = BrokerOrderLeg(
            symbol=exposure.symbol,
            side=OrderSide.SELL if exposure.quantity > 0 else OrderSide.BUY,
            quantity=abs(exposure.quantity),
        )
        result = await self._submit_leg(
            operation.strategy_instance_id,
            leg,
            account_id,
            journal,
            namespace=namespace,
            effect_operation_id=operation.decision_id,
        )
        state = EffectOperationState.EXIT_PENDING if result.status == "acked" else (
            EffectOperationState.REJECTED if result.status == "failed" else EffectOperationState.UNCERTAIN
        )
        receipt = EffectOperationReceipt(
            operation=operation,
            state=state,
            explanation=(
                "The Clerk submitted an exact reducing order for the current attributed quantity."
                if state is EffectOperationState.EXIT_PENDING
                else "The Clerk could not complete the requested exit; attributed exposure remains open until proven otherwise."
            ),
            next_step=(
                "Await terminal fill evidence."
                if state is EffectOperationState.EXIT_PENDING
                else result.error.why if result.error else None
            ),
            child_order_refs=(result.order_ref,),
        )
        await self._append_effect_receipt(journal, account_id, receipt)
        return receipt

    @staticmethod
    def _effect_child_refs(
        entries: list[OrderJournalEntry], operation: AlpacaEffectOperation
    ) -> tuple[str, ...]:
        return tuple(
            entry.order_ref
            for entry in entries
            if entry.effect_operation_id == operation.decision_id and entry.order_ref
        )

    @staticmethod
    def _working_entries(
        entries: list[OrderJournalEntry], namespace: str, symbol: str
    ) -> list[OrderJournalEntry]:
        terminal_refs = {
            entry.order_ref
            for entry in entries
            if entry.kind is ClerkEntryKind.ORDER_EVENT
            and entry.event is not None
            and entry.event.event_type in {"fill", "canceled", "rejected", "expired"}
            and entry.order_ref
        }
        return [
            entry
            for entry in entries
            if entry.kind is ClerkEntryKind.SUBMIT_ACKED
            and entry.order is not None
            and entry.order_ref not in terminal_refs
            and entry.leg is not None
            and entry.leg.symbol == symbol
            and order_ref_namespace_matches(entry.order_ref, frozenset({namespace}))
        ]

    @classmethod
    def _has_working_entry(
        cls, entries: list[OrderJournalEntry], namespace: str, side: OrderSide, symbol: str
    ) -> bool:
        return any(
            entry.leg is not None and entry.leg.side is side
            for entry in cls._working_entries(entries, namespace, symbol)
        )

    @staticmethod
    def _cancel_was_acked(entries: list[OrderJournalEntry], order_id: str | None) -> bool:
        return order_id is not None and any(
            entry.kind is ClerkEntryKind.CANCEL_ACKED and entry.broker_order_id == order_id
            for entry in entries
        )

    async def _cancel_owned_entry(
        self, journal: OrderJournal, account_id: str, entry: OrderJournalEntry
    ) -> OrderCancelResult:
        assert entry.order is not None
        order_id = entry.order.order_id
        recorded = OrderJournalEntry(
            kind=ClerkEntryKind.CANCEL_RECORDED,
            account_id=account_id,
            operator=entry.operator,
            intent_id=entry.intent_id,
            order_ref=entry.order_ref,
            client_order_id=entry.client_order_id,
            leg=entry.leg,
            broker_order_id=order_id,
            owned=True,
            recorded_at_ms=self._clock(),
            effect_operation_id=entry.effect_operation_id,
        )
        await journal.append_async(recorded)
        try:
            await self._trade.cancel(order_id)
        except BrokerError as exc:
            await journal.append_async(recorded.model_copy(update={
                "kind": ClerkEntryKind.CANCEL_FAILED,
                "recorded_at_ms": self._clock(),
                "error_message": exc.message,
                "error_detail": exc.detail,
            }))
            return OrderCancelResult(
                broker=self.broker_id,
                account_id=account_id,
                order_id=order_id,
                status="failed",
                owned=True,
                order_ref=entry.order_ref,
                error=OrderLegError(message=exc.message, why=exc.detail),
            )
        await journal.append_async(recorded.model_copy(update={
            "kind": ClerkEntryKind.CANCEL_ACKED,
            "recorded_at_ms": self._clock(),
        }))
        return OrderCancelResult(
            broker=self.broker_id,
            account_id=account_id,
            order_id=order_id,
            status="acked",
            owned=True,
            order_ref=entry.order_ref,
        )

    async def read_effect_receipts_for_instance(
        self, strategy_instance_id: str
    ) -> tuple[EffectOperationReceipt, ...]:
        """Project each effect's latest backend-authored receipt from the journal.

        The projection returns semantic state, trader-safe prose, operator next
        steps, and exact order references as durable Clerk values. Presentation
        code receives this completed meaning; it does not fold journal rows or
        manufacture safety copy.
        """
        entries = await self.read_journal_entries()
        latest: dict[str, EffectOperationReceipt] = {}
        for entry in entries:
            receipt = entry.effect_receipt
            if (
                receipt is not None
                and receipt.operation.strategy_instance_id == strategy_instance_id
            ):
                latest[receipt.operation.decision_id] = receipt
        return tuple(latest.values())
