"""Projection/execution orchestration for a new-run Resume."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from app.broker.alpaca.clerk.active_protocol import ClerkAdmissionSnapshotStaleError
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.marketdata.feed import MarketDataFeed
from app.schemas.broker_bots import BotStatusView
from app.schemas.run_admission import (
    ResumeCheckpointAdmissionFact,
    ResumeRunFacts,
    RunAdmissionDecision,
    RunProcessAdmissionFact,
    StartRuntimeAdmissionFact,
)
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_carryover import configuration_hash
from app.services.bot_start_admission import (
    CustodyBoundActivator,
    MarketLivenessFactResolver,
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

CustodyGuard = Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]]
ProcessFactResolver = Callable[[BrokerBotBinding, int], RunProcessAdmissionFact]
RuntimeFactResolver = Callable[[str, int], Awaitable[StartRuntimeAdmissionFact]]
CheckpointResolver = Callable[[BrokerBotBinding], ResumeCheckpointAdmissionFact | None]


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
        activate: CustodyBoundActivator,
        carryover_account_policy_enabled: bool,
        session_capability: SessionCapabilityResolver,
        market_liveness: MarketLivenessFactResolver = market_liveness_fact,
    ) -> None:
        self._now_ms = now_ms
        self._feed_resolver = feed_resolver
        self._custody_guard = custody_guard
        self._process_fact = process_fact
        self._runtime_fact = runtime_fact
        self._checkpoint = checkpoint
        self._activate = activate
        self._carryover_account_policy_enabled = carryover_account_policy_enabled
        self._session_capability = session_capability
        self._market_liveness = market_liveness

    async def preview(
        self,
        prior: BrokerBotBinding,
        status: BotStatusView,
    ) -> RunAdmissionDecision:
        """Evaluate Resume without mutation while holding the Clerk fence."""
        proposed = new_run_binding(_request_from(prior), now_ms=self._now_ms())
        async with self._decision(prior, proposed, status) as (decision, _feed, _custody):
            return decision

    async def resume(
        self,
        prior: BrokerBotBinding,
        status: BotStatusView,
    ) -> AdmittedBotResume:
        """Evaluate and activate a new run inside one Clerk custody cut."""
        proposed = new_run_binding(_request_from(prior), now_ms=self._now_ms())
        async with self._decision(prior, proposed, status) as (decision, feed, custody):
            if not decision.allowed:
                raise StartAdmissionDenied(decision)
            assert feed is not None
            activation_ms = self._now_ms()
            proposed = proposed.model_copy(update={"created_at_ms": activation_ms})
            bot = await self._activate(proposed, feed, activation_ms, custody)
        return AdmittedBotResume(bot=bot, admission=decision)

    @asynccontextmanager
    async def _decision(
        self,
        prior: BrokerBotBinding,
        proposed: BrokerBotBinding,
        status: BotStatusView,
    ) -> AsyncIterator[tuple[RunAdmissionDecision, MarketDataFeed | None, ClerkCustodySnapshot]]:
        try:
            async with self._custody_guard(prior.strategy_instance_id) as custody:
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
                facts = ResumeRunFacts(
                    strategy_instance_id=prior.strategy_instance_id,
                    proposed_run_id=proposed.run_id,
                    prior_run_id=prior.run_id,
                    configuration_hash=configuration_hash(prior),
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
                )
                yield (
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
    )
