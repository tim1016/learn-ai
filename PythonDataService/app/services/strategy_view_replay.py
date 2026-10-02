"""Replay a registered strategy and keep what each decision saw (#2639 D13).

A backtest is deterministic, so running the same registered program with the
same settings, engine and bars stages exactly the decisions the backtest
staged. Strategy Lab shows a saved run's own evaluations this way
(``engine_backtest_service.replay_engine_run``); the golden fixtures and tests
replay decisions under the decision-identity protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, pin_strategy_window
from app.engine.strategy.base import Strategy
from app.engine.strategy.signal_program import SignalDecision


def record_staged_decisions(strategy: Strategy, record: Callable[[TradeBar, SignalDecision], None]) -> None:
    """Hand ``record`` every (decision bar, decision) the strategy stages from now on.

    Wraps ``evaluate_signal_bar`` on the instance -- the method the Signal
    Session calls -- so nothing about the decision changes.
    """
    evaluate = strategy.evaluate_signal_bar  # type: ignore[attr-defined]

    def recording_evaluate(bar: TradeBar) -> SignalDecision:
        decision = evaluate(bar)
        record(bar, decision)
        return decision

    strategy.evaluate_signal_bar = recording_evaluate  # type: ignore[attr-defined]


def staged_decisions(
    strategy: Strategy, data_source: LeanMinuteDataReader, *, start: date, end: date
) -> list[tuple[TradeBar, SignalDecision]]:
    """Every (decision bar, decision) the strategy stages over ``start``..``end`` (ET dates), in order.

    The decision-identity protocol (``BacktestEngine.for_decision_identity``)
    the golden trace corpora replay with. Blocking: call it off the event loop
    (the engine's run gate refuses one).
    """
    staged: list[tuple[TradeBar, SignalDecision]] = []
    record_staged_decisions(strategy, lambda bar, decision: staged.append((bar, decision)))
    pin_strategy_window(strategy, start, end)
    BacktestEngine.for_decision_identity(data_source).run(strategy)
    return staged


__all__ = ["record_staged_decisions", "staged_decisions"]
