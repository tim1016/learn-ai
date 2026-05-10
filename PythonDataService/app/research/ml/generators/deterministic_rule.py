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

    Warmup policy — ``neutral_zero_until_feature_ready``:
        Indices 0–12 (13 bars) are emitted as ``prediction = 0.0``.
        From index 13 onward, ``prediction = (RSI14 - 50) / 100``,
        equivalent to ``RSI14/100 - 0.5``.

    Observed pandas_ta NaN range (verified empirically 2026-05-10,
    pandas_ta 0.3.14b, monotonically-increasing series of length 20):

        ta.rsi(series, length=14) → nan ONLY at index 0; indices 1–19
        all return a numeric value (100.0 for a monotone up-series
        because all gains, zero losses → RS=∞ → RSI→100).

    So pandas_ta's own NaN guard covers only index 0. The ``idx < 13``
    gate is an explicit DESIGN CHOICE to emit 13 warmup zeros rather
    than the 1 that pandas_ta's NaN range would require. Rationale:
    Wilder's smoothing needs at least 14 bars to produce a reliable
    estimate; the first-bar RSI of 100.0 for an up-series is
    technically "valid" but economically noisy. Emitting zeros for the
    full first 14 bars (indices 0–13) would match LEAN's convention;
    the current gate of ``idx < 13`` emits zeros for 13 bars, meaning
    index 13 is the first non-zero output.

    This is pinned by ``test_rsi_warmup_zero_count_is_pinned`` in
    ``tests/research/ml/test_generator.py``. Any change here (e.g. to
    align with LEAN's 14-bar convention) MUST update that test and the
    E2E hash fixture in ``tests/research/ml/fixtures/e2e_known_hashes.json``.
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
        # Apply neutral_zero_until_feature_ready warmup policy. Gate at idx < 13:
        # emits zeros for indices 0–12 (13 bars). Index 13 is the first bar
        # eligible for a non-zero prediction. See docstring for the rationale
        # vs. pandas_ta's own NaN range (only index 0 is NaN in practice).
        if idx < 13 or rsi_val is None or (isinstance(rsi_val, float) and pd.isna(rsi_val)):
            prediction = 0.0
        else:
            # Clamp to [-0.5, 0.5] to guard against floating-point overshoot
            # (e.g. RSI=100.0000...0001 from Wilder smoothing accumulation).
            prediction = max(-0.5, min(0.5, float(rsi_val) / 100.0 - 0.5))
        rows.append({"timestamp_ms": int(ts), "symbol": symbol, "prediction": prediction})
    return rows
