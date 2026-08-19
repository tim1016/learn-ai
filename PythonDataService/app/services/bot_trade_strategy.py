"""Alpaca Clerk adapter for validated strategy decisions.

Strategy mathematics remains canonical in ``app.engine.strategy.algorithms``.
This module selects an admitted strategy, feeds it the broker-neutral minute
stream, and routes only its semantic ENTER/EXIT intents to the Clerk.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionOutcome, SqliteDecisionReceipts
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentDecision,
    DeploymentValidationDecisionKernel,
)
from app.engine.strategy.algorithms.ema_crossover_signal import (
    EmaCrossoverSignalAlgorithm,
)
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind
from app.marketdata.feed import MarketDataBar, MarketDataFeed
from app.schemas.broker_bots import AlpacaPaperStrategyKey
from app.schemas.market_liveness import MarketLivenessFact
from app.services.market_liveness import market_liveness_fact
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.services.bot_dry_run import DryRunActivityJournal
    from app.services.bot_runner import BrokerBotBinding

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_EFFECT_PURPOSE_BY_INTENT = {
    SignalIntentKind.ENTER: EffectPurpose.ENTER,
    SignalIntentKind.EXIT: EffectPurpose.EXIT,
}
_INTENT_BY_DEPLOYMENT_DECISION = {
    DeploymentDecision.ENTER: SignalIntentKind.ENTER,
    DeploymentDecision.EXIT: SignalIntentKind.EXIT,
}
# Carryover requires reconstructing the strategy's in-flight lifecycle from
# durable state. Deployment validation is the one deliberately bounded
# validation primitive approved for this path; every future strategy must opt
# in here with its reconstruction evidence rather than inheriting permission.
EXPOSURE_CARRYOVER_STRATEGY_KEYS: frozenset[AlpacaPaperStrategyKey] = frozenset(
    {AlpacaPaperStrategyKey.DEPLOYMENT_VALIDATION}
)


@dataclass(frozen=True)
class StrategyEvaluation:
    """One closed bar and the zero-or-one semantic intent it produced."""

    bar: MarketDataBar
    intents: tuple[SignalIntent, ...]


class _EffectReceipt(Protocol):
    state: object


class _RecordingSignalIntentExecutor:
    """Satisfy the strategy boundary while the async adapter drains intents."""

    def execute(
        self,
        _context: SignalIntentExecutionContext,
        _intent: SignalIntent,
    ) -> None:
        return


def _engine_bar(bar: MarketDataBar) -> TradeBar:
    """Translate the broker-neutral wire bar into the canonical engine bar."""
    return TradeBar(
        symbol=bar.symbol,
        time=datetime.fromtimestamp(bar.start_ms / 1000, tz=_NY),
        end_time=datetime.fromtimestamp(bar.end_ms / 1000, tz=_NY),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


async def _deployment_validation_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[StrategyEvaluation]:
    kernel = DeploymentValidationDecisionKernel()
    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        decision = kernel.on_closed_bar(
            end_ms=bar.end_ms,
            open_price=bar.open,
            close_price=bar.close,
        )
        kind = _INTENT_BY_DEPLOYMENT_DECISION.get(decision)
        intents = (
            ()
            if kind is None
            else (
                SignalIntent(
                    kind=kind,
                    bar_close_ms=bar.end_ms,
                    intended_price=bar.close,
                ),
            )
        )
        yield StrategyEvaluation(bar=bar, intents=intents)


async def _ema_crossover_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[StrategyEvaluation]:
    """Run the canonical EMA strategy against the production minute stream.

    Formula: canonical EMA(5)/EMA(10), RSI(14), gap and five-bar lifecycle.
    Reference: ``references/qc-shadow/SpyEmaCrossoverAlgorithm.py``.
    Canonical implementation: ``app.engine.strategy.algorithms.ema_crossover_signal``.
    Validated against: ``tests/services/test_bot_runner.py::test_ema_trade_bot_matches_first_lean_round_trip``.
    """
    strategy = EmaCrossoverSignalAlgorithm(symbol=binding.symbol)
    context = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.ctx = context
    strategy.initialize()
    context.set_signal_intent_executor(_RecordingSignalIntentExecutor())

    async for market_bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        bar = _engine_bar(market_bar)
        context.current_time = bar.end_time
        strategy.on_minute_bar(bar)
        for consolidator in context.get_consolidators(bar.symbol):
            consolidator.update(bar)
        intents = tuple(context.signal_intents)
        context.signal_intents.clear()
        # StrategyContext retains consolidated bars for finite backtest
        # charting. This adapter is long-lived and has no chart consumer, so
        # release each emitted bar after the strategy has processed it.
        context.consolidated_bars.clear()
        yield StrategyEvaluation(bar=market_bar, intents=intents)


async def strategy_evaluations(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[StrategyEvaluation]:
    try:
        strategy_key = AlpacaPaperStrategyKey(binding.strategy_key)
    except ValueError as exc:
        raise ValueError(f"unsupported Alpaca paper strategy: {binding.strategy_key}") from exc
    evaluation_stream = _STRATEGY_EVALUATION_STREAMS[strategy_key]
    async for evaluation in evaluation_stream(binding, feed):
        yield evaluation


async def strategy_intents(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[SignalIntent]:
    async for evaluation in strategy_evaluations(binding, feed):
        for intent in evaluation.intents:
            yield intent


_StrategyEvaluationStream = Callable[
    ["BrokerBotBinding", MarketDataFeed],
    AsyncIterator[StrategyEvaluation],
]
_STRATEGY_EVALUATION_STREAMS: dict[AlpacaPaperStrategyKey, _StrategyEvaluationStream] = {
    AlpacaPaperStrategyKey.DEPLOYMENT_VALIDATION: _deployment_validation_evaluations,
    AlpacaPaperStrategyKey.EMA_CROSSOVER_SIGNAL: _ema_crossover_evaluations,
}


def supported_alpaca_paper_strategy_keys() -> frozenset[AlpacaPaperStrategyKey]:
    """Return the strategies backed by an executable Clerk intent stream."""
    return frozenset(_STRATEGY_EVALUATION_STREAMS)


async def run_trade_bot(binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
    """Execute one admitted strategy; the Clerk owns all execution truth."""
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise RuntimeError("The SQLite Alpaca Clerk is unavailable; trade-mode decisions are blocked.")
    if getattr(clerk, "authority_kind", None) != "sqlite":
        raise RuntimeError("Trade-mode decisions require the active SQLite Alpaca Clerk.")
    account_id = getattr(clerk, "account_id", None)
    if not isinstance(account_id, str) or not account_id:
        raise RuntimeError("The active SQLite Clerk has no account identity for decision receipts.")
    repository = getattr(clerk, "repository", None)
    if repository is None:
        raise RuntimeError("The active SQLite Clerk has no repository for decision receipts.")
    decision_receipts = SqliteDecisionReceipts(
        repository,
        strategy_instance_id=binding.strategy_instance_id,
    )
    async for evaluation in strategy_evaluations(binding, feed):
        if len(evaluation.intents) > 1:
            raise RuntimeError("A supported trade strategy emitted multiple intents for one closed bar.")
        if not evaluation.intents:
            _append_decision_receipt(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                outcome="no_action",
                reason_code="NO_ACTION",
            )
            continue
        intent = evaluation.intents[0]
        intent_id = f"{intent.bar_close_ms}:{intent.kind.value}"
        # The liveness gate applies only to ENTER — creating new exposure.
        # EXIT is deliberately exempt and always reaches the Clerk unblocked:
        # an emergency risk-reduction close must never be held hostage by
        # missing/stale liveness evidence (#1671 AC3). If a distinct
        # cancellation primitive is ever added, it must be exempted the
        # same way for the same reason.
        if intent.kind is SignalIntentKind.ENTER:
            liveness = market_liveness_fact(binding.symbol, now_ms_utc())
            if liveness.state != "TRADABLE":
                _append_decision_receipt(
                    decision_receipts,
                    binding=binding,
                    evaluation=evaluation,
                    outcome="blocked",
                    reason_code=liveness.reason_code,
                    intent_id=intent_id,
                    liveness=liveness,
                )
                logger.warning(
                    "Trade bot blocked new exposure on live market-liveness evidence",
                    extra={
                        "action": "bot_market_liveness_blocked",
                        "strategy_instance_id": binding.strategy_instance_id,
                        "strategy_key": binding.strategy_key,
                        "symbol": binding.symbol,
                        "market_liveness_state": liveness.state,
                        "reason_code": liveness.reason_code,
                    },
                )
                continue
        _append_decision_receipt(
            decision_receipts,
            binding=binding,
            evaluation=evaluation,
            outcome=("enter_intent" if intent.kind is SignalIntentKind.ENTER else "exit_intent"),
            reason_code=f"STRATEGY_{intent.kind.value}",
            intent_id=intent_id,
        )
        logger.info(
            "Trade bot decision",
            extra={
                "action": "bot_decision",
                "strategy_instance_id": binding.strategy_instance_id,
                "strategy_key": binding.strategy_key,
                "decision": intent.kind,
                "symbol": binding.symbol,
                "bar_end_ms": intent.bar_close_ms,
            },
        )
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=binding.strategy_instance_id,
            run_id=binding.run_id,
            decision_id=intent_id,
            purpose=_EFFECT_PURPOSE_BY_INTENT[intent.kind],
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
        if _effect_state_value(receipt) == EffectOperationState.REJECTED.value:
            _record_blocked_decision(
                decision_receipts,
                binding=binding,
                evaluation=evaluation,
                intent_id=intent_id,
                receipt=receipt,
            )
        logger.info(
            "Trade bot effect accepted",
            extra={
                "action": "bot_effect_accepted",
                "strategy_instance_id": binding.strategy_instance_id,
                "strategy_key": binding.strategy_key,
                "purpose": intent.kind,
                "effect_state": receipt.state.value,
                "order_refs": receipt.child_order_refs,
            },
        )


def _append_decision_receipt(
    receipts: SqliteDecisionReceipts,
    *,
    binding: BrokerBotBinding,
    evaluation: StrategyEvaluation,
    outcome: DecisionOutcome,
    reason_code: str,
    intent_id: str = "",
    order_ref: str = "",
    liveness: MarketLivenessFact | None = None,
) -> None:
    facts: dict[str, object] = {
        "bar_ref": f"{binding.symbol}@{evaluation.bar.end_ms}",
        "reason_code": reason_code,
    }
    if liveness is not None:
        facts["market_liveness"] = liveness.model_dump(mode="json")
    receipts.append(
        outcome=outcome,
        symbol=binding.symbol,
        observed_at_ms=now_ms_utc(),
        facts=facts,
        intent_id=intent_id or None,
        order_ref=order_ref or None,
    )


def _record_blocked_decision(
    receipts: SqliteDecisionReceipts,
    *,
    binding: BrokerBotBinding,
    evaluation: StrategyEvaluation,
    intent_id: str,
    receipt: _EffectReceipt,
) -> None:
    """Replace a provisional intent with the Clerk's final admission refusal."""
    refusal_reason = str(
        getattr(receipt, "next_step", None)
        or getattr(receipt, "explanation", None)
        or "The Account Clerk rejected this strategy submission."
    )
    receipts.update_final_outcome(
        bar_ref=f"{binding.symbol}@{evaluation.bar.end_ms}",
        outcome="blocked",
        order_ref=None,
        facts={
            "bar_ref": f"{binding.symbol}@{evaluation.bar.end_ms}",
            "reason_code": "CLERK_ADMISSION_REJECTED",
            "refusal_reason": refusal_reason,
        },
    )


def _effect_state_value(receipt: _EffectReceipt) -> str:
    state = receipt.state
    return str(getattr(state, "value", state))


async def run_dry_run_bot(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
    journal: DryRunActivityJournal,
) -> None:
    """Run real strategy decisions with durable simulated fills and no Clerk."""
    from app.services.bot_dry_run import DryRunActivity

    async for intent in strategy_intents(binding, feed):
        side = "buy" if intent.kind is SignalIntentKind.ENTER else "sell"
        order_ref = f"simulated:{binding.run_id}:{intent.bar_close_ms}:{intent.kind}"
        journal.append(
            DryRunActivity(
                seq=journal.next_seq(),
                strategy_instance_id=binding.strategy_instance_id,
                run_id=binding.run_id,
                recorded_at_ms=intent.bar_close_ms,
                bar_ref=f"{binding.symbol}@{intent.bar_close_ms}",
                intent=intent.kind.value,
                order_ref=order_ref,
                symbol=binding.symbol,
                side=side,
                quantity=float(binding.quantity),
                fill_price=float(intent.intended_price),
            )
        )
        logger.info(
            "Dry-run simulated fill",
            extra={
                "action": "dry_run_simulated_fill",
                "strategy_instance_id": binding.strategy_instance_id,
                "run_id": binding.run_id,
                "intent": intent.kind.value,
                "order_ref": order_ref,
            },
        )
