from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.strategy.spec.schema import (
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


# ----- validators ----------------------------------------------------
def test_spec_rejects_undeclared_prediction_id() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="declared")],
        entry_conditions=[_pred_cmp(prediction="undeclared")],
    )
    with pytest.raises(ValidationError, match="undeclared prediction id"):
        StrategySpec.model_validate(raw)


def test_spec_rejects_duplicate_prediction_ref_ids() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="a"), _pred_ref(id_="a")],
    )
    with pytest.raises(ValidationError, match="duplicate prediction ref ids"):
        StrategySpec.model_validate(raw)


def test_spec_rejects_multiple_distinct_prediction_set_ids() -> None:
    raw = _base_spec_dict(
        predictions=[
            _pred_ref(id_="a", set_id="pred_set_one"),
            _pred_ref(id_="b", set_id="pred_set_two"),
        ],
    )
    with pytest.raises(ValidationError, match="at most one prediction_set_id"):
        StrategySpec.model_validate(raw)


def test_spec_accepts_multiple_refs_to_same_set() -> None:
    raw = _base_spec_dict(
        predictions=[
            {"id": "a", "prediction_set_id": "pred_set_one", "field": "prediction"},
            {"id": "b", "prediction_set_id": "pred_set_one", "field": "prediction"},
        ],
    )
    StrategySpec.model_validate(raw)


def test_spec_rejects_path_unsafe_prediction_set_id() -> None:
    raw = _base_spec_dict(
        predictions=[_pred_ref(id_="a", set_id="../evil")],
    )
    with pytest.raises(ValidationError, match="path-safe"):
        StrategySpec.model_validate(raw)
