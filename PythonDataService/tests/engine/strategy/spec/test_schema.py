from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def test_prediction_ref_lookup_accepts_next_after_bar_close() -> None:
    ref = PredictionRef.model_validate(
        {
            "id": "p",
            "prediction_set_id": "x",
            "field": "prediction",
            "lookup": "next_after_bar_close",
        }
    )
    assert ref.lookup == "next_after_bar_close"


def test_prediction_ref_lookup_rejects_unknown_value() -> None:
    """Closed Literal — any other string is a validation error at the wire
    boundary. Catches typos like "next_after" or "next_bar_close" before
    they reach the evaluator."""
    with pytest.raises(ValidationError):
        PredictionRef.model_validate(
            {
                "id": "p",
                "prediction_set_id": "x",
                "field": "prediction",
                "lookup": "lookahead",
            }
        )


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


def test_strategy_spec_rejects_client_id() -> None:
    """IBKR client IDs are runtime infrastructure, not strategy spec state."""
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_minimal_spec_dict(client_id=-1))
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_minimal_spec_dict(client_id=2**31))
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_minimal_spec_dict(client_id=11))

    assert StrategySpec.model_validate(_minimal_spec_dict()).model_extra is None


def test_strategy_spec_bar_source_defaults_to_live_runtime_source() -> None:
    spec = StrategySpec.model_validate(_minimal_spec_dict())

    assert spec.bar_source_descriptor == SUPPORTED_LIVE_RUNTIME_BAR_SOURCE


def test_strategy_spec_bar_source_rejects_unknown_descriptor() -> None:
    with pytest.raises(ValidationError):
        StrategySpec.model_validate(_minimal_spec_dict(bar_source_descriptor="paper-ish"))
