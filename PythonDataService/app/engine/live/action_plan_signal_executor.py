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
from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind


def stock_symbol_from_action_plan(action: object) -> str | None:
    """Return the single stock underlying declared by a live action plan.

    Action plans are operator-authored deploy identity. For the current
    stock-only runtime path, exactly one long stock leg is the traded ticker.
    Option, short, and multi-leg plans are not consumable by the stock runtime
    yet, so they deliberately return ``None``.
    """
    if not isinstance(action, dict):
        return None
    on_enter = action.get("on_enter")
    if not isinstance(on_enter, list) or not on_enter:
        return None

    symbols: set[str] = set()
    for leg in on_enter:
        if not isinstance(leg, dict):
            return None
        if leg.get("position") != "long":
            return None
        instrument = leg.get("instrument")
        if not isinstance(instrument, dict):
            return None
        if instrument.get("kind") != "stock":
            return None
        underlying = instrument.get("underlying")
        if not isinstance(underlying, str) or not underlying.strip():
            return None
        symbols.add(underlying.strip().upper())

    if len(symbols) != 1 or len(on_enter) != 1:
        return None
    return next(iter(symbols))


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
