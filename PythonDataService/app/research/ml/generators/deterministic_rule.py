"""Deterministic-rule generator: emits a prediction value for every emitted
bar via a closed-form function of existing features.

v0.5 ships one rule: ``rsi_14_centered`` (prediction = RSI14/100 - 0.5).
Bars before RSI's 14-bar warmup completes emit ``prediction = 0.0``,
satisfying the ``neutral_zero_until_feature_ready`` warmup policy.

This module produces the per-row dicts only. Manifest assembly,
chunk parquet writing, and CLI orchestration live in
``app/research/ml/generate_prediction_set.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
import pandas_ta as ta  # type: ignore[import-untyped]

RULE_ID = "rsi_14_centered"
RULE_VERSION = "1.0"


def compute_rsi_14_centered_predictions(
    closes: Sequence[float],
    timestamps_ms: Sequence[int],
    *,
    symbol: str = "SPY",
) -> list[dict]:
    """Return one row per (close, timestamp) pair.

    Predictions for bars where RSI has not yet warmed (first 13 bars
    out of any non-empty input) are emitted as ``0.0``. From bar 14
    onwards, ``prediction = (RSI14 - 50) / 100``, equivalent to
    ``RSI14/100 - 0.5``.
    """
    if len(closes) != len(timestamps_ms):
        raise ValueError(
            f"closes ({len(closes)}) and timestamps_ms ({len(timestamps_ms)}) length mismatch"
        )

    if not closes:
        return []

    series = pd.Series(closes, dtype="float64")
    rsi: pd.Series | None = ta.rsi(series, length=14)

    # pandas_ta returns None when the series is shorter than its minimum length.
    rsi_list: list[float | None] = rsi.tolist() if rsi is not None else [None] * len(closes)

    rows: list[dict] = []
    for idx, (ts, rsi_val) in enumerate(zip(timestamps_ms, rsi_list, strict=True)):
        # Apply neutral_zero_until_feature_ready warmup policy: RSI-14 requires
        # 14 bars (indices 0-13) before it is meaningful; emit 0.0 for bars 0-12.
        if idx < 13 or rsi_val is None or (isinstance(rsi_val, float) and pd.isna(rsi_val)):
            prediction = 0.0
        else:
            # Clamp to [-0.5, 0.5] to guard against floating-point overshoot
            # (e.g. RSI=100.0000...0001 from Wilder smoothing accumulation).
            prediction = max(-0.5, min(0.5, float(rsi_val) / 100.0 - 0.5))
        rows.append({"timestamp_ms": int(ts), "symbol": symbol, "prediction": prediction})
    return rows
