"""Recency Chart strategy eligibility (design spec D1).

Eligibility is derived structurally from each strategy's param schema
rather than hand-flagged per strategy, so a newly registered strategy is
excluded by default until its schema is proven numeric-only (fail closed)
— a manually maintained allowlist could silently drift as strategies are
added.
"""

from __future__ import annotations

from typing import Literal

from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.recency.eligibility import is_recency_supported


def test_numeric_only_strategies_are_supported() -> None:
    for key in ("ema_crossover_signal", "sma_crossover", "rsi_mean_reversion"):
        schema = _STRATEGY_REGISTRY[key].param_schema
        assert is_recency_supported(schema) is True, key


def test_a_categorical_parameter_excludes_a_strategy() -> None:
    """One non-numeric knob is enough to exclude, however numeric the rest.

    This used to be pinned against ``spy_ema_crossover_options``, whose
    ``spread_type`` / ``pricing_mode`` sat beside 25 numeric parameters. That
    registration is gone, but the rule it demonstrated is the load-bearing
    one: the sweep needs a numeric range for every knob, so the gate excludes
    on *any* categorical rather than on some ratio of them. A synthetic schema
    states that without depending on which strategies happen to be registered
    today -- and the rule is not hypothetical: an ``ema_crossover_signal``
    revision briefly added a categorical ``gap_mode`` and silently dropped
    itself out of recency until this gate caught it.
    """

    class _MostlyNumericParams(StrategyParamsBase):
        symbol: str = "SPY"
        fast_period: int = 5
        slow_period: int = 10
        threshold: float = 0.2
        mode: Literal["a", "b"] = "a"

    assert is_recency_supported(_MostlyNumericParams) is False


def test_deployment_validation_is_excluded() -> None:
    schema = _STRATEGY_REGISTRY["deployment_validation"].param_schema
    assert is_recency_supported(schema) is False


def test_symbol_field_is_ignored_for_the_check() -> None:
    schema = _STRATEGY_REGISTRY["sma_crossover"].param_schema
    assert "symbol" in schema.model_fields
    assert is_recency_supported(schema) is True
