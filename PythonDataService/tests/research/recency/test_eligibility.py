"""Recency Chart strategy eligibility (design spec D1).

Eligibility is derived structurally from each strategy's param schema
rather than hand-flagged per strategy, so a newly registered strategy is
excluded by default until its schema is proven numeric-only (fail closed)
— a manually maintained allowlist could silently drift as strategies are
added.
"""

from __future__ import annotations

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.recency.eligibility import is_recency_supported


def test_numeric_only_strategies_are_supported() -> None:
    for key in ("ema_crossover_signal", "sma_crossover", "rsi_mean_reversion"):
        schema = _STRATEGY_REGISTRY[key].param_schema
        assert is_recency_supported(schema) is True, key


def test_options_spread_strategy_is_excluded() -> None:
    schema = _STRATEGY_REGISTRY["spy_ema_crossover_options"].param_schema
    assert is_recency_supported(schema) is False


def test_deployment_validation_is_excluded() -> None:
    schema = _STRATEGY_REGISTRY["deployment_validation"].param_schema
    assert is_recency_supported(schema) is False


def test_symbol_field_is_ignored_for_the_check() -> None:
    schema = _STRATEGY_REGISTRY["sma_crossover"].param_schema
    assert "symbol" in schema.model_fields
    assert is_recency_supported(schema) is True
