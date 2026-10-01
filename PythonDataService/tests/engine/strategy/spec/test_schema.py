from __future__ import annotations

from app.engine.strategy.spec.schema import (
    SUPPORTED_LIVE_RUNTIME_BAR_SOURCE,
    PredictionRef,
    StrategySpec,
)


def test_prediction_ref_lookup_defaults_to_exact_bar_close() -> None:
    """Default preserves backward compatibility: existing specs without an
    explicit lookup field continue to consume the prediction row at the
    bar's exact end_time_ms."""
    ref = PredictionRef.model_validate({"id": "p", "prediction_set_id": "x", "field": "prediction"})
    assert ref.lookup == "exact_bar_close"


def _minimal_spec_dict(**overrides) -> dict:
    base = {
        "schema_version": "1.0",
        "name": "synthetic",
        "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "indicators": [],
        "entry": {
            "logic": "AND",
            "conditions": [],
            "size": {"kind": "SetHoldings", "fraction": 1.0},
        },
        "exit": {"logic": "OR", "conditions": []},
    }
    base.update(overrides)
    return base


def test_strategy_spec_bar_source_defaults_to_live_runtime_source() -> None:
    spec = StrategySpec.model_validate(_minimal_spec_dict())

    assert spec.bar_source_descriptor == SUPPORTED_LIVE_RUNTIME_BAR_SOURCE
