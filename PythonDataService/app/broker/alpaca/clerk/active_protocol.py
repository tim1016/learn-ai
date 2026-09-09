"""Consumer-facing protocol for the one process-selected Alpaca Clerk."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
    from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
    from app.broker.alpaca.clerk.models import (
        ClerkCustodySnapshot,
        EffectOperationReceipt,
        EffectPurpose,
        InstanceCustodyProof,
        ReconciliationVerdict,
    )
    from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
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

    authority_kind: Literal["sqlite", "synthetic", "shadow"]
    broker_id: str

    @property
    def account_id(self) -> str:
        """The exact account identity this authority is composed against.

        Declared here rather than read by name off an ``ActiveAlpacaClerk``:
        every authority is account-scoped by construction, so a caller
        comparing a binding's sealed account against the installed one is
        asking a question the protocol answers, not probing for a capability.
        """
        ...

    @property
    def live_arming(self) -> ArmingGate | None:
        """The per-instance arming gate, or ``None`` where arming does not apply.

        Composed only on a live-mode authority (ADR 0059 D3/D11); the paper,
        shadow and synthetic authorities answer ``None``. Declared so a reader
        of the gate's fault -- the live verdict's mid-session mode
        disagreement -- reads a stated ``None`` rather than an absent
        attribute, which would look the same and mean something else.
        """
        ...

    @property
    def program_leg_policy(self) -> ProgramLegPolicy:
        """How this authority may shape an extended-session leg (ADR 0059 D5.3).

        ``program_leg_policy.window`` is the declared extended session
        (``None`` for a regular-only authority); it has no second accessor of
        its own, so a caller can never read a window the policy that prices
        against it does not carry.
        """
        ...

    async def recover(self) -> None: ...

    async def unresolved_effect_count(self, *, subject_id: str | None = None) -> int: ...

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

    def start_admission_projection(
        self,
        strategy_instance_id: str,
    ) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        """Read-only twin of :meth:`start_admission_snapshot` (#1776 WP2).

        Projects the sweep's last verdict instead of reconciling, so a read
        contacts no broker and appends nothing to the ledger.
        """
        ...

    async def cancel_working_entries_for_instance(
        self,
        strategy_instance_id: str,
    ) -> tuple[OrderResource, ...]: ...


__all__ = [
    "ActiveAlpacaClerk",
    "ClerkAdmissionSnapshotStaleError",
    "RevisionBoundRunRegistrar",
]
