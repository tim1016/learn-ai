"""Execution policies for instrument-free strategy decisions.

Strategies own the decision to enter or exit. An executor owns the concrete
instrument and execution surface, keeping policy strategies from reaching the
asset-selection boundary directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.engine.strategy.signal_intent import SignalIntent, SignalIntentKind


class SignalIntentExecutionContext(Protocol):
    """Minimal order surface available to a signal-intent executor."""

    def set_holdings(self, symbol: str, fraction: Decimal | float) -> None: ...

    def liquidate(self, symbol: str) -> None: ...


class SignalIntentExecutor(Protocol):
    """Route an asset-free ``SignalIntent`` through a concrete policy."""

    def execute(self, context: SignalIntentExecutionContext, intent: SignalIntent) -> None: ...


@dataclass(frozen=True)
class SignalSymbolExecutor:
    """Bind Engine Lab and parity signals explicitly to their sole stream."""

    symbol: str

    def execute(self, context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        if intent.kind is SignalIntentKind.ENTER:
            context.set_holdings(self.symbol, Decimal(1))
            return
        if intent.kind is SignalIntentKind.EXIT:
            context.liquidate(self.symbol)
            return
        raise ValueError(f"unsupported signal intent kind: {intent.kind!r}")
