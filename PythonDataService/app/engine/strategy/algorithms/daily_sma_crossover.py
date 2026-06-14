"""Shim module so the ``daily_sma_crossover`` registry key resolves at import.

VCR-0004 / Phase 2: the registry key is the runner's module path
(``app.engine.strategy.algorithms.{key}``). The daily and intraday SMA-cross
strategies share the same algorithm class (``SmaCrossoverAlgorithm``); only
the bar cadence and default windows differ, both expressed through the
registry's ``param_schema`` / ``build`` lambda. This shim re-exports the class
so ``import_module("...daily_sma_crossover")`` succeeds and the runner finds
``SmaCrossoverAlgorithm`` via ``registration.class_name``.
"""

from __future__ import annotations

from app.engine.strategy.algorithms.sma_crossover import SmaCrossoverAlgorithm

__all__ = ["SmaCrossoverAlgorithm"]
