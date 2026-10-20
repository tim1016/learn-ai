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
@pytest.mark.parametrize(
    ("value", "safe"),
    [
        pytest.param("pred_spy_v001", True, id="accepts-underscore"),
        pytest.param("abc-123", True, id="accepts-hyphen"),
        pytest.param("pred.v2", True, id="accepts-dot"),
        pytest.param("x", True, id="accepts-single-char"),
        pytest.param("", False, id="rejects-empty"),
        pytest.param("..", False, id="rejects-dotdot"),
        pytest.param("../foo", False, id="rejects-parent-traversal"),
        pytest.param("a/b", False, id="rejects-forward-slash"),
        pytest.param("a\\b", False, id="rejects-backslash"),
        pytest.param("with space", False, id="rejects-space"),
        pytest.param(".hidden", False, id="rejects-leading-dot"),
    ],
)
def test_path_safe_id_accepts_alnum_and_separators_rejects_traversal(value: str, safe: bool) -> None:
    assert is_path_safe_id(value) is safe


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


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        pytest.param("extra_field", "no", None, id="rejects_extras"),
        # start_ms equals trained_through_ms
        pytest.param(
            "start_ms",
            1714521600000,
            "start_ms must be > trained_through_ms",
            id="rejects_start_at_or_before_trained_through",
        ),
    ],
)
def test_chunk_ref_rejects_invalid(field: str, value: object, match: str | None) -> None:
    bad = _chunk_dict() | {field: value}
    with pytest.raises(ValidationError, match=match):
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


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        pytest.param("parquet_file_hash", "deadbeef", None, id="rejects_extras"),
        pytest.param("warmup_policy", "forward_fill", None, id="rejects_unknown_warmup_policy"),
        pytest.param("prediction_set_id", "../evil", "path-safe", id="rejects_path_unsafe_id"),
    ],
)
def test_manifest_rejects_invalid(field: str, value: object, match: str | None) -> None:
    bad = _manifest_dict() | {field: value}
    with pytest.raises(ValidationError, match=match):
        PredictionSetManifest.model_validate(bad)


# ----- hash helpers --------------------------------------------------
from app.research.ml.artifact import (  # noqa: E402
    compute_prediction_set_hash,
    compute_rows_hash,
)


def _row(ts_ms: int, prediction: float) -> dict:
    return {"timestamp_ms": ts_ms, "symbol": "SPY", "prediction": prediction}


def test_rows_hash_properties() -> None:
    rows_a = [_row(1, 0.1), _row(2, 0.2)]
    rows_b = [_row(1, 0.1), _row(2, 0.2)]
    assert compute_rows_hash(rows_a) == compute_rows_hash(rows_b), "deterministic for same content"

    a = compute_rows_hash([_row(1, 0.1)])
    b = compute_rows_hash([_row(1, 0.10000000001)])
    assert a != b, "changes when prediction changes"

    # Order on input does not matter; canonical order does.
    sorted_input = [_row(1, 0.1), _row(2, 0.2)]
    reverse_input = [_row(2, 0.2), _row(1, 0.1)]
    assert compute_rows_hash(sorted_input) == compute_rows_hash(reverse_input), "sorts by timestamp"

    h = compute_rows_hash([_row(1, 0.1)])
    assert len(h) == 64, "is 64 characters long"
    assert all(c in "0123456789abcdef" for c in h), "is lowercase hex"


def test_prediction_set_hash_properties() -> None:
    # Setting the prediction_set_hash field to anything must not affect the
    # computed hash — the field is removed from the dict before hashing.
    base = _manifest_dict()
    base["prediction_set_hash"] = "a" * 64
    h_a = compute_prediction_set_hash(base)
    base["prediction_set_hash"] = "b" * 64
    h_b = compute_prediction_set_hash(base)
    assert h_a == h_b, "excludes self field"

    base = _manifest_dict()
    h1 = compute_prediction_set_hash(base)
    base["chunks"][0]["rows_hash"] = "f" * 64
    h2 = compute_prediction_set_hash(base)
    assert h1 != h2, "changes when chunk rows_hash changes"


# ----- parquet I/O ---------------------------------------------------


def test_chunk_round_trip(tmp_path: Path) -> None:
    rows = [_row(1, 0.1), _row(2, 0.2), _row(3, 0.3)]
    path = tmp_path / "chunk.parquet"
    write_chunk_rows(path, rows, field_names=["prediction"])
    out = read_chunk_rows(path, field_names=["prediction"])
    assert out == rows


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        pytest.param((("prediction", 0.1), ("extra", 1.0)), "extra column", id="rejects_unknown_field_in_rows"),
        pytest.param((), "prediction", id="rejects_missing_field_in_rows"),
    ],
)
def test_chunk_rejects_invalid_rows(tmp_path: Path, fields: tuple, match: str) -> None:
    rows = [{"timestamp_ms": 1, "symbol": "SPY"} | dict(fields)]
    with pytest.raises(ValueError, match=match):
        write_chunk_rows(tmp_path / "x.parquet", rows, field_names=["prediction"])
