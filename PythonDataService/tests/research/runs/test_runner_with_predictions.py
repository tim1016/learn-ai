"""Runner-level tests for v0.5 ML predictions-as-data integration.

A spec that declares ``predictions`` triggers the runner to:

  * load the prediction set from the artifact root (override
    via ``LEARN_AI_PREDICTION_ARTIFACTS_ROOT``);
  * call ``PredictionSet.assert_pairs_with(spec)``;
  * run bar-clock coverage (the loaded set must include every
    ``end_time`` the engine's consolidator will fire);
  * thread ``manifest.prediction_set_hash`` into the ``RunLedger``;
  * pass the loaded set into ``SpecAlgorithm(spec, prediction_set=...)``.

Failures at any of those steps must produce a ``failed`` ledger via
``_failed`` rather than letting an exception propagate. Prediction-free
specs (existing behavior) must skip the entire block; that property is
covered by the existing ``test_runner_inmemory.py`` suite, which still
passes against this file's runner changes.
"""

from __future__ import annotations

import json
from datetime import date as Date
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.ml.artifact import (
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.ml.coverage import iter_consolidated_bars
from app.research.runs.runner import RunRequest, run_date_to_ms, run_strategy_spec


def _build_spec_with_prediction(prediction_set_id: str) -> StrategySpec:
    """Build a spec that references the given prediction set.

    Same shape as the canonical EMA crossover test spec used elsewhere,
    extended with one ``PredictionRef`` and one ``PredictionComparison``
    in the entry conditions. The prediction threshold is set to a value
    every prediction satisfies (>= 0.0) so the entry rule's gating is
    indistinguishable from the prediction-free spec — the test asserts
    on identity columns, not on trade behavior.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover with prediction gate",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": 5, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": 10, "source": "close"},
                {
                    "id": "rsi",
                    "kind": "RSI",
                    "period": 14,
                    "source": "close",
                    "ma_type": "wilders",
                },
            ],
            "predictions": [
                {"id": "p", "prediction_set_id": prediction_set_id, "field": "prediction"},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                    {"kind": "PredictionComparison", "prediction": "p", "op": ">=", "value": 0.0},
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 5}],
            },
        }
    )


def _make_artifact(
    artifacts_root: Path,
    set_id: str,
    bar_end_times_ms: list[int],
    *,
    symbol: str = "TEST",
) -> str:
    """Write a complete prediction-set artifact and return its top hash.

    One chunk, one prediction (``0.0``) per emitted bar end_time, all
    rows fall inside the chunk window. The manifest top-level
    ``prediction_set_hash`` is recomputed via ``compute_prediction_set_hash``
    so the loader's intrinsic validation accepts the artifact.
    """
    set_dir = artifacts_root / set_id
    chunk_dir = set_dir / "chunks"
    chunk_dir.mkdir(parents=True)

    rows = [{"timestamp_ms": ts, "symbol": symbol, "prediction": 0.0} for ts in bar_end_times_ms]
    trained_through_ms = bar_end_times_ms[0] - 1
    chunk_path = chunk_dir / f"{trained_through_ms}.parquet"
    write_chunk_rows(chunk_path, rows, field_names=["prediction"])

    manifest = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": symbol,
        "resolution_minutes": 15,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {
            "kind": "deterministic_rule",
            "rule_id": "test",
            "rule_version": "1.0",
        },
        "chunks": [
            {
                "trained_through_ms": trained_through_ms,
                "start_ms": bar_end_times_ms[0],
                "end_ms": bar_end_times_ms[-1],
                "row_count": len(rows),
                "rows_hash": compute_rows_hash(rows),
            }
        ],
        # Placeholder; recomputed below so the file is self-consistent.
        "prediction_set_hash": "0" * 64,
    }
    top_hash = compute_prediction_set_hash(manifest)
    manifest["prediction_set_hash"] = top_hash
    (set_dir / "manifest.json").write_text(json.dumps(manifest))
    return top_hash


def _harvest_consolidated_end_times_ms(
    bars_factory,
    symbol: str,
    start: Date,
    end: Date,
    resolution_minutes: int,
) -> list[int]:
    """Run the runner's own coverage helper to learn the engine's bar clock.

    By harvesting via ``iter_consolidated_bars`` we guarantee the
    artifact we build covers exactly the same bars the runner will
    later check against — without coupling the test to consolidator
    internals.
    """
    data_source = bars_factory(symbol, start, end)
    bars = list(
        iter_consolidated_bars(
            data_source,
            symbol=symbol,
            start_date=start,
            end_date=end,
            resolution_minutes=resolution_minutes,
        )
    )
    return [b.end_ms for b in bars]


@pytest.fixture
def fake_data_factory():
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: Date, end: Date):
        return FakeDataReader(bars=bars)

    return factory


@pytest.fixture
def artifacts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the runner at a tmp_path-rooted artifact directory."""
    root = tmp_path / "artifacts" / "predictions"
    root.mkdir(parents=True)
    monkeypatch.setenv("LEARN_AI_PREDICTION_ARTIFACTS_ROOT", str(root))
    return root


# ---------------------------------------------------------------------------
# Happy path: prediction set loads, covers, and its hash threads to the ledger.
# ---------------------------------------------------------------------------
def test_runner_threads_prediction_set_hash_into_ledger(fake_data_factory, artifacts_root: Path) -> None:
    """The runner loads the artifact, validates it, and records its hash."""
    start = Date(2024, 1, 2)
    end = Date(2024, 12, 31)

    expected_ts_ms = _harvest_consolidated_end_times_ms(fake_data_factory, "TEST", start, end, resolution_minutes=15)
    assert expected_ts_ms, "synthetic data must fire at least one consolidated bar"

    set_id = "pred_test_v001"
    expected_hash = _make_artifact(artifacts_root, set_id, expected_ts_ms)

    spec = _build_spec_with_prediction(set_id)
    ledger, result = run_strategy_spec(
        RunRequest(spec=spec, start_ms=run_date_to_ms(start), end_ms=run_date_to_ms(end)),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )

    assert ledger.status == "completed", ledger.failure_reason
    assert ledger.prediction_set_hash == expected_hash
    assert result.run_id == ledger.run_id


# ---------------------------------------------------------------------------
# Failure paths — each must produce a ``failed`` ledger, not a thrown exception.
# ---------------------------------------------------------------------------
def test_runner_fails_the_ledger_when_the_prediction_set_is_missing_unpaired_or_gappy(
    fake_data_factory, artifacts_root: Path
) -> None:
    start = Date(2024, 1, 2)
    end = Date(2024, 12, 31)
    expected_ts_ms = _harvest_consolidated_end_times_ms(fake_data_factory, "TEST", start, end, resolution_minutes=15)

    # Artifact directory is missing.
    spec = _build_spec_with_prediction("pred_missing_v001")
    missing_ledger, missing_result = run_strategy_spec(
        RunRequest(spec=spec, start_ms=run_date_to_ms(start), end_ms=run_date_to_ms(end)),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )

    assert missing_ledger.status == "failed", "missing artifact directory: ledger status is failed"
    assert missing_ledger.failure_reason is not None, "missing artifact directory: failure reason is recorded"
    assert "pred_missing_v001" in missing_ledger.failure_reason, "missing artifact directory: reason names the set"
    assert "failed to load" in missing_ledger.failure_reason, "missing artifact directory: reason says it failed to load"
    assert missing_ledger.prediction_set_hash is None, "missing artifact directory: no prediction set hash"
    # _failed must still populate the result hashes.
    assert missing_ledger.result_hash is not None, "missing artifact directory: failed ledger still has result hash"
    assert missing_result.warnings == [missing_ledger.failure_reason], "missing artifact directory: warning is the reason"

    # Symbol mismatch — spec wants TEST but artifact says SPY.
    set_id = "pred_wrong_symbol_v001"
    _make_artifact(artifacts_root, set_id, expected_ts_ms, symbol="SPY")

    spec = _build_spec_with_prediction(set_id)
    unpaired_ledger, _ = run_strategy_spec(
        RunRequest(spec=spec, start_ms=run_date_to_ms(start), end_ms=run_date_to_ms(end)),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )

    assert unpaired_ledger.status == "failed", "prediction set that does not pair with spec: ledger status is failed"
    assert unpaired_ledger.failure_reason is not None, "prediction set that does not pair with spec: reason is recorded"
    assert "does not pair with spec" in unpaired_ledger.failure_reason, (
        "prediction set that does not pair with spec: reason says it does not pair"
    )
    assert "symbol mismatch" in unpaired_ledger.failure_reason, (
        "prediction set that does not pair with spec: reason names the symbol mismatch"
    )
    assert unpaired_ledger.prediction_set_hash is None, "prediction set that does not pair with spec: no hash"

    # Drop a few bar end_times from the artifact -> coverage must fail.
    assert len(expected_ts_ms) > 5, "bar-clock coverage gap: synthetic data fires enough consolidated bars"

    # Drop the last 3 timestamps so coverage check finds a gap.
    short_ts_ms = expected_ts_ms[:-3]
    set_id = "pred_gap_v001"
    _make_artifact(artifacts_root, set_id, short_ts_ms)

    spec = _build_spec_with_prediction(set_id)
    gap_ledger, _ = run_strategy_spec(
        RunRequest(spec=spec, start_ms=run_date_to_ms(start), end_ms=run_date_to_ms(end)),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )

    assert gap_ledger.status == "failed", "bar-clock coverage gap: ledger status is failed"
    assert gap_ledger.failure_reason is not None, "bar-clock coverage gap: failure reason is recorded"
    assert "bar-clock coverage failed" in gap_ledger.failure_reason, (
        "bar-clock coverage gap: reason says coverage failed"
    )
    assert gap_ledger.prediction_set_hash is None, "bar-clock coverage gap: no prediction set hash"
