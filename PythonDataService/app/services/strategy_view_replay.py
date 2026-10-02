"""Replay a registered strategy and keep what each decision saw (#2639 D13).

A backtest is a deterministic replay, so running the same registered program
with the same settings over the same bars stages exactly the decisions the
backtest staged. This is how Strategy Lab shows a run's own evaluations
without storing them with the run, and how the golden fixtures and tests
replay decisions.
"""

from __future__ import annotations

from datetime import date

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, pin_strategy_window
from app.engine.strategy.base import Strategy
from app.engine.strategy.signal_program import SignalDecision


def staged_decisions(
    strategy: Strategy, data_source: LeanMinuteDataReader, *, start: date, end: date
) -> list[tuple[TradeBar, SignalDecision]]:
    """Every (decision bar, decision) the strategy stages over ``start``..``end`` (ET dates), in order.

    The decision-identity protocol (``BacktestEngine.for_decision_identity``)
    the golden trace corpora replay with. Recording wraps
    ``evaluate_signal_bar`` on the instance -- the method the Signal Session
    calls -- so nothing about the decision changes. Blocking: call it off the
    event loop (the engine's run gate refuses one).
    """
    staged: list[tuple[TradeBar, SignalDecision]] = []
    evaluate = strategy.evaluate_signal_bar  # type: ignore[attr-defined]

    def recording_evaluate(bar: TradeBar) -> SignalDecision:
        decision = evaluate(bar)
        staged.append((bar, decision))
        return decision

    strategy.evaluate_signal_bar = recording_evaluate  # type: ignore[attr-defined]
    pin_strategy_window(strategy, start, end)
    BacktestEngine.for_decision_identity(data_source).run(strategy)
    return staged


__all__ = ["staged_decisions"]
