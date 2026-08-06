"""Consumer-facing protocol for the one process-selected Alpaca Clerk."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.models import (
        ClerkCustodySnapshot,
        EffectOperationReceipt,
        EffectPurpose,
        InstanceCustodyProof,
        OrderCancelResult,
        ReconciliationVerdict,
    )
    from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission
    from app.broker.alpaca.clerk.sqlite.models import OrderResource
    from app.schemas.action_plan import ActionPlan
    from app.services.bot_binding_repository import BrokerBotBinding


class ActiveAlpacaClerk(Protocol):
    """Safety-critical surface shared by legacy and SQLite authorities."""

    authority_kind: str
    broker_id: str

    async def recover(self) -> None: ...

    async def unresolved_effect_count(self) -> int: ...

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None: ...

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> CommandSubmission | None: ...

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose: EffectPurpose,
        action_plan: ActionPlan,
        quantity: int,
    ) -> EffectOperationReceipt: ...

    async def reconcile_once(self) -> ReconciliationVerdict: ...

    async def prove_instance_custody(
        self,
        strategy_instance_id: str,
    ) -> InstanceCustodyProof: ...

    async def custody_snapshot(
        self,
        strategy_instance_id: str,
    ) -> ClerkCustodySnapshot: ...

    def start_admission_snapshot(
        self,
        strategy_instance_id: str,
    ) -> AbstractAsyncContextManager[ClerkCustodySnapshot]: ...

    async def cancel_working_entries_for_instance(
        self,
        strategy_instance_id: str,
    ) -> tuple[OrderCancelResult | OrderResource, ...]: ...


__all__ = ["ActiveAlpacaClerk"]
