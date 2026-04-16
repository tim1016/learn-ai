"""Strategy registry — maps strategy names to run functions and indicator definitions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from app.services.strategies import (
    ema_crossover_rsi,
    momentum_rsi_stochastic,
    rsi_mean_reversion,
    rsi_reversal,
    sma_crossover,
)
from app.services.strategies.common import StrategyResult

StrategyRunFn = Callable[[pd.DataFrame, dict], StrategyResult]


class StrategyDef:
    """Definition of a strategy with its run function and required indicators."""

    def __init__(
        self,
        run_fn: StrategyRunFn,
        indicators: list[dict[str, Any]],
    ):
        self.run_fn = run_fn
        self.indicators = indicators

    def get_indicator_entries(self, params: dict) -> list[dict[str, Any]]:
        """Build indicator entries from strategy params for pandas-ta computation."""
        entries = []
        for ind in self.indicators:
            length = params.get(ind["param_key"], ind["default"])
            entries.append({"name": ind["name"], "params": {"length": length}})
        return entries


STRATEGY_REGISTRY: dict[str, StrategyDef] = {
    "sma_crossover": StrategyDef(
        run_fn=sma_crossover.run,
        indicators=[
            {"name": "sma", "param_key": "ShortWindow", "default": 10},
            {"name": "sma", "param_key": "LongWindow", "default": 30},
        ],
    ),
    "rsi_mean_reversion": StrategyDef(
        run_fn=rsi_mean_reversion.run,
        indicators=[
            {"name": "rsi", "param_key": "Window", "default": 14},
        ],
    ),
    "momentum_rsi_stochastic": StrategyDef(
        run_fn=momentum_rsi_stochastic.run,
        indicators=[
            {"name": "sma", "param_key": "FastMa", "default": 20},
            {"name": "sma", "param_key": "SlowMa", "default": 50},
            {"name": "rsi", "param_key": "RsiLength", "default": 14},
            {"name": "stoch", "param_key": "StochK", "default": 14},
        ],
    ),
    "ema_crossover_rsi": StrategyDef(
        run_fn=ema_crossover_rsi.run,
        indicators=[
            {"name": "ema", "param_key": "fast_ema_period", "default": 5},
            {"name": "ema", "param_key": "slow_ema_period", "default": 10},
            {"name": "rsi", "param_key": "rsi_period", "default": 14},
            {"name": "adx", "param_key": "adx_period", "default": 14},
        ],
    ),
    "rsi_reversal": StrategyDef(
        run_fn=rsi_reversal.run,
        indicators=[
            {"name": "rsi", "param_key": "Window", "default": 14},
        ],
    ),
}


def get_strategy(name: str) -> StrategyDef:
    """Look up a strategy by name."""
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> list[str]:
    """Return all registered strategy names."""
    return list(STRATEGY_REGISTRY.keys())
