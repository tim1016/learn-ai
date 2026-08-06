"""Active-authority boundary for Alpaca ``trade_updates`` evidence.

The websocket owns transport, capture, parsing, and in-connection duplicate
suppression.  It does not know which durable Clerk is active.  These sinks are
the only authority-specific part of that flow, which prevents a frame from
being written to both the legacy JSONL ledger and the SQLite authority.
"""

from __future__ import annotations

from typing import Protocol

from app.broker.alpaca.clerk.models import ClerkEntryKind
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderEvent
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort

UNEXPLAINED_TRADE_UPDATE_REASON_CODE = "UNEXPLAINED_ORDER"


class TradeUpdateEvidenceSink(Protocol):
    """The durable destination selected for one account at process boot."""

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


class AsyncIntakeFence(Protocol):
    async def __aenter__(self) -> object: ...

    async def __aexit__(self, *exc: object) -> None: ...


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
        read: BrokerReadPort,
        trade: BrokerTradePort,
        intake: AsyncIntakeFence,
    ) -> None:
        self._repo = repo
        self._read = read
        self._trade = trade
        self._intake = intake

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
        del event, recovery_source, recovery_window_limit
        async with self._intake:
            local_order = self._repo.order(client_order_id) if client_order_id else None
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
            fold_order_evidence(
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
        async with self._intake:
            result = await reconcile_account(
                self._repo,
                read=self._read,
                trade=self._trade,
                trigger="AUTOMATIC",
            )
            if result.verdict == "stale":
                raise RuntimeError("SQLite Alpaca Clerk could not reconcile the trade-update gap")


__all__ = [
    "UNEXPLAINED_TRADE_UPDATE_REASON_CODE",
    "ActivityRecoveryRecorder",
    "AsyncIntakeFence",
    "LegacyLifecycleRecorder",
    "LegacyTradeUpdateEvidenceSink",
    "SqliteTradeUpdateEvidenceSink",
    "TradeUpdateEvidenceSink",
]
