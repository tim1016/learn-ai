"""Active-authority boundary for Alpaca ``trade_updates`` evidence.

The websocket owns transport, capture, parsing, and in-connection duplicate
suppression.  It does not know which durable Clerk is active.  These sinks are
the only authority-specific part of that flow, which prevents a frame from
being written to both the legacy JSONL ledger and the SQLite authority.
"""

from __future__ import annotations

from typing import Protocol

from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.sqlite.broker_port_guard import guard_broker_read_port
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import (
    AccountHoldRaisedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_acknowledgement,
    fold_order_evidence,
)
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderEvent
from app.broker.contract.ports import BrokerReadPort

UNEXPLAINED_TRADE_UPDATE_REASON_CODE = "UNEXPLAINED_ORDER"


class TradeUpdateEvidenceSink(Protocol):
    """The durable destination selected for one account at process boot."""

    def guard_reconnect_read(self, read: BrokerReadPort) -> BrokerReadPort:
        """Return the read port normalized for this authority's intake domain."""

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None,
        recovery_source: str | None,
        recovery_window_limit: int | None,
    ) -> ClerkEntryKind: ...

    async def recover_activity_window(
        self,
        *,
        read: BrokerReadPort,
        limit: int,
    ) -> int: ...

    async def reconcile_gap(self) -> None: ...


class SqliteReconciliationFacade(Protocol):
    """The active facade owns broker access for account reconciliation."""

    async def reconcile_account(
        self,
        *,
        trigger: str,
    ) -> AccountReconciliationResult: ...


class ActivityRecoveryRecorder(Protocol):
    async def cursor_ms(self) -> int | None: ...

    async def record(
        self,
        *,
        activity: BrokerActivity,
        window_limit: int,
    ) -> bool: ...


class LegacyLifecycleRecorder(Protocol):
    @property
    def activity_recovery(self) -> ActivityRecoveryRecorder: ...

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None,
        recovery_source: str | None,
        recovery_window_limit: int | None,
    ) -> ClerkEntryKind: ...


class LegacyTradeUpdateEvidenceSink:
    """Keep the established JSONL lifecycle behavior behind the new boundary."""

    def __init__(self, clerk: LegacyLifecycleRecorder) -> None:
        self._clerk = clerk

    def guard_reconnect_read(self, read: BrokerReadPort) -> BrokerReadPort:
        return read

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None,
        recovery_source: str | None,
        recovery_window_limit: int | None,
    ) -> ClerkEntryKind:
        return await self._clerk.record_lifecycle_event(
            client_order_id=client_order_id,
            event=event,
            event_key=event_key,
            order=order,
            recovery_source=recovery_source,
            recovery_window_limit=recovery_window_limit,
        )

    async def recover_activity_window(
        self,
        *,
        read: BrokerReadPort,
        limit: int,
    ) -> int:
        recorded = 0
        activity_recovery = self._clerk.activity_recovery
        cursor = await activity_recovery.cursor_ms()
        activities = await read.list_activities(after_ms=cursor, limit=limit)
        for activity in sorted(
            activities,
            key=lambda item: item.occurred_at_ms if item.occurred_at_ms is not None else -1,
        ):
            if await activity_recovery.record(activity=activity, window_limit=limit):
                recorded += 1
        return recorded

    async def reconcile_gap(self) -> None:
        # The legacy consumer retains its bounded closed-order/activity recovery
        # below. Its full periodic reconciliation remains independently owned by
        # the established sweep.
        return


class SqliteTradeUpdateEvidenceSink:
    """Fold lifecycle evidence only into the activated SQLite authority."""

    def __init__(
        self,
        *,
        repo: ClerkSqliteRepository,
        intake: ReentrantAsyncLock,
        reconciler: SqliteReconciliationFacade,
    ) -> None:
        self._repo = repo
        self._intake = intake
        self._reconciler = reconciler

    @property
    def intake(self) -> ReentrantAsyncLock:
        """The shared SQLite fold fence used to guard reconnect reads."""
        return self._intake

    def guard_reconnect_read(self, read: BrokerReadPort) -> BrokerReadPort:
        """Keep reconnect REST reads outside, and forbidden from, SQLite intake."""
        return guard_broker_read_port(read, intake=self._intake)

    async def record_lifecycle_event(
        self,
        *,
        client_order_id: str | None,
        event: BrokerOrderEvent,
        event_key: str,
        order: BrokerOrder | None,
        recovery_source: str | None,
        recovery_window_limit: int | None,
    ) -> ClerkEntryKind:
        del recovery_window_limit
        async with self._intake:
            local_order = self._repo.order(client_order_id) if client_order_id else None
            if local_order is None and order is not None:
                # This broker identity is not captured by any bot-owned
                # order.  Persist it separately from bot economics; the
                # observation fold raises its own atomic account hold.
                observe_external_order(
                    self._repo,
                    order=order,
                    proof_reference=event_key,
                )
                return ClerkEntryKind.UNEXPLAINED_ORDER

            if local_order is None or order is None:
                evidence_refs = tuple(
                    ref
                    for ref in (
                        event_key,
                        client_order_id,
                        order.order_id if order is not None else None,
                    )
                    if ref is not None
                )
                facts = AccountHoldRaisedFacts(
                    reason_code=UNEXPLAINED_TRADE_UPDATE_REASON_CODE,
                    evidence_refs=list(evidence_refs),
                )

                def _transition(kind: str) -> TransitionInput:
                    return TransitionInput(
                        transition_kind=kind,
                        custody_owner="ACCOUNT_CLERK",
                        execution_authority="ACCOUNT_CLERK",
                        operation_state="succeeded",
                        broker_order_id=(order.order_id if order is not None else None),
                        proof_reference=event_key,
                        source_event_at_ms=(order.updated_at_ms if order is not None else None),
                        clerk_observed_at_ms=self._repo.clock(),
                        summary_code=kind,
                        facts_json=facts.to_facts_json(),
                    )

                self._repo.observe_account_hold(
                    reason_code=UNEXPLAINED_TRADE_UPDATE_REASON_CODE,
                    evidence_refs_json=canonicalize(evidence_refs),
                    build_raise=lambda: _transition("ACCOUNT_HOLD_RAISED"),
                    build_refresh=lambda: _transition("ACCOUNT_HOLD_REFRESHED"),
                )
                return ClerkEntryKind.UNEXPLAINED_ORDER

            owner = self._repo.active_exit_for_order(local_order.order_ref)
            if owner is None:
                owner = self._repo.effect_operation(local_order.effect_operation_id)
            if owner is None:
                raise RuntimeError(f"SQLite order {local_order.order_ref!r} has no owning effect operation")

            if (
                recovery_source == "closed_orders_window"
                and event.event_type in {"fill", "partial_fill"}
                and event.execution_id is None
            ):
                # REST order snapshots carry only cumulative order economics.
                # Preserve them through the explicitly labelled recovery fold;
                # do not invent an execution ID just to enter the websocket path.
                fold_order_evidence(
                    self._repo,
                    effect_operation_id=owner.effect_operation_id,
                    order=order,
                )
                return ClerkEntryKind.ORDER_EVENT

            if event.event_type in {"fill", "partial_fill"} and event.execution_id is not None:
                if event.quantity is None or event.price is None:
                    raise ValueError(
                        "Alpaca execution event must include a quantity and price when execution_id is set"
                    )
                facts = ExecutionSliceFilledFacts(
                    execution_id=event.execution_id,
                    symbol=order.symbol,
                    side=order.side.upper(),
                    slice_qty=event.quantity,
                    slice_price=event.price,
                    fee=None,
                    fee_fidelity="not_reported",
                    evidence_source="websocket",
                    source_event_at_ms=event.occurred_at_ms,
                )

                def _execution_transition() -> TransitionInput:
                    return TransitionInput(
                        strategy_instance_id=owner.strategy_instance_id,
                        run_id=owner.run_id,
                        command_id=owner.command_id,
                        effect_operation_id=owner.effect_operation_id,
                        order_ref=local_order.order_ref,
                        broker_order_id=order.order_id,
                        transition_kind="EXECUTION_SLICE_FILLED",
                        custody_owner="ACCOUNT_CLERK",
                        execution_authority="ACCOUNT_CLERK",
                        operation_state="in_progress",
                        proof_reference=event_key,
                        source_event_at_ms=event.occurred_at_ms,
                        clerk_observed_at_ms=self._repo.clock(),
                        summary_code="EXECUTION_SLICE_FILLED",
                        facts_json=facts.to_facts_json(),
                    )

                def _coverage_conflict_transition() -> TransitionInput:
                    conflict_facts = UncertaintyRaisedFacts(
                        severity="error",
                        blocks_new_exposure=True,
                        allows_reduction=False,
                        reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
                        headline="Exact execution conflicts with prior immutable evidence",
                        explanation=(
                            "The received websocket execution cannot be safely merged with "
                            "the order's prior execution evidence."
                        ),
                        operator_impact=(
                            "New exposure is blocked until this order's execution coverage is reconciled."
                        ),
                        next_step=(
                            "Reconcile the broker order and its execution slices before resuming."
                        ),
                        evidence_refs=[event_key, event.execution_id],
                        cause_facts=ExecutionCoverageConflictCause(
                            order_ref=local_order.order_ref,
                            execution_id=event.execution_id,
                        ).to_mapping(),
                    )
                    return TransitionInput(
                        strategy_instance_id=owner.strategy_instance_id,
                        run_id=owner.run_id,
                        command_id=owner.command_id,
                        effect_operation_id=owner.effect_operation_id,
                        order_ref=local_order.order_ref,
                        broker_order_id=order.order_id,
                        transition_kind="UNCERTAINTY_RAISED",
                        custody_owner="ACCOUNT_CLERK",
                        execution_authority="ACCOUNT_CLERK",
                        operation_state="succeeded",
                        proof_reference=event_key,
                        source_event_at_ms=event.occurred_at_ms,
                        clerk_observed_at_ms=self._repo.clock(),
                        summary_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
                        facts_json=conflict_facts.to_facts_json(),
                    )

                self._repo.append_execution_slice_if_absent(
                    execution_id=event.execution_id,
                    order_ref=local_order.order_ref,
                    build_transition=_execution_transition,
                    build_coverage_conflict=_coverage_conflict_transition,
                )

            # A websocket's embedded order is aggregate lifecycle evidence,
            # never the source of fill math: it may report a cumulative filled
            # quantity while the frame above contains exactly one execution
            # slice. REST reconciliation remains the explicitly-labelled
            # cumulative-recovery path in ``fold_order_evidence``.
            fold_order_acknowledgement(
                self._repo,
                effect_operation_id=owner.effect_operation_id,
                order=order,
                append_stale_ack=False,
            )
            return ClerkEntryKind.ORDER_EVENT

    async def recover_activity_window(
        self,
        *,
        read: BrokerReadPort,
        limit: int,
    ) -> int:
        # SQLite's canonical recovery evidence is the bounded order/position
        # snapshot reconciled below. Account activities are a legacy JSONL
        # projection and must not become a second write path after activation.
        del read, limit
        return 0

    async def reconcile_gap(self) -> None:
        result = await self._reconciler.reconcile_account(trigger="AUTOMATIC")
        if result.verdict == "stale":
            raise RuntimeError("SQLite Alpaca Clerk could not reconcile the trade-update gap")


__all__ = [
    "UNEXPLAINED_TRADE_UPDATE_REASON_CODE",
    "ActivityRecoveryRecorder",
    "LegacyLifecycleRecorder",
    "LegacyTradeUpdateEvidenceSink",
    "SqliteReconciliationFacade",
    "SqliteTradeUpdateEvidenceSink",
    "TradeUpdateEvidenceSink",
]
