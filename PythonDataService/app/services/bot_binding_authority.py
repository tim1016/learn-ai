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
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityKind,
    evidence_account_id_for,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.active_authority import (
    DRY_RUN_ACCOUNT_HELD_SENTENCE,
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
from app.broker.alpaca.clerk.sqlite.budget_authority import authority_review_token, commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.synthetic_activation import SyntheticActivationStore
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.engine.live.bot_lifecycle_state import BotLifecycleStateRepo
from app.schemas.account_authority import CustodyWorld
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_lifecycle_projection import (
    AlpacaLifecycleProjector,
    SqliteAlpacaLifecycleAuthority,
)
from app.services.bot_start_admission import (
    AdmissionCustodyCut,
    StartAdmissionUnavailable,
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

#: The one owner sentence for a Dry Run whose simulated account another live
#: copy of this Clerk still holds (#2670). Authored in the broker layer
#: (``active_authority``, ``DRY_RUN_ACCOUNT_HELD_SENTENCE``) so the
#: authority's own startup failure, the panel, and Start all quote it;
#: mapped from the typed ``ExecutionLeaseHeld``, never from its message: an
#: internal ``sim:`` id and the term "execution lease" are not owner words.
#: Re-exported here for the runner-side seams that never import the broker
#: layer's selection module directly.


class SyntheticAccountRestoring(RuntimeError):
    """A ``sim:`` account is under boot's restoration; answer at once (#2684).

    Raised by the account layer before any lock wait, so every reader and
    action -- the panel, the roster, replay, fees, Start -- gets the same
    immediate answer instead of waiting out the restoration's lease deadline.
    """


class DryRunAccountHeldElsewhere(StartAdmissionUnavailable):
    """A ``sim:`` account's lease is still held by another live process (#2670).

    The typed form of the synthetic authority's lease-held startup failure;
    Start's re-check and boot's restoration both recognize it, and its words
    are :data:`DRY_RUN_ACCOUNT_HELD_SENTENCE` -- one sentence everywhere.
    """

    def __init__(self) -> None:
        headline, detail = DRY_RUN_ACCOUNT_HELD_SENTENCE
        super().__init__(headline, detail=detail)


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

    ``restoring`` marks the account as under boot's restoration (#2684):
    while another task restores, every other reader and action is answered
    at once instead of queueing behind the restoration's lease wait.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = field(default=None, init=False)
    restoring: bool = field(default=False, init=False)

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        current = asyncio.current_task()
        if current is not None and self.owner is current:
            yield
            return
        async with self.lock:
            self.owner = current
            try:
                yield
            finally:
                self.owner = None

    def refuse_if_restoring_elsewhere(self) -> None:
        """The one fail-fast restoring check (#2684).

        Set by the restoration itself and read by every opening; the restorer
        and its own reentrant calls pass through untouched.
        """
        if not self.restoring:
            return
        current = asyncio.current_task()
        if self.owner is current:
            return
        headline, _detail = DRY_RUN_RESTORING_SENTENCE
        raise SyntheticAccountRestoring(headline)


#: The owner sentence every immediate restoring answer carries (#2684).
DRY_RUN_RESTORING_SENTENCE = (
    "This Dry Run is still being restored after the Clerk restarted.",
    "Wait up to a minute, then try again.",
)


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
        """Compose this account for operation -- its full recovery included -- or raise why it cannot."""
        runtime = await self.open(SyntheticOpening.OPERATE, lease_wait_s=lease_wait_s)
        if runtime.clerk is None:
            failure = runtime.startup_failure
            if failure is not None and failure.reason_code == "SYNTHETIC_CLERK_LEASE_HELD":
                # The typed lease-held answer (#2670): boot's restoration,
                # Start's re-check, and the panel all recognize this one.
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
    async def restoring(self) -> AsyncIterator[None]:
        """Mark this account as under boot's restoration, under its lock (#2684).

        The restorer already holds this access through its recovery; the mark
        is what every *other* reader and action sees, so each is answered at
        once with :data:`DRY_RUN_RESTORING_SENTENCE` instead of queueing
        behind the lease wait. Cleared on every exit, failure included.
        """
        async with self.runtime_access.hold():
            self.runtime_access.restoring = True
            try:
                yield
            finally:
                self.runtime_access.restoring = False

    def is_restoring(self) -> bool:
        """Whether boot's restoration currently holds this account."""
        return self.runtime_access.restoring

    @asynccontextmanager
    async def held_for_request(self, opening: SyntheticOpening) -> AsyncIterator[ActiveClerkRuntime]:
        """This account for one request; a runtime composed only for it is released after it."""
        self.runtime_access.refuse_if_restoring_elsewhere()
        async with self.runtime_access.hold():
            was_active = get_clerk_runtime(self.account_id) is not None
            runtime = await self.open(opening)
            try:
                yield runtime
            finally:
                if not was_active:
                    await self.release_if_unused()

    async def release_if_unused(self) -> None:
        self.runtime_access.refuse_if_restoring_elsewhere()
        async with self.runtime_access.hold():
            if self.runtime_in_use(self.strategy_instance_id):
                return
            runtime = get_clerk_runtime(self.account_id)
            if runtime is not None:
                unregister_clerk_runtime(self.account_id)
                await runtime.close()
            self.brokers.pop(self.account_id, None)

    async def open(self, opening: SyntheticOpening, *, lease_wait_s: float = 0.0) -> ActiveClerkRuntime:
        self.runtime_access.refuse_if_restoring_elsewhere()
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

    def restoring(self) -> AbstractAsyncContextManager[None]:
        """Mark this Dry Run's account as under boot's restoration (#2684)."""
        return self._account.restoring()

    def is_restoring(self) -> bool:
        """Whether boot's restoration currently holds this Dry Run's account."""
        return self._account.is_restoring()

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

    def restoring(self) -> AbstractAsyncContextManager[None]:
        """Mark this Dry Run's account as under boot's restoration (#2684)."""
        return self._account.restoring()

    def is_restoring(self) -> bool:
        """Whether boot's restoration currently holds this Dry Run's account."""
        return self._account.is_restoring()

    async def ensure_recoverable(self, *, lease_wait_s: float = 0.0) -> None:
        await self._account.ensure_operating(lease_wait_s=lease_wait_s)


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

    def _runtime_access(self, strategy_instance_id: str) -> SyntheticRuntimeAccess:
        """The one lock per ``sim:`` account, shared by its bound and unbound authorities."""
        return self.synthetic_runtime_access.setdefault(strategy_instance_id, SyntheticRuntimeAccess())


__all__ = [
    "DRY_RUN_ACCOUNT_HELD_SENTENCE",
    "DRY_RUN_RESTORING_SENTENCE",
    "BindingAuthority",
    "BindingAuthoritySelector",
    "PrimaryAccountBindingAuthority",
    "SyntheticAccountRestoring",
    "SyntheticBindingAuthority",
    "UnboundDryRunAuthority",
    "primary_custody_kind",
]
