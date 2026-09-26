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

The re-exports stay lazy for the same reason ``app.engine.strategy``'s do
(#2450): ``evaluator`` imports ``app.engine.strategy.base``, which imports
declared Signal Program sources, so an eager import here would run before
the build-proof source anchor and make the anchor refuse. Collecting this
package's own test root (``spec/tests``) imports the package chain before
any conftest can prime the anchor; laziness keeps that order safe.
"""

import importlib

__all__ = ["DecisionColumnSpec", "SpecAlgorithm", "StrategySpec", "load_spec_from_path"]

_RE_EXPORTS = {
    "SpecAlgorithm": "app.engine.strategy.spec.evaluator",
    "StrategySpec": "app.engine.strategy.spec.schema",
    "DecisionColumnSpec": "app.engine.strategy.spec.schema",
    "load_spec_from_path": "app.engine.strategy.spec.schema",
}


def __getattr__(name: str):
    module_name = _RE_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
