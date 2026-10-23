"""The registered program and its seal read the same real indicators."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_program import EvaluationMode, EvaluationStage, Settlement
from tests._helpers.signal_program import LONG_PERIODS

PROGRAMS = [key for key, reg in _STRATEGY_REGISTRY.items() if reg.signal_program_factory is not None]


@pytest.mark.parametrize(("key", "overrides"), [(key, {}) for key in PROGRAMS] + list(LONG_PERIODS.items()))
def test_program_and_seal_converge_at_the_slowest_indicator(key: str, overrides: dict) -> None:
    registration = _STRATEGY_REGISTRY[key]
    params = registration.param_schema(**overrides)
    assert registration.signal_program_factory is not None
    assert registration.signal_program_contract is not None
    program = registration.signal_program_factory(params)
    program.strategy.ctx = StrategyContext(portfolio=Portfolio(initial_cash=Decimal(100_000)))
    program.strategy.initialize()
    series = registration.signal_program_contract.resolved_signals(params)
    required = max((item.bars_to_converge or 0 for item in series), default=0)
    assert program.session.convergence_bars_needed == required
    assert program.session.converged is (required == 0)
    for sample in range(1, required + 1):
        width = program.session.timeframe_ms
        end = 1_704_205_800_000 + sample * width
        price = Decimal(100) + Decimal(sample % 7)
        bar = TradeBar(symbol="SPY", start_ms=end - width, end_ms=end,
                       open=price, high=price + 1, low=price - 1, close=price, volume=1000)
        stage = program.session.advance(bar, mode=EvaluationMode.DECIDE, require_convergence=True)
        assert isinstance(stage, EvaluationStage)
        assert stage.trace.converged is (sample == required)
        assert stage.trace.spawning is (sample < required)
        if sample < required:
            assert stage.trace.evaluation_mode is EvaluationMode.OBSERVE_ONLY
            assert stage.settlement is Settlement.DISCARD
        else:
            assert stage.trace.evaluation_mode is EvaluationMode.DECIDE
            program.session.settle(Settlement.DISCARD)


def test_uninitialized_program_cannot_claim_convergence() -> None:
    reg = _STRATEGY_REGISTRY["sma_crossover"]
    assert reg.signal_program_factory is not None
    program = reg.signal_program_factory(reg.param_schema())
    with pytest.raises(RuntimeError, match="declare its indicators"):
        _ = program.session.converged
