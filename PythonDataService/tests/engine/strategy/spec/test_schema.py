from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine.strategy.spec.schema import PredictionRef


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
