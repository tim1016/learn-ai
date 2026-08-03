"""Focused orchestration for first-run admission and fenced activation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from app.broker.alpaca.clerk import ClerkAdmissionSnapshotChangedError
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.marketdata.feed import MarketDataFeed
from app.schemas.action_plan import ActionPlan
from app.schemas.broker_bots import BotStatusView
from app.schemas.run_admission import (
    MarketDataAdmissionFact,
    RunAdmissionDecision,
    RunProcessAdmissionFact,
    StartRunFacts,
    StartRuntimeAdmissionFact,
)
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_carryover import configuration_hash
from app.services.run_admission import evaluate_run_admission
from app.services.session_authority import session_state_at_ms

logger = logging.getLogger(__name__)

CustodyGuard = Callable[[str], AbstractAsyncContextManager[ClerkCustodySnapshot]]
ProcessFactResolver = Callable[[BrokerBotBinding, int], RunProcessAdmissionFact]
RuntimeFactResolver = Callable[[str, int], Awaitable[StartRuntimeAdmissionFact]]
StartActivator = Callable[[BrokerBotBinding, MarketDataFeed, int], Awaitable[BotStatusView]]


@dataclass(frozen=True)
class StartRequest:
    """Immutable configuration requested for one new strategy instance."""

    broker: str
    strategy_instance_id: str
    strategy_key: str
    symbol: str
    use_rth: bool
    mode: Literal["log_only", "dry_run", "trade"]
    quantity: int
    carryover_policy: Literal["FORBID", "ALLOW"]
    action_plan: ActionPlan


@dataclass(frozen=True)
class AdmittedBotStart:
    """Successful Start plus the exact decision used before activation."""

    bot: BotStatusView
    admission: RunAdmissionDecision


class StartAdmissionDenied(Exception):
    """The canonical policy refused Start with a typed decision."""

    def __init__(self, decision: RunAdmissionDecision) -> None:
        super().__init__(decision.explanation)
        self.decision = decision


class StartAdmissionEvidenceChanged(Exception):
    """Clerk custody could not be held at one stable journal cut."""


class StartAdmissionUnavailable(Exception):
    """The authority needed to evaluate Start is not installed."""

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def make_start_request(
    *,
    broker: str,
    strategy_instance_id: str,
    strategy_key: str,
    symbol: str,
    use_rth: bool,
    mode: Literal["log_only", "dry_run", "trade"],
    quantity: int,
    carryover_policy: Literal["FORBID", "ALLOW"],
    action_plan: ActionPlan,
) -> StartRequest:
    """Build the one typed request shared by preview and execution."""
    return StartRequest(
        broker=broker,
        strategy_instance_id=strategy_instance_id,
        strategy_key=strategy_key,
        symbol=symbol,
        use_rth=use_rth,
        mode=mode,
        quantity=quantity,
        carryover_policy=carryover_policy,
        action_plan=action_plan,
    )


async def resolve_start_runtime_fact(
    *,
    strategy_instance_id: str,
    observed_at_ms: int,
    boot_recovery_required: bool,
    boot_recovery_complete: bool,
    unresolved_intents_probe: Callable[[], Awaitable[int]] | None,
    projected_start_count: int,
    restart_threshold: int,
    restart_window_ms: int,
) -> StartRuntimeAdmissionFact:
    """Project recovery and restart intensity without mutating runner state."""
    if boot_recovery_required and not boot_recovery_complete:
        return StartRuntimeAdmissionFact(
            state="BOOT_RECOVERY_INCOMPLETE",
            observed_at_ms=observed_at_ms,
            explanation="Bot runner recovery has not completed after process startup.",
            next_step="Wait for the boot recovery sweep before Start.",
        )
    if unresolved_intents_probe is not None:
        try:
            unresolved = await unresolved_intents_probe()
        except Exception as exc:
            logger.warning(
                "Start recovery probe failed",
                extra={
                    "action": "start_admission_recovery_unknown",
                    "strategy_instance_id": strategy_instance_id,
                    "error": str(exc),
                },
            )
            return StartRuntimeAdmissionFact(
                state="RECOVERY_UNCERTAIN",
                observed_at_ms=observed_at_ms,
                explanation="The bot runner could not prove recovery completeness.",
                next_step="Restore the recovery probe and reconcile before Start.",
            )
        if unresolved > 0:
            return StartRuntimeAdmissionFact(
                state="RECOVERY_UNCERTAIN",
                observed_at_ms=observed_at_ms,
                explanation=f"{unresolved} order intent(s) remain unresolved after recovery.",
                next_step="Resolve recovery intents before Start.",
            )
    if projected_start_count >= restart_threshold:
        return StartRuntimeAdmissionFact(
            state="RESTART_INTENSITY_EXCEEDED",
            observed_at_ms=observed_at_ms,
            explanation=(
                f"The next activation would be number {projected_start_count} inside "
                f"the {restart_window_ms} ms restart window."
            ),
            next_step="Wait for the restart window to clear before Start.",
        )
    return StartRuntimeAdmissionFact(
        state="READY",
        observed_at_ms=observed_at_ms,
        explanation="Boot recovery and restart intensity admit Start.",
    )


def default_start_custody_guard(
    strategy_instance_id: str,
) -> AbstractAsyncContextManager[ClerkCustodySnapshot]:
    """Resolve the production Clerk guard lazily after application startup."""
    from app.broker.alpaca.clerk import get_alpaca_clerk

    clerk = get_alpaca_clerk()
    if clerk is None:
        raise StartAdmissionUnavailable(
            "Start admission is unavailable because the Clerk is not installed.",
            detail="Restore the account Clerk before starting a bot.",
        )
    return clerk.start_admission_snapshot(strategy_instance_id)


def new_run_binding(request: StartRequest, *, now_ms: int) -> BrokerBotBinding:
    """Mint the per-run identity around immutable strategy configuration."""
    return BrokerBotBinding(
        strategy_instance_id=request.strategy_instance_id,
        strategy_key=request.strategy_key,
        broker=request.broker,
        symbol=request.symbol,
        use_rth=request.use_rth,
        mode=request.mode,
        quantity=request.quantity,
        carryover_policy=request.carryover_policy,
        action_plan=request.action_plan,
        run_id=uuid4().hex,
        created_at_ms=now_ms,
    )


def log_run_launch(
    binding: BrokerBotBinding,
    *,
    reason: Literal["deploy", "resume"],
) -> None:
    """Emit the common structured launch event for Start and Resume."""
    logger.info(
        "Bot run launched",
        extra={
            "action": "bot_run_launched",
            "launch_reason": reason,
            "strategy_instance_id": binding.strategy_instance_id,
            "broker": binding.broker,
            "symbol": binding.symbol,
            "run_id": binding.run_id,
        },
    )


class BotStartAdmission:
    """Own the shared preview/execution path for a first Start."""

    def __init__(
        self,
        *,
        now_ms: Callable[[], int],
        feed_resolver: Callable[[], MarketDataFeed | None],
        custody_guard: CustodyGuard,
        process_fact: ProcessFactResolver,
        runtime_fact: RuntimeFactResolver,
        activate: StartActivator,
    ) -> None:
        self._now_ms = now_ms
        self._feed_resolver = feed_resolver
        self._custody_guard = custody_guard
        self._process_fact = process_fact
        self._runtime_fact = runtime_fact
        self._activate = activate

    async def preview(self, request: StartRequest) -> RunAdmissionDecision:
        """Evaluate without mutation while holding the same Clerk fence."""
        binding = new_run_binding(request, now_ms=self._now_ms())
        async with self._decision(binding) as (decision, _feed):
            return decision

    async def start(self, request: StartRequest) -> AdmittedBotStart:
        """Evaluate and activate inside one stable Clerk custody cut."""
        binding = new_run_binding(request, now_ms=self._now_ms())
        async with self._decision(binding) as (decision, feed):
            if not decision.allowed:
                raise StartAdmissionDenied(decision)
            assert feed is not None
            activation_ms = self._now_ms()
            binding = binding.model_copy(update={"created_at_ms": activation_ms})
            bot = await self._activate(binding, feed, activation_ms)
        return AdmittedBotStart(bot=bot, admission=decision)

    @asynccontextmanager
    async def _decision(
        self, binding: BrokerBotBinding
    ) -> AsyncIterator[tuple[RunAdmissionDecision, MarketDataFeed | None]]:
        try:
            async with self._custody_guard(binding.strategy_instance_id) as custody:
                observed_at_ms = self._now_ms()
                runtime = await self._runtime_fact(binding.strategy_instance_id, observed_at_ms)
                process = self._process_fact(binding, observed_at_ms)
                feed = self._feed_resolver()
                facts = StartRunFacts(
                    strategy_instance_id=binding.strategy_instance_id,
                    proposed_run_id=binding.run_id,
                    configuration_hash=configuration_hash(binding),
                    runtime=runtime,
                    process=process,
                    market_data=market_data_admission_fact(
                        feed,
                        observed_at_ms,
                        use_rth=binding.use_rth,
                    ),
                )
                yield (
                    evaluate_run_admission(
                        facts,
                        custody,
                        evaluated_at_ms=self._now_ms(),
                    ),
                    feed,
                )
        except ClerkAdmissionSnapshotChangedError as exc:
            raise StartAdmissionEvidenceChanged("Clerk custody evidence changed before Start could be fenced.") from exc


def market_data_admission_fact(
    feed: MarketDataFeed | None,
    observed_at_ms: int,
    *,
    use_rth: bool,
) -> MarketDataAdmissionFact:
    if feed is None:
        return MarketDataAdmissionFact(
            state="UNAVAILABLE",
            observed_at_ms=observed_at_ms,
            reason="The process-level market-data feed is not installed.",
        )
    try:
        health = feed.health()
    except Exception as exc:
        logger.warning(
            "market-data health could not author run admission",
            extra={
                "action": "run_admission_market_data_unknown",
                "feed_id": feed.feed_id,
                "error": str(exc),
            },
        )
        return MarketDataAdmissionFact(
            state="UNKNOWN",
            feed_id=feed.feed_id,
            observed_at_ms=observed_at_ms,
            reason="The market-data health probe failed.",
        )
    session = session_state_at_ms(now_ms=health.observed_at_ms)
    bars_expected = session.phase == "RTH" if use_rth else session.phase in {"PRE", "RTH", "POST", "OVERNIGHT"}
    stalled_subscription = health.stale and health.active_subscription_count > 0 and bars_expected
    state: Literal["AVAILABLE", "STALE", "UNAVAILABLE"] = (
        "UNAVAILABLE" if not health.connected else ("STALE" if stalled_subscription else "AVAILABLE")
    )
    reason = health.reason or None
    if health.connected and health.stale and not stalled_subscription:
        reason = (
            "The connected feed is idle; Start may establish the candidate subscription."
            if health.active_subscription_count == 0
            else f"No {'RTH ' if use_rth else ''}bar is expected during {session.phase}."
        )
    return MarketDataAdmissionFact(
        state=state,
        feed_id=feed.feed_id,
        last_bar_ms=health.last_bar_ms,
        observed_at_ms=health.observed_at_ms,
        reason=reason,
    )
