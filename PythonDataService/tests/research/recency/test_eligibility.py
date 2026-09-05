"""Recency Chart eligibility delegates to the shared sweep predicate (PRD #1926)."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from app.engine.strategy.params import StrategyParamsBase
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.recency.eligibility import is_recency_supported


def test_numeric_only_production_strategies_are_supported() -> None:
    for key in ("ema_crossover_signal", "sma_crossover", "rsi_mean_reversion"):
        assert is_recency_supported(_STRATEGY_REGISTRY[key]) is True, key


def test_a_categorical_public_parameter_excludes_a_strategy() -> None:
    """One non-numeric knob is enough to exclude, however numeric the rest.

    The rule is not hypothetical: an ``ema_crossover_signal`` revision briefly
    added a categorical ``gap_mode`` and silently dropped itself out of
    recency until this gate caught it.
    """

    class _MostlyNumericParams(StrategyParamsBase):
        symbol: str = "SPY"
        fast_period: int = 5
        slow_period: int = 10
        threshold: float = 0.2
        mode: Literal["a", "b"] = "a"

    registration = replace(_STRATEGY_REGISTRY["sma_crossover"], param_schema=_MostlyNumericParams)
    assert is_recency_supported(registration) is False


def test_deployment_validation_is_excluded_by_category() -> None:
    assert is_recency_supported(_STRATEGY_REGISTRY["deployment_validation"]) is False


def test_symbol_field_is_ignored_for_the_check() -> None:
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    assert "symbol" in registration.param_schema.model_fields
    assert is_recency_supported(registration) is True
