"""Tests for the deterministic-rule generator: rsi_14_centered.

The rule is: prediction = rsi_14(close) / 100.0 - 0.5
Bars before RSI is ready emit prediction = 0.0 (warmup_policy:
neutral_zero_until_feature_ready).
"""
from __future__ import annotations

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
