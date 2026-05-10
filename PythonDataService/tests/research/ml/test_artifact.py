from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    is_path_safe_id,
)


# ----- path-safe id ----------------------------------------------------
@pytest.mark.parametrize("good", ["pred_spy_v001", "abc-123", "pred.v2", "x"])
def test_path_safe_id_accepts_alnum_and_separators(good: str) -> None:
    assert is_path_safe_id(good)


@pytest.mark.parametrize("bad", ["", "..", "../foo", "a/b", "a\\b", "with space", ".hidden"])
def test_path_safe_id_rejects_traversal_and_separators(bad: str) -> None:
    assert not is_path_safe_id(bad)


# ----- ChunkRef -------------------------------------------------------
def _chunk_dict() -> dict:
    return {
        "trained_through_ms": 1714521600000,
        "start_ms": 1714608000000,
        "end_ms": 1717199999000,
        "row_count": 173,
        "rows_hash": "0" * 64,
    }


def test_chunk_ref_round_trip() -> None:
    c = ChunkRef.model_validate(_chunk_dict())
    assert c.trained_through_ms == 1714521600000
    assert c.row_count == 173


def test_chunk_ref_rejects_extras() -> None:
    bad = _chunk_dict() | {"extra_field": "no"}
    with pytest.raises(ValidationError):
        ChunkRef.model_validate(bad)


def test_chunk_ref_rejects_start_at_or_before_trained_through() -> None:
    bad = _chunk_dict() | {"start_ms": 1714521600000}  # equals trained_through_ms
    with pytest.raises(ValidationError, match="start_ms must be > trained_through_ms"):
        ChunkRef.model_validate(bad)


# ----- PredictionSetManifest -----------------------------------------
def _manifest_dict() -> dict:
    return {
        "schema_version": "1.0",
        "prediction_set_id": "pred_spy_rsi_rule_v001",
        "symbol": "SPY",
        "resolution_minutes": 15,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {
            "kind": "deterministic_rule",
            "rule_id": "rsi_14_centered",
            "rule_version": "1.0",
        },
        "chunks": [_chunk_dict()],
        "prediction_set_hash": "0" * 64,
    }


def test_manifest_round_trip() -> None:
    m = PredictionSetManifest.model_validate(_manifest_dict())
    assert m.prediction_set_id == "pred_spy_rsi_rule_v001"
    assert m.warmup_policy == "neutral_zero_until_feature_ready"
    assert len(m.chunks) == 1


def test_manifest_rejects_extras() -> None:
    bad = _manifest_dict() | {"parquet_file_hash": "deadbeef"}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_manifest_rejects_unknown_warmup_policy() -> None:
    bad = _manifest_dict() | {"warmup_policy": "forward_fill"}
    with pytest.raises(ValidationError):
        PredictionSetManifest.model_validate(bad)


def test_manifest_rejects_path_unsafe_id() -> None:
    bad = _manifest_dict() | {"prediction_set_id": "../evil"}
    with pytest.raises(ValidationError, match="path-safe"):
        PredictionSetManifest.model_validate(bad)
