from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.strategy.spec.schema import (
    PredictionComparison,
    PredictionRef,
    StrategySpec,
)


def _base_spec_dict(predictions=None, entry_conditions=None) -> dict:
    return {
        "schema_version": "1.0",
        "name": "t",
        "symbols": ["SPY"],
        "resolution": {"period_minutes": 15},
        "indicators": [],
        "predictions": predictions or [],
        "entry": {
            "logic": "AND",
            "conditions": entry_conditions or [],
            "size": {"kind": "SetHoldings", "fraction": 1.0},
            "pyramiding": 1,
        },
        "exit": {"logic": "AND", "conditions": []},
        "position": {"kind": "EQUITY_LONG"},
        "survival": [],
        "diagnostics": {"snapshot_at_entry": [], "snapshot_at_exit": []},
    }


def _pred_ref(id_: str = "rsi_pred", set_id: str = "pred_spy_v001") -> dict:
    return {"id": id_, "prediction_set_id": set_id, "field": "prediction"}


def _pred_cmp(prediction: str = "rsi_pred", op: str = ">", value: float = 0.0) -> dict:
    return {"kind": "PredictionComparison", "prediction": prediction, "op": op, "value": value}


# ----- standalone Pydantic models ------------------------------------
def test_prediction_ref_round_trip() -> None:
    p = PredictionRef.model_validate(_pred_ref())
    assert p.id == "rsi_pred"
    assert p.field == "prediction"


def test_prediction_ref_default_field_is_prediction() -> None:
    p = PredictionRef.model_validate({"id": "x", "prediction_set_id": "pred_v001"})
    assert p.field == "prediction"


def test_prediction_comparison_round_trip() -> None:
    p = PredictionComparison.model_validate(_pred_cmp())
    assert p.kind == "PredictionComparison"
    assert p.op == ">"


def test_prediction_ref_rejects_extras() -> None:
    bad = _pred_ref() | {"unexpected": True}
    with pytest.raises(ValidationError):
        PredictionRef.model_validate(bad)


# ----- spec round-trip with predictions block ------------------------
def test_spec_with_predictions_block_loads() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref()],
        entry_conditions=[_pred_cmp()],
    )
    spec = StrategySpec.model_validate(raw)
    assert len(spec.predictions) == 1
    assert spec.predictions[0].id == "rsi_pred"


def test_spec_with_no_predictions_still_loads() -> None:
    raw = _base_spec_dict()
    spec = StrategySpec.model_validate(raw)
    assert spec.predictions == []
