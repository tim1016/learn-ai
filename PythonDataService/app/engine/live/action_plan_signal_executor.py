"""Stock Action Plan executor for instrument-free strategy decisions.

This is intentionally a small execution-boundary adapter. It owns the
traded-symbol decision; strategies receive no symbol or sizing information
from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.engine.execution.signal_intent_executor import (
    SignalIntentExecutionContext,
    SignalIntentExecutor,
)
from app.engine.live.config import stock_symbol_from_action_plan
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind


@dataclass(frozen=True)
class StockActionPlanSignalExecutor(SignalIntentExecutor):
    """Apply long-only enter/exit intents to the Action Plan's stock leg."""

    traded_symbol: str

    @classmethod
    def from_action_plan(cls, action_plan: object) -> StockActionPlanSignalExecutor:
        symbol = stock_symbol_from_action_plan(action_plan)
        if symbol is None:
            raise ValueError(
                "Signal-only stock execution requires exactly one long stock entry leg in live_config.action"
            )
        return cls(traded_symbol=symbol.upper())

    def execute(self, context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        """Route a decision without exposing the selected asset to the strategy."""
        if intent.kind is SignalIntentKind.ENTER:
            context.set_holdings(self.traded_symbol, Decimal(1))
            return
        if intent.kind is SignalIntentKind.EXIT:
            context.liquidate(self.traded_symbol)
            return
        raise ValueError(f"unsupported signal intent kind: {intent.kind!r}")
