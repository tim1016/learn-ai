"""Compatibility import path for pre-migration SPY EMA run ledgers.

New work must register and deploy ``ema_crossover_signal``. This module keeps
the historical class and key so an existing run ledger can be resumed without
changing its strategy identity or indicator-state storage path.
"""

from app.engine.strategy.algorithms.ema_crossover_signal import (
    EmaCrossoverSignalAlgorithm,
    _OpenTrade,
    _PendingEntry,
)


class SpyEmaCrossoverAlgorithm(EmaCrossoverSignalAlgorithm):
    """Deprecated compatibility wrapper for the former SPY-named strategy."""

    STRATEGY_KEY = "spy_ema_crossover"


__all__ = ["SpyEmaCrossoverAlgorithm", "_OpenTrade", "_PendingEntry"]
