"""Consumer-facing protocol for the one process-selected Alpaca Clerk."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
    from app.broker.alpaca.clerk.models import (
        ClerkCustodySnapshot,
        EffectOperationReceipt,
        EffectPurpose,
        InstanceCustodyProof,
        ReconciliationVerdict,
    )
    from app.broker.alpaca.clerk.sqlite.commands import CommandSubmission
    from app.broker.alpaca.clerk.sqlite.models import OrderResource
    from app.schemas.action_plan import ActionPlan
    from app.services.bot_binding_repository import BrokerBotBinding
    from app.services.source_bar_ledger import RetainedSourceBar


class ClerkAdmissionSnapshotStaleError(RuntimeError):
    """A revision-bound Clerk rejected an admission snapshot at activation."""


@runtime_checkable
class RevisionBoundRunRegistrar(Protocol):
    """Clerk capability for atomic Start/Resume admission-token validation."""

    supports_revision_bound_admission: Literal[True]

    async def register_strategy_run(
        self,
        binding: BrokerBotBinding,
        *,
        admission_snapshot: ClerkCustodySnapshot,
    ) -> None: ...


class ActiveAlpacaClerk(Protocol):
    """Safety-critical surface exposed by the activated SQLite authority."""

    authority_kind: Literal["sqlite", "synthetic"]
    broker_id: str

    async def recover(self) -> None: ...

    async def unresolved_effect_count(self) -> int: ...

    async def register_strategy_run(
        self,
        binding: BrokerBotBinding,
        *,
        admission_snapshot: ClerkCustodySnapshot | None = None,
    ) -> None: ...

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
        use_rth: bool = True,
        capability_account_id: str | None = None,
        retained_source_bar: RetainedSourceBar | None = None,
        decision_evidence: EffectDecisionEvidence | None = None,
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
    ) -> tuple[OrderResource, ...]: ...


__all__ = [
    "ActiveAlpacaClerk",
    "ClerkAdmissionSnapshotStaleError",
    "RevisionBoundRunRegistrar",
]
