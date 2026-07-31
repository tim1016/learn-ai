"""Thin Alpaca adapter over the canonical deployment-validation decision kernel."""

from __future__ import annotations

import logging

from app.broker.alpaca.clerk.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.models import EffectPurpose
from app.engine.strategy.algorithms.deployment_validation import (
    DeploymentDecision,
    DeploymentValidationDecisionKernel,
)
from app.marketdata.feed import MarketDataFeed

logger = logging.getLogger(__name__)

_EFFECT_PURPOSE_BY_DECISION = {
    DeploymentDecision.ENTER: EffectPurpose.ENTER,
    DeploymentDecision.EXIT: EffectPurpose.EXIT,
}


async def run_trade_bot(binding, feed: MarketDataFeed) -> None:
    """Emit canonical ENTER/EXIT decisions; the Clerk owns all execution truth."""
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise RuntimeError("AlpacaClerk is not installed; cannot execute trade-mode decisions.")
    kernel = DeploymentValidationDecisionKernel()
    sid = binding.strategy_instance_id
    async for bar in feed.stream_bars(binding.symbol, use_rth=binding.use_rth):
        decision = kernel.on_closed_bar(
            end_ms=bar.end_ms, open_price=bar.open, close_price=bar.close
        )
        logger.info(
            "Trade bot decision",
            extra={
                "action": "bot_decision",
                "strategy_instance_id": sid,
                "decision": decision,
                "symbol": bar.symbol,
                "bar_start_ms": bar.start_ms,
                "bar_end_ms": bar.end_ms,
            },
        )
        if decision is DeploymentDecision.HOLD:
            continue
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=sid,
            run_id=binding.run_id,
            decision_id=f"{bar.end_ms}:{decision}",
            purpose=_EFFECT_PURPOSE_BY_DECISION[decision],
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
        logger.info(
            "Trade bot effect accepted",
            extra={
                "action": "bot_effect_accepted",
                "strategy_instance_id": sid,
                "purpose": decision,
                "effect_state": receipt.state.value,
                "order_refs": receipt.child_order_refs,
            },
        )
