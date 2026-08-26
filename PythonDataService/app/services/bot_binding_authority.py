"""One authority-selection module for Alpaca bot bindings.

The runner asks this module which custody authority owns a binding.  Real
Paper and Dry Run differ only behind this seam: callers receive the same
admission guard, lifecycle projector, source-evidence store, recovery view,
and runtime-release lifecycle without branching on ``binding.mode``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    paper_evidence_account_id_for_strategy,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_synthetic_clerk_authority,
    get_clerk_runtime,
    register_clerk_runtime,
    select_synthetic_clerk_runtime,
    unregister_clerk_runtime,
)
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.engine.live.bot_lifecycle_state import BotLifecycleStateRepo
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleProjector,
    SqliteAlpacaLifecycleAuthority,
)
from app.services.bot_start_admission import (
    StartAdmissionUnavailable,
    default_start_custody_guard,
    default_start_custody_projection,
)
from app.services.source_bar_ledger import SourceBarLedger


class BindingAuthority:
    """The complete custody/evidence authority for one immutable binding."""

    account_id: str

    def start_custody_guard(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        raise NotImplementedError

    def start_custody_projection(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        """Custody for a read: projects the sweep's verdict, never reconciles."""
        raise NotImplementedError

    def lifecycle_projector(self) -> AlpacaLifecycleProjector:
        raise NotImplementedError

    def source_bars(self) -> SourceBarLedger | None:
        return None

    async def ensure_recoverable(self) -> None:
        return

    @asynccontextmanager
    async def runtime_for_projection(self) -> AsyncIterator[ActiveClerkRuntime | None]:
        yield None

    async def release_if_unused(self) -> None:
        return

    def lifecycle_recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        return ()


@dataclass(frozen=True)
class RealPaperBindingAuthority(BindingAuthority):
    """The active real-paper Clerk remains the sole real custody authority."""

    binding: BrokerBotBinding
    projector: AlpacaLifecycleProjector
    external_start_guard: Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]] | None
    artifacts_root: Path
    account_id: str = "real_paper"

    def start_custody_guard(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        if self.external_start_guard is not None:
            return self.external_start_guard(self.binding.strategy_instance_id)
        return default_start_custody_guard(self.binding)

    def start_custody_projection(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        # An injected guard is a whole-authority substitution (tests, sim
        # harnesses); it stands in for reads too.
        if self.external_start_guard is not None:
            return self.external_start_guard(self.binding.strategy_instance_id)
        return default_start_custody_projection(self.binding)

    def lifecycle_projector(self) -> AlpacaLifecycleProjector:
        return self.projector

    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(
            artifacts_root=self.artifacts_root,
            account_id=paper_evidence_account_id_for_strategy(
                self.binding.strategy_instance_id
            ),
        )


@dataclass
class SyntheticBindingAuthority(BindingAuthority):
    """A deterministic ``sim:<strategy-instance>`` sealed custody authority."""

    binding: BrokerBotBinding
    artifacts_root: Path
    lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo]
    runtime_in_use: Callable[[BrokerBotBinding], bool]
    brokers: dict[str, SyntheticBroker]
    account_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.account_id = synthetic_account_id_for_strategy(self.binding.strategy_instance_id)

    def start_custody_guard(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        return self._start_custody_guard()

    def start_custody_projection(self) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
        return self._start_custody_guard(project=True)

    def lifecycle_projector(self) -> AlpacaLifecycleProjector:
        runtime = get_clerk_runtime(self.account_id)
        repository = None if runtime is None else runtime.sqlite_repository
        if repository is None:
            raise StartAdmissionUnavailable(
                "Dry Run lifecycle authority is unavailable.",
                detail="Activate the isolated synthetic Clerk before deploying this bot.",
            )
        return AlpacaLifecycleProjector(
            authority=SqliteAlpacaLifecycleAuthority(repository),
            lifecycle_repo_for=self.lifecycle_repo_for,
            require_alpaca_identity=lambda _sid, _sqlite_claim: None,
        )

    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(artifacts_root=self.artifacts_root, account_id=self.account_id)

    async def ensure_recoverable(self) -> None:
        runtime = await self._runtime()
        if runtime.clerk is None:
            detail = (
                runtime.startup_failure.recovery
                if runtime.startup_failure is not None
                else "Synthetic runtime was not composed."
            )
            raise StartAdmissionUnavailable(
                "Dry Run synthetic authority could not be restored.",
                detail=detail,
            )

    @asynccontextmanager
    async def runtime_for_projection(self) -> AsyncIterator[ActiveClerkRuntime]:
        was_active = get_clerk_runtime(self.account_id) is not None
        runtime = await self._runtime()
        try:
            yield runtime
        finally:
            if not was_active:
                await self.release_if_unused()

    async def release_if_unused(self) -> None:
        if self.runtime_in_use(self.binding):
            return
        runtime = unregister_clerk_runtime(self.account_id)
        if runtime is not None:
            await runtime.close()
        self.brokers.pop(self.account_id, None)

    def lifecycle_recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        runtime = get_clerk_runtime(self.account_id)
        repository = None if runtime is None else runtime.sqlite_repository
        if repository is None:
            raise StartAdmissionUnavailable(
                "Dry Run lifecycle authority is unavailable for boot recovery.",
                detail=f"Restore the isolated synthetic authority {self.account_id!r} before boot repair.",
            )
        return tuple(
            (candidate.strategy_instance_id, candidate.run_id)
            for candidate in repository.lifecycle_recovery_candidates()
            if candidate.strategy_instance_id == self.binding.strategy_instance_id
        )

    @asynccontextmanager
    async def _start_custody_guard(self, *, project: bool = False) -> AsyncIterator[ClerkCustodySnapshot]:
        runtime = await self._runtime()
        clerk = runtime.clerk
        if clerk is None:
            raise StartAdmissionUnavailable(
                "Dry Run synthetic Clerk activation failed.",
                detail=(runtime.startup_failure.recovery if runtime.startup_failure is not None else "Retry activation."),
            )
        admission = (
            clerk.start_admission_projection if project else clerk.start_admission_snapshot
        )
        async with admission(self.binding.strategy_instance_id) as snapshot:
            yield snapshot

    async def _runtime(self) -> ActiveClerkRuntime:
        existing = get_clerk_runtime(self.account_id)
        if existing is not None:
            return existing
        broker = SyntheticBroker(account_id=self.account_id, source_bars=self.source_bars())
        await activate_synthetic_clerk_authority(
            account_id=self.account_id,
            artifacts_root=self.artifacts_root,
        )
        runtime = await select_synthetic_clerk_runtime(
            account_id=self.account_id,
            read=broker,
            trade=broker,
            artifacts_root=self.artifacts_root,
        )
        if runtime.clerk is not None:
            register_clerk_runtime(runtime)
            self.brokers[self.account_id] = broker
        return runtime


@dataclass
class BindingAuthoritySelector:
    """Build the one typed authority object for each immutable bot binding."""

    artifacts_root: Path
    lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo]
    real_projector: AlpacaLifecycleProjector
    external_start_guard: Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]] | None
    runtime_in_use: Callable[[BrokerBotBinding], bool]
    synthetic_brokers: dict[str, SyntheticBroker] = field(default_factory=dict)

    def for_binding(self, binding: BrokerBotBinding) -> BindingAuthority:
        if binding.mode == "dry_run":
            return SyntheticBindingAuthority(
                binding=binding,
                artifacts_root=self.artifacts_root,
                lifecycle_repo_for=self.lifecycle_repo_for,
                runtime_in_use=self.runtime_in_use,
                brokers=self.synthetic_brokers,
            )
        return RealPaperBindingAuthority(
            binding=binding,
            projector=self.real_projector,
            external_start_guard=self.external_start_guard,
            artifacts_root=self.artifacts_root,
        )


__all__ = [
    "BindingAuthority",
    "BindingAuthoritySelector",
    "RealPaperBindingAuthority",
    "SyntheticBindingAuthority",
]
