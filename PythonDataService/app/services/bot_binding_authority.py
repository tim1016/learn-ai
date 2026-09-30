"""One authority-selection module for Alpaca bot bindings.

The runner asks this module which custody authority owns a binding.  Dry Run
and the process's primary account authority -- real paper, or the shadow of a
live account -- differ only behind this seam: callers receive the same
admission guard, lifecycle projector, source-evidence store, recovery view,
and runtime-release lifecycle without branching on ``binding.mode``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityKind,
    authority_kind_for_account,
    evidence_account_id_for,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    SYNTHETIC_CLERK_LEASE_HELD,
    ActiveClerkRuntime,
    SyntheticOpening,
    activate_synthetic_clerk_authority,
    get_alpaca_clerk,
    get_clerk_runtime,
    primary_custody_world,
    register_clerk_runtime,
    select_synthetic_clerk_runtime,
    unregister_clerk_runtime,
)
from app.broker.alpaca.clerk.active_runtime import DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S
from app.broker.alpaca.clerk.models import ReconciliationCut
from app.broker.alpaca.clerk.shadow_authority import open_graduated_shadow_store
from app.broker.alpaca.clerk.sqlite.budget_authority import authority_review_token, commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseHeld
from app.broker.alpaca.clerk.sqlite.run_ownership import RUNNER_GONE_REASON
from app.broker.alpaca.clerk.synthetic_activation import SyntheticActivationStore
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.engine.live.bot_lifecycle_state import BotLifecycleStateRepo
from app.schemas.account_authority import CustodyWorld
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.alpaca_bot_identity import AlpacaBotIdentityGuard
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleProjector,
    SqliteAlpacaLifecycleAuthority,
)
from app.services.bot_start_admission import (
    AdmissionCustodyCut,
    DryRunAccountHeldElsewhere,
    StartAdmissionUnavailable,
    SyntheticAccountRestoring,
    default_reconciliation_covers,
    default_start_custody_guard,
    default_start_custody_projection,
)
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

#: How long boot's Dry Run restoration waits, in all, for the execution
#: leases a dead predecessor left on its Dry Runs' accounts before calling one
#: that bot's own failure (#2582). That process renewed them all and died
#: without releasing any, so they lapse together within one lease lifetime:
#: one deadline covers every Dry Run boot restores.
BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S = DEFAULT_EXECUTION_LEASE_WAIT_TIMEOUT_S
#: How often a Dry Run's opening looks again at a held lease: a lease lapsing
#: a second from now costs a second, never a whole lease lifetime.
BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S = 1.0


class BindingAuthority:
    """The complete custody/evidence authority for one immutable binding."""

    account_id: str

    def start_custody_guard(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        raise NotImplementedError

    def start_custody_projection(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        """Custody for a read: projects the sweep's verdict, never reconciles."""
        raise NotImplementedError

    def reconciliation_covers(self, cut: ReconciliationCut) -> bool:
        """Whether ``cut`` -- one pass of the account Clerk -- still proves this binding's custody.

        Only the account's own authority can answer yes; any other authority
        keeps its own ledger and reconciles for itself.
        """
        return False

    def lifecycle_projector(self) -> AlpacaLifecycleProjector:
        raise NotImplementedError

    @asynccontextmanager
    async def lifecycle_for_settle(self) -> AsyncIterator[AlpacaLifecycleProjector]:
        """The projector a bot whose runner is gone is settled through (#2589)."""
        yield self.lifecycle_projector()

    def source_bars(self) -> SourceBarLedger | None:
        return None

    async def ensure_recoverable(self, *, lease_wait_s: float = 0.0) -> None:
        return

    @asynccontextmanager
    async def runtime_for_projection(self) -> AsyncIterator[ActiveClerkRuntime | None]:
        yield None

    async def release_if_unused(self) -> None:
        return

    async def release_after_run_end(self) -> None:
        """Release once a run has ended; an authority with run-end work does it first."""
        await self.release_if_unused()

    async def reconcile_for_end(self) -> None:
        """Run this authority's reconciliation pass now: a bot's owner-set end has come (#2607).

        The pass is what carries the end out (``clerk.sqlite.scheduled_end``).
        """
        raise NotImplementedError

    def lifecycle_recovery_candidates(self) -> tuple[tuple[str, str], ...]:
        return ()


def primary_custody_kind() -> CustodyWorld:
    """The world the primary authority custodies in; refuses to guess when none is installed.

    ``primary_custody_world`` is the single reader of that selection. It
    answers ``None`` for every world a binding's evidence cannot be filed in
    — no authority installed, and the isolated synthetic world a primary boot
    never selects — and this caller must refuse rather than guess.
    """
    world = primary_custody_world()
    if world is None:
        raise StartAdmissionUnavailable(
            "The account Clerk is not installed.",
            detail=(
                "Restore the account Clerk before starting a bot; its world "
                "decides where evidence is retained."
            ),
        )
    return world


@dataclass(frozen=True)
class PrimaryAccountBindingAuthority(BindingAuthority):
    """The process's primary account authority -- real paper, or the shadow of a live account -- remains the sole real custody authority."""

    binding: BrokerBotBinding
    projector: AlpacaLifecycleProjector
    external_start_guard: Callable[[str], AbstractAsyncContextManager[AdmissionCustodyCut]] | None
    artifacts_root: Path
    custody_kind: Callable[[], AccountAuthorityKind]
    account_id: str = "real_paper"

    def start_custody_guard(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        if self.external_start_guard is not None:
            return self.external_start_guard(self.binding.strategy_instance_id)
        return default_start_custody_guard(self.binding)

    def start_custody_projection(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        # An injected guard is a whole-authority substitution (tests, sim
        # harnesses); it stands in for reads too.
        if self.external_start_guard is not None:
            return self.external_start_guard(self.binding.strategy_instance_id)
        return default_start_custody_projection(self.binding)

    def reconciliation_covers(self, cut: ReconciliationCut) -> bool:
        # An injected guard answers custody itself, so no pass of the account
        # Clerk speaks for it.
        if self.external_start_guard is not None:
            return False
        return default_reconciliation_covers(self.binding, cut)

    def lifecycle_projector(self) -> AlpacaLifecycleProjector:
        return self.projector

    async def reconcile_for_end(self) -> None:
        # The account's periodic sweep would reach the end within its
        # interval; this pass reaches it on time.
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise StartAdmissionUnavailable(
                "The account Clerk is not installed.",
                detail="A bot's end is carried out by its account's Clerk; the end waits until it is back.",
            )
        await clerk.reconcile_once()

    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(
            artifacts_root=self.artifacts_root,
            account_id=evidence_account_id_for(
                mode=self.binding.mode,
                strategy_instance_id=self.binding.strategy_instance_id,
                custody_kind=self.custody_kind(),
            ),
        )


@dataclass
class SyntheticRuntimeAccess:
    """Serialize one private runtime's open/use/close lifecycle across callers.

    This lock permits awaited setup and recovery. It is distinct from the
    Clerk's short intake fence, which lifecycle work may acquire inside it.
    Task reentry lets an admission promote a runtime already opened for its
    projection; child tasks must wait like every other caller.

    ``restorer`` is the task restoring this account, from the moment boot
    queues it until its restoration settles (#2684). It is the one
    fail-fast check: every other task asking for the account is answered at
    once with :class:`SyntheticAccountRestoring` instead of queueing behind
    the restoration's lease wait; the restorer passes by its identity.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = field(default=None, init=False)
    restorer: asyncio.Task | None = field(default=None, init=False)

    def is_restoring(self) -> bool:
        return self.restorer is not None

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        current = asyncio.current_task()
        if current is not None and self.owner is current:
            yield
            return
        if self.restorer is not None and self.restorer is not current:
            raise SyntheticAccountRestoring()
        async with self.lock:
            self.owner = current
            try:
                yield
            finally:
                self.owner = None


@dataclass
class _SyntheticAccount:
    """One ``sim:<strategy-instance>`` account's open/use/close lifecycle.

    The bound authority and an unbound orphan's share it: both open the same
    store under the same per-strategy lock, and differ only in which
    openings (``SyntheticOpening``) they may ask for.
    """

    strategy_instance_id: str
    budget_consent: DeployBudgetConsent | None
    artifacts_root: Path
    runtime_in_use: Callable[[str], bool]
    brokers: dict[str, SyntheticBroker]
    clock: Clock
    runtime_access: SyntheticRuntimeAccess
    account_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.account_id = synthetic_account_id_for_strategy(self.strategy_instance_id)

    def source_bars(self) -> SourceBarLedger:
        return SourceBarLedger(artifacts_root=self.artifacts_root, account_id=self.account_id)

    async def ensure_operating(self, *, lease_wait_s: float) -> None:
        """Compose this account for operation -- its full recovery included -- or raise why it cannot be."""
        try:
            runtime = await self.open(SyntheticOpening.OPERATE, lease_wait_s=lease_wait_s)
        except ExecutionLeaseHeld as exc:
            # The activation's own open raises the store's error raw.
            raise DryRunAccountHeldElsewhere() from exc
        if runtime.clerk is None:
            failure = runtime.startup_failure
            if failure is not None and failure.reason_code == SYNTHETIC_CLERK_LEASE_HELD:
                # The selection's form of the same fact (#2670): one typed
                # error for both, so no caller matches either raw form.
                raise DryRunAccountHeldElsewhere()
            detail = (
                failure.recovery
                if failure is not None
                else "Synthetic runtime was not composed."
            )
            raise StartAdmissionUnavailable(
                "Dry Run synthetic authority could not be restored.",
                detail=detail,
            )

    @asynccontextmanager
    async def held_for_request(self, opening: SyntheticOpening) -> AsyncIterator[ActiveClerkRuntime]:
        """This account for one request; a runtime composed only for it is released after it."""
        async with self.runtime_access.hold():
            was_active = get_clerk_runtime(self.account_id) is not None
            runtime = await self.open(opening)
            try:
                yield runtime
            finally:
                if not was_active:
                    await self.release_if_unused()

    async def release_if_unused(self) -> None:
        async with self.runtime_access.hold():
            if self.runtime_in_use(self.strategy_instance_id):
                return
            runtime = get_clerk_runtime(self.account_id)
            if runtime is not None:
                unregister_clerk_runtime(self.account_id)
                await runtime.close()
            self.brokers.pop(self.account_id, None)

    async def open(self, opening: SyntheticOpening, *, lease_wait_s: float = 0.0) -> ActiveClerkRuntime:
        async with self.runtime_access.hold():
            return await self._open_locked(opening, lease_wait_s=lease_wait_s)

    async def _open_locked(self, opening: SyntheticOpening, *, lease_wait_s: float) -> ActiveClerkRuntime:
        operating = opening is SyntheticOpening.OPERATE
        existing = get_clerk_runtime(self.account_id)
        if existing is not None:
            if operating:
                await self._prepare_budget_runtime(existing)
                if existing.envelope_sync is not None:
                    if self.budget_consent is None:
                        await existing.envelope_sync.tick()
                    existing.envelope_sync.start()
            return existing
        broker = SyntheticBroker(account_id=self.account_id, source_bars=self.source_bars(), clock=self.clock)
        if operating:
            await activate_synthetic_clerk_authority(
                account_id=self.account_id,
                artifacts_root=self.artifacts_root,
                clock=self.clock,
                execution_lease_wait_timeout_s=lease_wait_s,
                execution_lease_retry_interval_s=BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S,
            )
        runtime = await select_synthetic_clerk_runtime(
            account_id=self.account_id,
            read=broker,
            trade=broker,
            artifacts_root=self.artifacts_root,
            repository_opener=lambda account_id, root: ClerkSqliteRepository.open(
                account_id=account_id, artifacts_root=root, clock=self.clock,
            ),
            execution_lease_wait_timeout_s=lease_wait_s,
            execution_lease_retry_interval_s=BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S,
            simulation_initial_cash=(None if not operating or self.budget_consent is None else Decimal(self.budget_consent.committed_cents) / 100),
            opening=opening,
        )
        if runtime.clerk is not None:
            register_clerk_runtime(runtime)
            self.brokers[self.account_id] = broker
            if operating:
                await self._prepare_budget_runtime(runtime)
        return runtime

    async def _prepare_budget_runtime(self, runtime: ActiveClerkRuntime) -> None:
        consent = self.budget_consent
        repo = runtime.sqlite_repository
        if consent is None or repo is None:
            return
        if repo.budget_authority_version() < 2:
            with repo.write_fence() as conn:
                if conn.execute("SELECT 1 FROM runs LIMIT 1").fetchone():
                    raise StartAdmissionUnavailable("This earlier Dry Run cannot be restarted.", detail="Review a fresh deployment identity and simulated starting cash.")
                commit_budget_authority_cutover(repo, actor=consent.actor,
                    reviewed_token=authority_review_token(repo), stop_receipt="fresh-private-authority-with-no-runs")
        if runtime.envelope_sync is not None:
            await runtime.envelope_sync.refresh_private_starting_cash(Decimal(consent.committed_cents) / 100)


@dataclass
class SyntheticBindingAuthority(BindingAuthority):
    """A deterministic ``sim:<strategy-instance>`` sealed custody authority."""

    binding: BrokerBotBinding
    artifacts_root: Path
    lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo]
    runtime_in_use: Callable[[str], bool]
    brokers: dict[str, SyntheticBroker]
    account_id: str = field(init=False)
    clock: Clock = now_ms_utc
    runtime_access: SyntheticRuntimeAccess = field(default_factory=SyntheticRuntimeAccess)

    def __post_init__(self) -> None:
        self.account_id = synthetic_account_id_for_strategy(self.binding.strategy_instance_id)

    @property
    def _account(self) -> _SyntheticAccount:
        # Built per use, so it reads the binding's consent as it stands now.
        return _SyntheticAccount(
            strategy_instance_id=self.binding.strategy_instance_id,
            budget_consent=self.binding.budget_consent,
            artifacts_root=self.artifacts_root,
            runtime_in_use=self.runtime_in_use,
            brokers=self.brokers,
            clock=self.clock,
            runtime_access=self.runtime_access,
        )

    def start_custody_guard(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
        return self._start_custody_guard()

    def start_custody_projection(self) -> AbstractAsyncContextManager[AdmissionCustodyCut]:
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
        return self._account.source_bars()

    async def ensure_recoverable(self, *, lease_wait_s: float = 0.0) -> None:
        """Compose this Dry Run's authority, or raise why it cannot be.

        ``lease_wait_s`` is how long the opening may wait out an execution
        lease another process holds: boot's restoration passes what remains
        of its one deadline, since a restart meets its dead predecessor's
        lease on every Dry Run account (#2582). Zero is a single attempt.
        """
        await self._account.ensure_operating(lease_wait_s=lease_wait_s)

    @asynccontextmanager
    async def runtime_for_projection(self) -> AsyncIterator[ActiveClerkRuntime]:
        # A bound bot's read keeps the store's recovery pass, whose published
        # verdict its money views project (#1776).
        async with self._account.held_for_request(SyntheticOpening.PROJECT) as runtime:
            yield runtime

    async def release_if_unused(self) -> None:
        await self._account.release_if_unused()

    async def release_after_run_end(self) -> None:
        """Close what the ended run left, then release (owner decision 2026-09-29).

        The account's reconciliation pass closes a Dry Run's leftover position
        (``dry_run_close``); running it here, while the authority is still
        open, stamps that simulated sale when the run ended instead of
        whenever the bot is next opened. The authority is released whatever
        the pass does, and any failure of the pass is logged with its stack,
        not raised: the run has already ended, so nothing here may fail the
        Stop that committed it or the shutdown loop releasing every other
        bot, and the close -- derived from durable facts -- is retried by the
        next opening.
        """
        async with self.runtime_access.hold():
            runtime = get_clerk_runtime(self.account_id)
            try:
                if (
                    runtime is not None and runtime.clerk is not None
                    and not self.runtime_in_use(self.binding.strategy_instance_id)
                ):
                    await runtime.clerk.reconcile_once()
            except Exception:
                logger.exception(
                    "a Dry Run's run-end reconciliation failed; its next opening closes what the run left",
                    extra={"action": "dry_run_run_end_reconcile_failed", "account_id": self.account_id,
                           "strategy_instance_id": self.binding.strategy_instance_id},
                )
            finally:
                await self.release_if_unused()

    async def reconcile_for_end(self) -> None:
        # A Dry Run's own Clerk has no periodic sweep: without this pass its
        # end would wait for the next time its account is opened.
        async with self.runtime_access.hold():
            runtime = get_clerk_runtime(self.account_id)
            if runtime is None or runtime.clerk is None:
                raise StartAdmissionUnavailable(
                    "This Dry Run's simulated account is not open.",
                    detail="Its end is carried out the next time its account is opened.",
                )
            await runtime.clerk.reconcile_once()

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
    async def _start_custody_guard(self, *, project: bool = False) -> AsyncIterator[AdmissionCustodyCut]:
        async with self.runtime_access.hold():
            runtime = await self._account.open(SyntheticOpening.OPERATE)
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
                yield snapshot, clerk.program_leg_policy, clerk.exit_terms_for_instance(self.binding.strategy_instance_id)


@dataclass(frozen=True)
class UnboundDryRunAuthority:
    """A Dry Run found by its private authority's own activation, not a binding.

    Deploy commits the ``sim:<strategy-instance>`` budget and run before the
    runner records the binding, so a crash between the two leaves this
    authority as the only index (#2559). With no binding and no consent it
    can never admit or launch, and it offers exactly two openings:

    - ``runtime_for_projection`` -- a read. The store opens without its
      mutating startup recovery: nothing retired, nothing reconciled, no
      custody transition appended.
    - ``ensure_recoverable`` -- boot's restoration. It re-proves the
      activation and runs the full recovery that retires the orphaned run and
      fails its command, so a restart, never whichever read comes first,
      releases the orphan.
    """

    _account: _SyntheticAccount

    @property
    def strategy_instance_id(self) -> str:
        return self._account.strategy_instance_id

    @property
    def account_id(self) -> str:
        return self._account.account_id

    def runtime_for_projection(self) -> AbstractAsyncContextManager[ActiveClerkRuntime]:
        return self._account.held_for_request(SyntheticOpening.READ_ONLY)

    async def ensure_recoverable(self, *, lease_wait_s: float = 0.0) -> None:
        await self._account.ensure_operating(lease_wait_s=lease_wait_s)


@dataclass(frozen=True)
class SealedShadowBindingAuthority:
    """A binding sealed on a graduated live account's ``shadow:`` store (#2589).

    The bots that rehearsed on the shadow authority stay sealed on it after
    their live account graduates, while the installed authority custodies the
    live account itself: no sweep reads their store again, and Start refuses
    them ``SEALED_ACCOUNT_MISMATCH``. This authority offers the one thing such
    a bot still needs -- settling a dead run's duty record through its own
    store, never through the installed authority (ADR 0050's posture). It
    admits, trades and proves custody for nothing.
    """

    binding: BrokerBotBinding
    account_id: str
    artifacts_root: Path
    lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo]

    @asynccontextmanager
    async def lifecycle_for_settle(self) -> AsyncIterator[AlpacaLifecycleProjector]:
        """The sealed store's projector, once this bot's dead run there is closed.

        No runner in this process holds a run on a store the installed
        authority does not custody, and the execution lease this opening
        takes proves no other process does: a run the store still holds
        ACTIVE is a dead process's, closed as the #2369 retirement closes one
        on the installed account. The projector keeps the identity guard.
        """
        sid = self.binding.strategy_instance_id
        async with open_graduated_shadow_store(
            account_id=self.account_id, artifacts_root=self.artifacts_root
        ) as repository:
            run = repository.active_run(sid)
            if run is not None:
                submit_stop_run(
                    repository,
                    account_id=self.account_id,
                    strategy_instance_id=sid,
                    lifecycle_run_id=run.lifecycle_run_id,
                    operator_reason=RUNNER_GONE_REASON,
                    clock=repository.clock,
                )
            identity = AlpacaBotIdentityGuard(self.artifacts_root)
            yield AlpacaLifecycleProjector(
                authority=SqliteAlpacaLifecycleAuthority(repository),
                lifecycle_repo_for=self.lifecycle_repo_for,
                require_alpaca_identity=lambda instance, claim: identity.require(instance, sqlite_claim=claim),
            )


@dataclass
class BindingAuthoritySelector:
    """Build the one typed authority object for each immutable bot binding."""

    artifacts_root: Path
    lifecycle_repo_for: Callable[[str], BotLifecycleStateRepo]
    real_projector: AlpacaLifecycleProjector
    external_start_guard: Callable[[str], AbstractAsyncContextManager[AdmissionCustodyCut]] | None
    runtime_in_use: Callable[[str], bool]
    synthetic_brokers: dict[str, SyntheticBroker] = field(default_factory=dict)
    synthetic_runtime_access: dict[str, SyntheticRuntimeAccess] = field(default_factory=dict)
    clock: Clock = now_ms_utc

    def for_binding(self, binding: BrokerBotBinding) -> BindingAuthority:
        if binding.mode == "dry_run":
            return SyntheticBindingAuthority(
                binding=binding,
                artifacts_root=self.artifacts_root,
                lifecycle_repo_for=self.lifecycle_repo_for,
                runtime_in_use=self.runtime_in_use,
                brokers=self.synthetic_brokers,
                clock=self.clock,
                runtime_access=self._runtime_access(binding.strategy_instance_id),
            )
        return PrimaryAccountBindingAuthority(
            binding=binding,
            projector=self.real_projector,
            external_start_guard=self.external_start_guard,
            artifacts_root=self.artifacts_root,
            custody_kind=primary_custody_kind,
        )

    def for_settle(
        self, binding: BrokerBotBinding, *, foreign_account_id: str | None
    ) -> BindingAuthority | SealedShadowBindingAuthority | None:
        """The authority a bot whose runner is gone is settled through (#2589); ``None`` if this lane has none.

        ``foreign_account_id`` is the account the binding is sealed on when
        the installed authority does not custody it -- the one classification,
        ``BotBootRecovery.foreign_binding``. The account's kind decides the
        rest: a ``shadow:`` store settles through itself, and any other
        account this lane does not hold has no authority here.
        """
        if foreign_account_id is None:
            return self.for_binding(binding)
        if authority_kind_for_account(foreign_account_id) != "shadow":
            return None
        return SealedShadowBindingAuthority(
            binding=binding,
            account_id=foreign_account_id,
            artifacts_root=self.artifacts_root,
            lifecycle_repo_for=self.lifecycle_repo_for,
        )

    def for_unbound_dry_run(self, strategy_instance_id: str) -> UnboundDryRunAuthority | None:
        """The private authority a Deploy activated before recording its binding, if any."""
        activation = SyntheticActivationStore(self.artifacts_root).latest(
            synthetic_account_id_for_strategy(strategy_instance_id)
        )
        return None if activation is None else self.unbound_dry_run(strategy_instance_id)

    def unbound_dry_run(self, strategy_instance_id: str) -> UnboundDryRunAuthority:
        """The authority of a Dry Run whose activation the caller already found.

        Boot's enumeration lists the activations once and builds each orphan's
        authority from that listing: a second read of the ledger here is a
        second way for a bad ledger to fail boot (#2559).
        """
        return UnboundDryRunAuthority(_SyntheticAccount(
            strategy_instance_id=strategy_instance_id,
            budget_consent=None,
            artifacts_root=self.artifacts_root,
            runtime_in_use=self.runtime_in_use,
            brokers=self.synthetic_brokers,
            clock=self.clock,
            runtime_access=self._runtime_access(strategy_instance_id),
        ))

    def queue_for_restoration(self, strategy_instance_ids: Iterable[str], restorer: asyncio.Task[Any]) -> None:
        """Mark each ``sim:`` account restoring by ``restorer`` from now, not from its turn (#2684).

        Boot restores its Dry Runs one at a time, so a bot still queued is as
        unreadable to everyone else as the one being restored. Each mark ends
        when that bot's own restoration settles (``restoring``) -- and every
        mark still standing ends with ``restorer`` itself, however it ends.
        """
        for strategy_instance_id in strategy_instance_ids:
            self._runtime_access(strategy_instance_id).restorer = restorer
        restorer.add_done_callback(self._end_restorer)

    @contextmanager
    def restoring(self, strategy_instance_id: str) -> Iterator[None]:
        """Mark one ``sim:`` account restoring by the current task until this settles (#2684)."""
        access = self._runtime_access(strategy_instance_id)
        restorer = asyncio.current_task()
        access.restorer = restorer
        try:
            yield
        finally:
            if access.restorer is restorer:
                access.restorer = None

    def is_restoring(self, strategy_instance_id: str) -> bool:
        """Whether a restoration holds this ``sim:`` account: the one answer Start, Stop and every read share."""
        access = self.synthetic_runtime_access.get(strategy_instance_id)
        return access is not None and access.is_restoring()

    def _end_restorer(self, restorer: asyncio.Task[Any]) -> None:
        for access in self.synthetic_runtime_access.values():
            if access.restorer is restorer:
                access.restorer = None

    def _runtime_access(self, strategy_instance_id: str) -> SyntheticRuntimeAccess:
        """The one lock per ``sim:`` account, shared by its bound and unbound authorities."""
        return self.synthetic_runtime_access.setdefault(strategy_instance_id, SyntheticRuntimeAccess())


__all__ = [
    "BindingAuthority",
    "BindingAuthoritySelector",
    "PrimaryAccountBindingAuthority",
    "SealedShadowBindingAuthority",
    "SyntheticBindingAuthority",
    "UnboundDryRunAuthority",
    "primary_custody_kind",
]
