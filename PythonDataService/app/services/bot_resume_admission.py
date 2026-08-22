"""Projection/execution orchestration for a new-run Resume."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.marketdata.feed import MarketDataFeed
from app.schemas.broker_bots import BotStatusView
from app.schemas.run_admission import (
    ProgramBuildAdmissionFact,
    ResumeCheckpointAdmissionFact,
    ResumeRunFacts,
    RunAdmissionDecision,
    RunProcessAdmissionFact,
    StartRuntimeAdmissionFact,
    StrategyValidationAdmissionFact,
    TerminalEvidenceAdmissionFact,
)
from app.services.bot_binding_repository import BrokerBotBinding, LegacyMigrationLineageRecord
from app.services.bot_carryover import configuration_hash
from app.services.bot_start_admission import (
    CustodyBoundActivator,
    MarketLivenessFactResolver,
    RunAdmissionInvariantError,
    SessionCapabilityResolver,
    StartAdmissionDenied,
    StartAdmissionEvidenceChanged,
    StartRequest,
    market_data_admission_fact,
    market_data_capability_account_id,
    new_run_binding,
)
from app.services.bot_trade_strategy import EXPOSURE_CARRYOVER_STRATEGY_KEYS
from app.services.market_liveness import market_liveness_fact
from app.services.run_admission import evaluate_run_admission
from app.services.signal_program_admission import (
    LegacyProgramUnreconstructibleError,
    legacy_migration_clone_instance_id,
    prove_running_program_build,
    reconstruct_legacy_program_seal,
)
from app.services.strategy_validation_admission import current_strategy_validation_fact

CustodyGuard = Callable[[BrokerBotBinding], AbstractAsyncContextManager[ClerkCustodySnapshot]]
ProcessFactResolver = Callable[[BrokerBotBinding, int], RunProcessAdmissionFact]
RuntimeFactResolver = Callable[[str, int], Awaitable[StartRuntimeAdmissionFact]]
CheckpointResolver = Callable[[BrokerBotBinding], ResumeCheckpointAdmissionFact | None]
TerminalEvidenceResolver = Callable[[BrokerBotBinding], TerminalEvidenceAdmissionFact]
ValidationFactResolver = Callable[[BrokerBotBinding, int], StrategyValidationAdmissionFact]


class LegacyMigrationLineageWriter(Protocol):
    """The one write capability a mutating Resume needs for PRD Sec 11.5.

    Deliberately narrower than ``BotBindingRepository`` (its only production
    implementer): Resume never reads or writes anything else through this
    seam, so typing the dependency against the full repository would make
    "does Resume have what it needs to keep its promise" a matter of
    auditing the whole repository's surface instead of this one method.
    """

    def ensure_legacy_migration_clone_lineage(
        self,
        *,
        original_strategy_instance_id: str,
        clone_instance_id: str,
        reason: str,
        now_ms: int,
    ) -> LegacyMigrationLineageRecord: ...


class LegacyMigrationLineageUnavailableError(RuntimeError):
    """A mutating Resume cannot durably record the clone lineage it needs.

    PRD Sec 11.5 / ADR 0043 Sec 3: the clone branch's returned
    ``ProgramBuildAdmissionFact.next_step`` tells the operator the clone
    "carries explicit lineage back to this instance" — a claim about
    durable evidence, not an aspiration. ``resume()`` (``mutating=True``)
    must therefore never reach that return with lineage unwritten. Raising
    here — instead of silently skipping the write, as a ``None`` writer
    used to let happen — makes that impossible: either the lineage record
    exists before the decision is returned, or the caller sees this error
    instead of a decision that promises evidence it never persisted.
    ``preview()`` (``mutating=False``) never reaches this branch; it is not
    allowed to write anything, by design.
    """


@dataclass(frozen=True)
class AdmittedBotResume:
    """Successful Resume plus the exact decision used before activation."""

    bot: BotStatusView
    admission: RunAdmissionDecision


class BotResumeAdmission:
    """Own the shared preview/execution path for a new-run Resume."""

    def __init__(
        self,
        *,
        now_ms: Callable[[], int],
        feed_resolver: Callable[[], MarketDataFeed | None],
        custody_guard: CustodyGuard,
        process_fact: ProcessFactResolver,
        runtime_fact: RuntimeFactResolver,
        checkpoint: CheckpointResolver,
        terminal_evidence: TerminalEvidenceResolver,
        validation_fact: ValidationFactResolver = current_strategy_validation_fact,
        activate: CustodyBoundActivator,
        carryover_account_policy_enabled: bool,
        session_capability: SessionCapabilityResolver,
        market_liveness: MarketLivenessFactResolver = market_liveness_fact,
        legacy_migration_repository: LegacyMigrationLineageWriter | None = None,
    ) -> None:
        self._now_ms = now_ms
        self._feed_resolver = feed_resolver
        self._custody_guard = custody_guard
        self._process_fact = process_fact
        self._runtime_fact = runtime_fact
        self._checkpoint = checkpoint
        self._terminal_evidence = terminal_evidence
        self._validation_fact = validation_fact
        self._activate = activate
        self._carryover_account_policy_enabled = carryover_account_policy_enabled
        self._session_capability = session_capability
        self._market_liveness = market_liveness
        # PRD Sec 11.5 legacy migration (#1728): only ``resume()`` persists
        # clone lineage evidence (``preview()`` stays mutation-free). ``None``
        # keeps every existing caller working unchanged for the common
        # append path, which needs no lineage writer at all. A mutating
        # Resume that actually reaches the clone branch is different: it
        # must not return a decision whose ``next_step`` promises lineage it
        # never wrote, so a ``None`` writer at that point fails closed with
        # ``LegacyMigrationLineageUnavailableError`` instead of silently
        # completing — see ``_resolve_program_build``.
        self._legacy_migration_repository = legacy_migration_repository

    async def preview(
        self,
        prior: BrokerBotBinding,
        status: BotStatusView,
    ) -> RunAdmissionDecision:
        """Evaluate Resume without mutation while holding the Clerk fence."""
        proposed = new_run_binding(_request_from(prior), now_ms=self._now_ms())
        async with self._decision(prior, proposed, status, mutating=False) as (
            _sealed_proposed,
            decision,
            _feed,
            _custody,
        ):
            return decision

    async def resume(
        self,
        prior: BrokerBotBinding,
        status: BotStatusView,
    ) -> AdmittedBotResume:
        """Evaluate and activate a new run inside one Clerk custody cut."""
        proposed = new_run_binding(_request_from(prior), now_ms=self._now_ms())
        async with self._decision(prior, proposed, status, mutating=True) as (proposed, decision, feed, custody):
            if not decision.allowed:
                raise StartAdmissionDenied(decision)
            if feed is None:
                raise RunAdmissionInvariantError(
                    f"Resume for '{prior.strategy_instance_id}' was admitted with no "
                    "market-data feed resolved."
                )
            activation_ms = self._now_ms()
            proposed = proposed.model_copy(update={"created_at_ms": activation_ms})
            bot = await self._activate(proposed, feed, activation_ms, custody)
        return AdmittedBotResume(bot=bot, admission=decision)

    def _resolve_program_build(
        self,
        *,
        prior: BrokerBotBinding,
        proposed: BrokerBotBinding,
        validation: StrategyValidationAdmissionFact,
        verified_at_ms: int,
        mutating: bool,
    ) -> tuple[BrokerBotBinding, ProgramBuildAdmissionFact]:
        """Prove the running build, migrating a pre-v2-seal instance first.

        PRD Sec 11.5: a missing seal on an *existing* instance means it
        predates the v2 seal format, not that ordinary Start construction
        applies. An exact reconstruction is appended to this same instance —
        mirroring Start's own auto-seal (``bot_start_admission._decision``) —
        by attaching it to ``proposed`` so the caller's normal activation
        path persists it via the existing idempotent
        ``BotBindingRepository._ensure_sealed_program``. An unprovable
        parameter set fails closed and, only on the mutating ``resume()``
        call (never ``preview()``), durably records create-once lineage
        evidence naming the clone that must be deployed instead *before*
        returning the refusal — a mutating call that reaches this branch
        with no lineage writer configured raises
        :class:`LegacyMigrationLineageUnavailableError` rather than return a
        decision whose ``next_step`` promises lineage it never persisted.
        """
        if proposed.sealed_program is not None:
            return proposed, prove_running_program_build(proposed, verified_at_ms=verified_at_ms)
        try:
            reconstructed = reconstruct_legacy_program_seal(prior, validation)
        except LegacyProgramUnreconstructibleError:
            clone_instance_id = legacy_migration_clone_instance_id(prior.strategy_instance_id)
            if mutating:
                if self._legacy_migration_repository is None:
                    raise LegacyMigrationLineageUnavailableError(
                        f"Resume for '{prior.strategy_instance_id}' would refuse with a "
                        f"clone instruction naming '{clone_instance_id}', but no lineage "
                        "writer is configured to durably record that clone's lineage."
                    )
                self._legacy_migration_repository.ensure_legacy_migration_clone_lineage(
                    original_strategy_instance_id=prior.strategy_instance_id,
                    clone_instance_id=clone_instance_id,
                    reason=(
                        "Persisted v1 parameters no longer validate against the current "
                        f"'{prior.strategy_key}' contract."
                    ),
                    now_ms=verified_at_ms,
                )
            return proposed, ProgramBuildAdmissionFact(
                state="UNPROVEN",
                program_key=prior.strategy_key,
                verified_at_ms=verified_at_ms,
                explanation=(
                    "This instance's persisted parameters no longer validate against the "
                    f"registered '{prior.strategy_key}' contract, so an exact v2 seal cannot "
                    "be reconstructed. It remains inspectable but cannot Resume."
                ),
                next_step=(
                    f"Deploy the cloned successor instance '{clone_instance_id}'; it carries "
                    "explicit lineage back to this instance and requires fresh parameters."
                ),
            )
        if reconstructed is not None:
            proposed = proposed.model_copy(update={"sealed_program": reconstructed})
        return proposed, prove_running_program_build(proposed, verified_at_ms=verified_at_ms)

    @asynccontextmanager
    async def _decision(
        self,
        prior: BrokerBotBinding,
        proposed: BrokerBotBinding,
        status: BotStatusView,
        *,
        mutating: bool,
    ) -> AsyncIterator[tuple[BrokerBotBinding, RunAdmissionDecision, MarketDataFeed | None, ClerkCustodySnapshot]]:
        try:
            async with self._custody_guard(prior) as custody:
                proposed = proposed.model_copy(
                    update={
                        "sealed_account_id": prior.sealed_account_id,
                        "sealed_program": prior.sealed_program,
                        "strategy_param_origins": prior.strategy_param_origins,
                    }
                )
                observed_at_ms = self._now_ms()
                feed = self._feed_resolver()
                capability_account_id = market_data_capability_account_id(feed)
                runtime = await self._runtime_fact(
                    prior.strategy_instance_id,
                    observed_at_ms,
                )
                # Re-captured after the await: the market clock refreshes on
                # its own cadence (~1s) independent of this coroutine, so the
                # pre-await instant can already be older than freshly-arrived
                # evidence by the time we reach here — compose_market_liveness's
                # own freshness check would then see `now_ms < observed_at_ms`
                # and refuse Resume outright.
                observed_at_ms = self._now_ms()
                validation = self._validation_fact(prior, observed_at_ms)
                proposed, program_build = self._resolve_program_build(
                    prior=prior,
                    proposed=proposed,
                    validation=validation,
                    verified_at_ms=observed_at_ms,
                    mutating=mutating,
                )
                proposed = proposed.model_copy(update={"program_build": program_build})
                facts = ResumeRunFacts(
                    strategy_instance_id=prior.strategy_instance_id,
                    proposed_run_id=proposed.run_id,
                    prior_run_id=prior.run_id,
                    configuration_hash=configuration_hash(prior),
                    sealed_account_id=prior.sealed_account_id or "",
                    mode=prior.mode,
                    program_build=program_build,
                    validation=validation,
                    runtime=runtime,
                    process=self._process_fact(prior, observed_at_ms),
                    market_data=market_data_admission_fact(
                        feed,
                        observed_at_ms,
                        symbol=prior.symbol,
                        use_rth=prior.use_rth,
                        capability=(
                            self._session_capability(prior.symbol, capability_account_id)
                            if capability_account_id is not None
                            else None
                        ),
                        account_id=capability_account_id,
                    ),
                    market_liveness=self._market_liveness(
                        prior.symbol,
                        observed_at_ms,
                    ),
                    desired_state=status.desired_state,
                    phase=status.phase,
                    carryover_policy=prior.carryover_policy,
                    carryover_account_policy_enabled=(
                        self._carryover_account_policy_enabled
                    ),
                    exposure_carryover_supported=(
                        prior.strategy_key in EXPOSURE_CARRYOVER_STRATEGY_KEYS
                    ),
                    checkpoint=self._checkpoint(prior),
                    terminal_evidence=self._terminal_evidence(prior),
                )
                yield (
                    proposed,
                    evaluate_run_admission(
                        facts,
                        custody,
                        evaluated_at_ms=self._now_ms(),
                    ),
                    feed,
                    custody,
                )
        except ClerkAdmissionSnapshotStaleError as exc:
            raise StartAdmissionEvidenceChanged(
                "Clerk custody evidence changed before Resume could be fenced."
            ) from exc


def _request_from(binding: BrokerBotBinding) -> StartRequest:
    """Reuse immutable instance configuration while minting a new run ID."""
    return StartRequest(
        broker=binding.broker,
        strategy_instance_id=binding.strategy_instance_id,
        strategy_key=binding.strategy_key,
        symbol=binding.symbol,
        use_rth=binding.use_rth,
        mode=binding.mode,
        quantity=binding.quantity,
        carryover_policy=binding.carryover_policy,
        evidence_override=binding.evidence_override,
        action_plan=binding.action_plan,
        strategy_params=binding.strategy_params,
        strategy_param_origins=binding.strategy_param_origins,
    )
