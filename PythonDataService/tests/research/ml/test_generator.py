"""Tests for the deterministic-rule generator: rsi_14_centered.

The rule is: prediction = rsi_14(close) / 100.0 - 0.5
Bars before RSI is ready emit prediction = 0.0 (warmup_policy:
neutral_zero_until_feature_ready).
"""
from __future__ import annotations

import json
from datetime import date as Date
from pathlib import Path

from app.research.ml.generators.deterministic_rule import compute_rsi_14_centered_predictions


def test_first_thirteen_bars_emit_zero() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert all(r["prediction"] == 0.0 for r in rows[:13])


def test_warmed_bars_have_nonzero_prediction() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert any(r["prediction"] != 0.0 for r in rows[14:])


def test_predictions_are_in_range_minus_half_to_plus_half() -> None:
    closes = [100.0 + i * 0.1 for i in range(50)]
    timestamps_ms = [1000 * i for i in range(50)]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    for r in rows:
        assert -0.5 <= r["prediction"] <= 0.5


def test_deterministic_for_same_input() -> None:
    closes = [100.0 + i * 0.1 for i in range(20)]
    timestamps_ms = [1000 * i for i in range(20)]
    a = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    b = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert a == b


def test_row_shape() -> None:
    closes = [100.0]
    timestamps_ms = [0]
    rows = compute_rsi_14_centered_predictions(closes, timestamps_ms)
    assert rows[0] == {"timestamp_ms": 0, "symbol": "SPY", "prediction": 0.0}


def test_symbol_passes_through_arg() -> None:
    rows = compute_rsi_14_centered_predictions([100.0], [0], symbol="QQQ")
    assert rows[0]["symbol"] == "QQQ"


# ----- CLI orchestration --------------------------------------------


from app.research.ml.generate_prediction_set import generate_prediction_set  # noqa: E402


def _synthetic_bars_provider(num_bars: int):
    """Return a callable that yields (close, timestamp_ms) pairs the generator
    can consume. The CLI's real path will call into the LEAN reader; this
    factory swaps a fixed synthetic series for unit tests.
    """

    def provider(*, symbol: str, start: Date, end: Date, resolution_minutes: int):
        for i in range(num_bars):
            close = 100.0 + i * 0.1
            timestamp_ms = 1714521600000 + i * resolution_minutes * 60_000
            yield close, timestamp_ms

    return provider


def test_generate_writes_artifact_round_trip(tmp_path: Path) -> None:
    """Generator -> loader round-trip: hash check passes."""
    out_dir = tmp_path / "artifacts" / "predictions"
    out_dir.mkdir(parents=True)

    set_id = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_dir,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )

    assert set_id == "pred_spy_rsi_14_centered_2024-05-01_2024-05-02_15m_v1"
    set_dir = out_dir / set_id
    assert (set_dir / "manifest.json").is_file()

    from app.research.ml.loader import PredictionSet

    pset = PredictionSet.load(set_dir)
    assert pset.manifest.prediction_set_id == set_id
    assert pset.manifest.warmup_policy == "neutral_zero_until_feature_ready"


def test_generate_is_byte_identical_on_repeat(tmp_path: Path) -> None:
    """Same inputs -> same prediction_set_hash."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()

    set_id_a = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_a,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )
    set_id_b = generate_prediction_set(
        rule="rsi_14_centered",
        symbol="SPY",
        start=Date(2024, 5, 1),
        end=Date(2024, 5, 2),
        resolution_minutes=15,
        artifacts_root=out_b,
        bars_provider=_synthetic_bars_provider(num_bars=20),
    )
    assert set_id_a == set_id_b

    a_manifest = json.loads((out_a / set_id_a / "manifest.json").read_text())
    b_manifest = json.loads((out_b / set_id_b / "manifest.json").read_text())
    assert a_manifest["prediction_set_hash"] == b_manifest["prediction_set_hash"]
