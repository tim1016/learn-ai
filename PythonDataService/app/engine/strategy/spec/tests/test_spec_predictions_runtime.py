"""Wires PredictionSet into SpecAlgorithm and asserts that ctx.predictions
is populated for the expected bar timestamps before evaluate runs.
"""
from __future__ import annotations

import pytest

from app.engine.strategy.spec import SpecAlgorithm
from app.engine.strategy.spec import schema as S
from app.research.ml.artifact import ChunkRef, GeneratorMeta, PredictionSetManifest
from app.research.ml.loader import PredictionSet


def _make_pset(timestamps_ms: list[int], values: list[float]) -> PredictionSet:
    """Build a PredictionSet directly in memory (skip filesystem)."""
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="SPY",
        resolution_minutes=15,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=GeneratorMeta(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[ChunkRef(
            trained_through_ms=timestamps_ms[0] - 1,
            start_ms=timestamps_ms[0],
            end_ms=timestamps_ms[-1],
            row_count=len(timestamps_ms),
            rows_hash="0" * 64,
        )],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": v} for ts, v in zip(timestamps_ms, values, strict=True)}
    return PredictionSet(manifest=manifest, index=index)


def test_spec_algorithm_accepts_optional_prediction_set() -> None:
    """Existing prediction-free constructor signature still works."""
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "entry": {"logic": "AND", "conditions": [],
                  "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1},
        "exit": {"logic": "AND", "conditions": []},
    })
    algo = SpecAlgorithm(spec)
    assert algo._prediction_set is None


def test_spec_algorithm_accepts_explicit_prediction_set() -> None:
    spec = S.StrategySpec.model_validate({
        "schema_version": "1.0", "name": "t", "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "predictions": [{"id": "p", "prediction_set_id": "t", "field": "prediction"}],
        "entry": {
            "logic": "AND",
            "conditions": [{"kind": "PredictionComparison", "prediction": "p", "op": ">", "value": 0.0}],
            "size": {"kind": "SetHoldings", "fraction": 1.0}, "pyramiding": 1,
        },
        "exit": {"logic": "AND", "conditions": []},
    })
    pset = _make_pset([1_000, 2_000], [0.5, -0.5])
    algo = SpecAlgorithm(spec, prediction_set=pset)
    assert algo._prediction_set is pset


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
