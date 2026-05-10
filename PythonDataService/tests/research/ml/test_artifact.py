from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    is_path_safe_id,
    read_chunk_rows,
    write_chunk_rows,
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


# ----- hash helpers --------------------------------------------------
from app.research.ml.artifact import (  # noqa: E402
    compute_prediction_set_hash,
    compute_rows_hash,
)


def _row(ts_ms: int, prediction: float) -> dict:
    return {"timestamp_ms": ts_ms, "symbol": "SPY", "prediction": prediction}


def test_rows_hash_deterministic_for_same_content() -> None:
    rows_a = [_row(1, 0.1), _row(2, 0.2)]
    rows_b = [_row(1, 0.1), _row(2, 0.2)]
    assert compute_rows_hash(rows_a) == compute_rows_hash(rows_b)


def test_rows_hash_changes_when_prediction_changes() -> None:
    a = compute_rows_hash([_row(1, 0.1)])
    b = compute_rows_hash([_row(1, 0.10000000001)])
    assert a != b


def test_rows_hash_sorts_by_timestamp() -> None:
    """Order on input does not matter; canonical order does."""
    sorted_input = [_row(1, 0.1), _row(2, 0.2)]
    reverse_input = [_row(2, 0.2), _row(1, 0.1)]
    assert compute_rows_hash(sorted_input) == compute_rows_hash(reverse_input)


def test_rows_hash_is_64_char_hex() -> None:
    h = compute_rows_hash([_row(1, 0.1)])
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_prediction_set_hash_excludes_self_field() -> None:
    """Setting the prediction_set_hash field to anything must not affect the
    computed hash — the field is removed from the dict before hashing.
    """
    base = _manifest_dict()
    base["prediction_set_hash"] = "a" * 64
    h_a = compute_prediction_set_hash(base)
    base["prediction_set_hash"] = "b" * 64
    h_b = compute_prediction_set_hash(base)
    assert h_a == h_b


def test_prediction_set_hash_changes_when_chunk_rows_hash_changes() -> None:
    base = _manifest_dict()
    h1 = compute_prediction_set_hash(base)
    base["chunks"][0]["rows_hash"] = "f" * 64
    h2 = compute_prediction_set_hash(base)
    assert h1 != h2


# ----- parquet I/O ---------------------------------------------------


def test_chunk_round_trip(tmp_path: Path) -> None:
    rows = [_row(1, 0.1), _row(2, 0.2), _row(3, 0.3)]
    path = tmp_path / "chunk.parquet"
    write_chunk_rows(path, rows, field_names=["prediction"])
    out = read_chunk_rows(path, field_names=["prediction"])
    assert out == rows


def test_chunk_rejects_unknown_field_in_rows(tmp_path: Path) -> None:
    rows = [{"timestamp_ms": 1, "symbol": "SPY", "prediction": 0.1, "extra": 1.0}]
    with pytest.raises(ValueError, match="extra column"):
        write_chunk_rows(tmp_path / "x.parquet", rows, field_names=["prediction"])


def test_chunk_rejects_missing_field_in_rows(tmp_path: Path) -> None:
    rows = [{"timestamp_ms": 1, "symbol": "SPY"}]
    with pytest.raises(ValueError, match="prediction"):
        write_chunk_rows(tmp_path / "x.parquet", rows, field_names=["prediction"])
