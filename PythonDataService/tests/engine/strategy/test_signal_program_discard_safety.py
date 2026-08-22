"""A DISCARDED evaluation must never leave position custody advanced.

``SignalSession`` stages a decision and only applies its effect when the
runner settles ``Settlement.COMMIT``; ``Settlement.DISCARD`` means the
candidate was never acted on. If a strategy mutates its own position state
inside ``evaluate_signal_bar`` rather than ``commit_signal_decision``, a
discarded candidate leaves the strategy believing it holds a position it
never opened -- and every later decision clock reads that corrupted flag.
The bot keeps running and simply decides wrongly, which is the hardest
shape of failure to notice in production.

This is driven off the registry rather than a hand-listed set of keys.
``sma_crossover`` was promoted with no coverage for this bug class at all
(an injected violation left all 294 of its tests passing), while its five
siblings were protected only incidentally, by tests written for other
reasons. A per-program copy would have kept that coverage a matter of who
remembered; deriving the program list means the next promotion is covered
the moment it is registered.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.execution.signal_intent_executor import SignalIntentExecutionContext
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntent
from app.engine.strategy.signal_program import EvaluationMode, Settlement, StageQuarantine

# Position custody: what the strategy believes it holds. Deliberately a
# closed list rather than a scan of every attribute -- indicator state and
# bar bookkeeping legitimately advance during evaluation, and only these
# describe an open or intended position.
_CUSTODY_ATTRIBUTES = ("_in_position", "_pending_entry", "_open_trade", "_bars_until_exit")


class _RecordingExecutor:
    """Absorbs committed intents so warmup can COMMIT without a broker."""

    def __init__(self) -> None:
        self.intents: list[SignalIntent] = []

    def execute(self, _context: SignalIntentExecutionContext, intent: SignalIntent) -> None:
        self.intents.append(intent)


def _sealed_programs() -> list[tuple[str, Any]]:
    return [(key, reg) for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


def _custody_snapshot(strategy: object) -> dict[str, object]:
    snap: dict[str, object] = {}
    for name in _CUSTODY_ATTRIBUTES:
        if hasattr(strategy, name):
            value = getattr(strategy, name)
            snap[name] = repr(value)
    return snap


def _bar(symbol: str, index: int, width_ms: int, close: str) -> TradeBar:
    start = index * width_ms
    price = Decimal(close)
    return TradeBar(
        symbol=symbol,
        start_ms=start,
        end_ms=start + width_ms,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=1_000,
    )


@pytest.mark.parametrize("key", [k for k, _ in _sealed_programs()])
def test_discarded_evaluation_leaves_position_custody_untouched(key: str) -> None:
    registration = _STRATEGY_REGISTRY[key]
    params = registration.param_schema()
    program = registration.signal_program_factory(params)
    strategy = program.strategy
    width = program.session.timeframe_ms

    # Warm through the session itself rather than BacktestEngine: these
    # strategies declare a fixed backtest window, so synthetic bars outside
    # it would be filtered out and the indicators would never advance --
    # setup that looks like warmup while doing nothing. Driving the session
    # directly warms real indicator state and exercises the same staged
    # advance/settle path the runner uses.
    session = program.session
    strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal("100000")))
    strategy.initialize()
    strategy.ctx.set_signal_intent_executor(_RecordingExecutor())

    # Snapshot while the strategy is still flat, then drive a long run of
    # evaluations that are ALL discarded. The invariant is that no sequence
    # of discarded candidates may advance custody -- so custody must still
    # equal this initial flat state at the end.
    #
    # Snapshotting after the warmup instead would be vacuous: a strategy
    # that wrongly sets its custody flag inside evaluate_signal_bar would
    # have corrupted it during warmup too, and the comparison would match
    # a corrupted value against itself. Both weaker formulations were tried
    # and confirmed to pass with the violation injected.
    before = _custody_snapshot(strategy)

    staged_any = False
    for offset in range(120):
        staged = session.advance(
            _bar(params.symbol, 1 + offset, width, str(100 + (offset % 11))),
            mode=EvaluationMode.DECIDE,
        )
        if isinstance(staged, StageQuarantine):
            pytest.skip(f"'{key}' quarantined a synthetic bar ({staged.reason})")
        staged_any = True
        session.settle(Settlement.DISCARD)

    assert staged_any, f"'{key}' never staged an evaluation; nothing was discarded"
    after = _custody_snapshot(strategy)

    assert after == before, (
        f"'{key}' advanced position custody during an evaluation that was DISCARDED: "
        f"{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }. "
        "Custody state belongs in commit_signal_decision, never evaluate_signal_bar."
    )
