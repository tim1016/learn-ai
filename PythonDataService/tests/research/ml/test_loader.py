from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.strategy.spec.schema import (
    EntryBlock,
    EquityLongPosition,
    ExitBlock,
    Resolution,
    SetHoldings,
    StrategySpec,
)
from app.research.ml.artifact import (
    ChunkRef,
    DeterministicRuleGenerator,
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.ml.loader import PredictionSet


def _row(ts: int, p: float) -> dict:
    return {"timestamp_ms": ts, "symbol": "SPY", "prediction": p}


def _write_artifact(
    root: Path,
    *,
    set_id: str = "pred_spy_test_v001",
    chunks: list[tuple[int, list[dict]]] | None = None,
) -> Path:
    """Materialize a complete prediction-set artifact under ``root/<set_id>/``."""
    if chunks is None:
        chunks = [(0, [_row(900_000, 0.0), _row(960_000, 0.5), _row(1_020_000, -0.5)])]

    set_dir = root / set_id
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)

    manifest_chunks = []
    for trained_through_ms, rows in chunks:
        path = chunk_dir / f"{trained_through_ms}.parquet"
        write_chunk_rows(path, rows, field_names=["prediction"])
        manifest_chunks.append(
            {
                "trained_through_ms": trained_through_ms,
                "start_ms": rows[0]["timestamp_ms"],
                "end_ms": rows[-1]["timestamp_ms"],
                "row_count": len(rows),
                "rows_hash": compute_rows_hash(rows),
            }
        )

    manifest_dict = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": "SPY",
        "resolution_minutes": 1,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": "test", "rule_version": "1.0"},
        "chunks": manifest_chunks,
        "prediction_set_hash": "0" * 64,
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)
    PredictionSetManifest.model_validate(manifest_dict)
    (set_dir / "manifest.json").write_text(json.dumps(manifest_dict))
    return set_dir


# ----- happy path ----------------------------------------------------
def test_load_succeeds_on_valid_artifact(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    pset = PredictionSet.load(set_dir)
    assert pset.manifest.prediction_set_id == "pred_spy_test_v001"
    assert set(pset.index.keys()) == {900_000, 960_000, 1_020_000}
    assert pset.index[960_000] == {"prediction": 0.5}


# ----- intrinsic validation ------------------------------------------
def test_load_fails_when_manifest_missing(tmp_path: Path) -> None:
    set_dir = tmp_path / "empty"
    set_dir.mkdir()
    with pytest.raises(FileNotFoundError, match=r"manifest\.json"):
        PredictionSet.load(set_dir)


def test_load_fails_when_rows_hash_mismatched(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["chunks"][0]["rows_hash"] = "f" * 64
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="rows_hash"):
        PredictionSet.load(set_dir)


def test_load_fails_when_prediction_set_hash_mismatched(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["prediction_set_hash"] = "f" * 64
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="prediction_set_hash"):
        PredictionSet.load(set_dir)


def test_load_fails_on_leakage_chunk_start_at_or_before_trained_through(tmp_path: Path) -> None:
    """A chunk with start_ms == trained_through_ms is rejected at the
    manifest layer (ChunkRef invariant) before the loader runs."""
    set_dir = tmp_path / "pred_leak"
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)
    rows = [_row(100, 0.0), _row(200, 0.0)]
    write_chunk_rows(chunk_dir / "100.parquet", rows, field_names=["prediction"])
    raw = {
        "schema_version": "1.0",
        "prediction_set_id": "pred_leak",
        "symbol": "SPY",
        "resolution_minutes": 1,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": "x", "rule_version": "1.0"},
        "chunks": [
            {
                "trained_through_ms": 100,
                "start_ms": 100,
                "end_ms": 200,
                "row_count": 2,
                "rows_hash": compute_rows_hash(rows),
            }
        ],
        "prediction_set_hash": "0" * 64,
    }
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    (set_dir / "manifest.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="start_ms must be > trained_through_ms"):
        PredictionSet.load(set_dir)


def test_load_fails_on_row_outside_chunk_window(tmp_path: Path) -> None:
    chunks = [(0, [_row(900_000, 0.0), _row(2_000_000, 0.0)])]
    set_dir = _write_artifact(tmp_path, chunks=chunks)
    manifest_path = set_dir / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["chunks"][0]["end_ms"] = 1_000_000
    raw["prediction_set_hash"] = compute_prediction_set_hash(raw)
    manifest_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="outside chunk window"):
        PredictionSet.load(set_dir)


@pytest.mark.parametrize(
    "chunk_rows",
    [
        pytest.param(((0, ((1000, 0.0), (1000, 0.5))),), id="within_chunk"),
        pytest.param(((0, ((1000, 0.0),)), (500, ((1000, 0.5),))), id="across_chunks"),
    ],
)
def test_load_fails_on_duplicate_timestamp(tmp_path: Path, chunk_rows: tuple) -> None:
    chunks = [(trained_through_ms, [_row(ts, p) for ts, p in rows]) for trained_through_ms, rows in chunk_rows]
    set_dir = _write_artifact(tmp_path, chunks=chunks)
    with pytest.raises(ValueError, match="duplicate timestamp"):
        PredictionSet.load(set_dir)


# ----- spec-pairing --------------------------------------------------
def _spec_for(symbol: str, period_minutes: int) -> StrategySpec:
    return StrategySpec(
        schema_version="1.0",
        name="t",
        symbols=[symbol],
        resolution=Resolution(period_minutes=period_minutes),
        entry=EntryBlock(logic="AND", conditions=[], size=SetHoldings(kind="SetHoldings", fraction=1.0)),
        exit=ExitBlock(logic="AND", conditions=[]),
        position=EquityLongPosition(kind="EQUITY_LONG"),
    )


def test_assert_pairs_with_spec_succeeds_on_match(tmp_path: Path) -> None:
    set_dir = _write_artifact(tmp_path)
    pset = PredictionSet.load(set_dir)
    pset.assert_pairs_with(_spec_for("SPY", 1))


@pytest.mark.parametrize(
    ("symbol", "period_minutes", "match"),
    [
        pytest.param("QQQ", 1, "symbol mismatch", id="symbol_mismatch"),
        pytest.param("SPY", 5, "resolution mismatch", id="resolution_mismatch"),
    ],
)
def test_assert_pairs_with_spec_fails_on_mismatch(tmp_path: Path, symbol: str, period_minutes: int, match: str) -> None:
    set_dir = _write_artifact(tmp_path)
    pset = PredictionSet.load(set_dir)
    with pytest.raises(ValueError, match=match):
        pset.assert_pairs_with(_spec_for(symbol, period_minutes))


# ----- next_after + PredictionLookupError ----------------------------
def _pset_with_keys(keys_ms: list[int]) -> PredictionSet:
    manifest = PredictionSetManifest(
        schema_version="1.0",
        prediction_set_id="t",
        symbol="AAPL",
        resolution_minutes=1440,
        field_names=["prediction"],
        warmup_policy="neutral_zero_until_feature_ready",
        generator=DeterministicRuleGenerator(kind="deterministic_rule", rule_id="x", rule_version="1.0"),
        chunks=[
            ChunkRef(
                trained_through_ms=min(keys_ms) - 1 if keys_ms else 0,
                start_ms=min(keys_ms) if keys_ms else 0,
                end_ms=max(keys_ms) if keys_ms else 0,
                row_count=len(keys_ms),
                rows_hash="0" * 64,
            )
        ],
        prediction_set_hash="0" * 64,
    )
    index = {ts: {"prediction": float(ts)} for ts in keys_ms}
    return PredictionSet(manifest=manifest, index=index)


# The defining invariant: strict `>`, not `>=`. A query at a key that exists
# returns the NEXT row, not the queried one. Index dict insertion order may be
# unsorted; the sorted-key cache must produce correct ordering regardless of
# dict iteration order. ``expected`` is the next row's prediction, None when no
# later key exists.
@pytest.mark.parametrize(
    ("keys_ms", "query_ms", "expected"),
    [
        pytest.param((100, 200, 300, 400), 100, 200.0, id="returns_smallest_strictly_greater_key_at_key_100"),
        pytest.param((100, 200, 300, 400), 150, 200.0, id="returns_smallest_strictly_greater_key_between_keys_150"),
        pytest.param((100, 200, 300, 400), 199, 200.0, id="returns_smallest_strictly_greater_key_just_below_199"),
        pytest.param((100, 200, 300, 400), 200, 300.0, id="strict_skips_queried_key_200_itself"),
        pytest.param((100, 200, 300), 300, None, id="returns_none_at_last_key"),
        pytest.param((100, 200, 300), 999, None, id="returns_none_past_last_key"),
        pytest.param((300, 100, 400, 200), 100, 200.0, id="unsorted_input_keys_at_key_100"),
        pytest.param((300, 100, 400, 200), 250, 300.0, id="unsorted_input_keys_between_keys_250"),
        pytest.param((300, 100, 400, 200), 400, None, id="unsorted_input_keys_returns_none_at_last_key"),
    ],
)
def test_next_after(keys_ms: tuple[int, ...], query_ms: int, expected: float | None) -> None:
    pset = _pset_with_keys(list(keys_ms))
    expected_row = None if expected is None else {"prediction": expected}
    assert pset.next_after(query_ms) == expected_row
