"""Alpaca Clerk adapter for validated strategy decisions.

Strategy mathematics remains canonical in ``app.engine.strategy.algorithms``.
This module selects an admitted strategy, feeds it the broker-neutral minute
stream, and routes only its semantic ENTER/EXIT intents to the Clerk.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.broker.alpaca.clerk.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.models import EffectPurpose
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

if TYPE_CHECKING:
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


async def _deployment_validation_intents(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[SignalIntent]:
    kernel = DeploymentValidationDecisionKernel()
    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        decision = kernel.on_closed_bar(
            end_ms=bar.end_ms,
            open_price=bar.open,
            close_price=bar.close,
        )
        kind = _INTENT_BY_DEPLOYMENT_DECISION.get(decision)
        if kind is not None:
            yield SignalIntent(
                kind=kind,
                bar_close_ms=bar.end_ms,
                intended_price=bar.close,
            )


async def _ema_crossover_intents(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[SignalIntent]:
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
        for intent in intents:
            yield intent


async def _strategy_intents(
    binding: BrokerBotBinding,
    feed: MarketDataFeed,
) -> AsyncIterator[SignalIntent]:
    if binding.strategy_key == AlpacaPaperStrategyKey.DEPLOYMENT_VALIDATION:
        async for intent in _deployment_validation_intents(binding, feed):
            yield intent
        return
    if binding.strategy_key == AlpacaPaperStrategyKey.EMA_CROSSOVER_SIGNAL:
        async for intent in _ema_crossover_intents(binding, feed):
            yield intent
        return
    raise ValueError(f"unsupported Alpaca paper strategy: {binding.strategy_key}")


async def run_trade_bot(binding: BrokerBotBinding, feed: MarketDataFeed) -> None:
    """Execute one admitted strategy; the Clerk owns all execution truth."""
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise RuntimeError("AlpacaClerk is not installed; cannot execute trade-mode decisions.")
    async for intent in _strategy_intents(binding, feed):
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
            decision_id=f"{intent.bar_close_ms}:{intent.kind}",
            purpose=_EFFECT_PURPOSE_BY_INTENT[intent.kind],
            action_plan=binding.action_plan,
            quantity=binding.quantity,
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
