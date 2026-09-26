"""Declarative strategy-spec layer.

Phase 1 scope (equity-only, single-symbol, no survival actions, no options
materialization): a JSON spec describes indicators, entry/exit conditions,
and position sizing; ``SpecAlgorithm`` consumes a validated ``StrategySpec``
and runs through the existing ``BacktestEngine`` to produce trades parity-
matched against the hand-coded reference algorithms.

Phase 1 acceptance gate: spec versions of SPY EMA crossover, SMA crossover,
and RSI mean reversion produce identical trade logs to their hand-coded
twins (``SpyEmaCrossoverAlgorithm``, ``SmaCrossoverAlgorithm``,
``RsiMeanReversionAlgorithm``) on the same input data.

The hand-coded strategies remain math-authority for their three pinned
algorithms (per ``docs/math-sources-of-truth.md``); ``SpecAlgorithm`` is a
parity-pinned secondary implementation. Same pattern as
``test_bs_cross_engine_parity.py``.
"""

from app.engine.strategy.spec.evaluator import SpecAlgorithm
from app.engine.strategy.spec.schema import (
    DecisionColumnSpec,
    StrategySpec,
    load_spec_from_path,
)

__all__ = ["DecisionColumnSpec", "SpecAlgorithm", "StrategySpec", "load_spec_from_path"]
