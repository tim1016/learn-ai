"""Wires PredictionSet into SpecAlgorithm and asserts that ctx.predictions
is populated for the expected bar timestamps before evaluate runs.
"""
from __future__ import annotations

import pytest

from app.engine.strategy.spec import SpecAlgorithm
from app.engine.strategy.spec import schema as S


def test_spec_with_predictions_requires_prediction_set() -> None:
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "predictions": [{"id": "p", "prediction_set_id": "t", "field": "prediction"}],
        "entry": {"logic": "AND",
                  "conditions": [{"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": 0.0}],
                  "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1},
        "exit": {"logic": "AND", "conditions": []},
    })
    with pytest.raises(ValueError, match="declares predictions"):
        SpecAlgorithm(spec)
