"""Indicator parity tests: verify calculate_dynamic_indicators matches direct pandas-ta output.

These tests ensure the wiring in calculate_dynamic_indicators correctly passes
parameters to pandas-ta and that column naming/extraction is consistent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.services.dataset_service import calculate_dynamic_indicators


def _make_realistic_bars(count: int = 500, seed: int = 42) -> pd.DataFrame:
    """Generate plausible minute-bar OHLCV data using a random walk."""
    rng = np.random.default_rng(seed)
    prices = [150.0]
    for _ in range(count - 1):
        prices.append(prices[-1] + rng.normal(0.0, 0.3))
    prices = np.array(prices)

    highs = prices + rng.uniform(0.1, 1.0, count)
    lows = prices - rng.uniform(0.1, 1.0, count)
    opens = prices + rng.uniform(-0.5, 0.5, count)
    closes = prices + rng.uniform(-0.5, 0.5, count)

    # Enforce OHLC consistency
    highs = np.maximum(highs, np.maximum(opens, closes))
    lows = np.minimum(lows, np.minimum(opens, closes))

    base_ts = 1704067200000  # 2024-01-01 00:00 UTC in ms
    return pd.DataFrame(
        {
            "timestamp": [base_ts + i * 60_000 for i in range(count)],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": rng.uniform(10_000, 500_000, count),
        }
    )


def test_ema_parity():
    """EMA(20) via calculate_dynamic_indicators matches pandas_ta.ema directly."""
    df = _make_realistic_bars()
    entries = [{"name": "ema", "params": {"length": 20}}]

    result_df, col_meta = calculate_dynamic_indicators(df.copy(), entries)

    # Find the EMA column produced
    ema_cols = [m["column"] for m in col_meta if m["indicator"] == "ema"]
    assert len(ema_cols) == 1, f"Expected 1 EMA column, got {ema_cols}"
    ema_col = ema_cols[0]

    expected = ta.ema(df["close"], length=20)
    pd.testing.assert_series_equal(
        result_df[ema_col].dropna().reset_index(drop=True),
        expected.dropna().reset_index(drop=True),
        rtol=1e-6,
        check_names=False,
    )


def test_rsi_parity():
    """RSI(14) via calculate_dynamic_indicators matches pandas_ta.rsi directly."""
    df = _make_realistic_bars()
    entries = [{"name": "rsi", "params": {"length": 14}}]

    result_df, col_meta = calculate_dynamic_indicators(df.copy(), entries)

    rsi_cols = [m["column"] for m in col_meta if m["indicator"] == "rsi"]
    assert len(rsi_cols) == 1, f"Expected 1 RSI column, got {rsi_cols}"
    rsi_col = rsi_cols[0]

    expected = ta.rsi(df["close"], length=14)
    pd.testing.assert_series_equal(
        result_df[rsi_col].dropna().reset_index(drop=True),
        expected.dropna().reset_index(drop=True),
        rtol=1e-6,
        check_names=False,
    )


def test_macd_parity():
    """MACD(12,26,9) via calculate_dynamic_indicators matches pandas_ta.macd directly."""
    df = _make_realistic_bars()
    entries = [{"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}]

    result_df, col_meta = calculate_dynamic_indicators(df.copy(), entries)

    macd_meta = [m for m in col_meta if m["indicator"] == "macd"]
    assert len(macd_meta) == 3, f"Expected 3 MACD columns, got {len(macd_meta)}"

    expected = ta.macd(df["close"], fast=12, slow=26, signal=9)
    for m in macd_meta:
        col = m["column"]
        # Match by prefix: macd_ -> MACD_, macds_ -> MACDs_, macdh_ -> MACDh_
        if col.startswith("macdh"):
            exp_col = next(c for c in expected.columns if c.startswith("MACDh_"))
        elif col.startswith("macds"):
            exp_col = next(c for c in expected.columns if c.startswith("MACDs_"))
        else:
            exp_col = next(c for c in expected.columns if c.startswith("MACD_"))

        pd.testing.assert_series_equal(
            result_df[col].dropna().reset_index(drop=True),
            expected[exp_col].dropna().reset_index(drop=True),
            rtol=1e-6,
            check_names=False,
        )


def test_bbands_parity():
    """Bollinger Bands(20, 2.0) via calculate_dynamic_indicators matches pandas_ta.bbands."""
    df = _make_realistic_bars()
    entries = [{"name": "bbands", "params": {"length": 20, "std": 2.0}}]

    result_df, col_meta = calculate_dynamic_indicators(df.copy(), entries)

    bb_meta = [m for m in col_meta if m["indicator"] == "bbands"]
    assert len(bb_meta) >= 3, f"Expected >=3 BBands columns, got {len(bb_meta)}"

    expected = ta.bbands(df["close"], length=20, std=2.0)
    for m in bb_meta:
        col = m["column"]
        # Match lower/mid/upper bands by prefix
        if col.startswith("bbl"):
            exp_col = next(c for c in expected.columns if c.startswith("BBL_"))
        elif col.startswith("bbm"):
            exp_col = next(c for c in expected.columns if c.startswith("BBM_"))
        elif col.startswith("bbu"):
            exp_col = next(c for c in expected.columns if c.startswith("BBU_"))
        elif col.startswith("bbb"):
            exp_col = next(c for c in expected.columns if c.startswith("BBB_"))
        elif col.startswith("bbp"):
            exp_col = next(c for c in expected.columns if c.startswith("BBP_"))
        else:
            continue

        pd.testing.assert_series_equal(
            result_df[col].dropna().reset_index(drop=True),
            expected[exp_col].dropna().reset_index(drop=True),
            rtol=1e-6,
            check_names=False,
        )


def test_supertrend_parity():
    """Supertrend(10, 3.0) via calculate_dynamic_indicators matches pandas_ta.supertrend."""
    df = _make_realistic_bars()
    entries = [{"name": "supertrend", "params": {"length": 10, "multiplier": 3.0}}]

    result_df, col_meta = calculate_dynamic_indicators(df.copy(), entries)

    st_meta = [m for m in col_meta if m["indicator"] == "supertrend"]
    assert len(st_meta) >= 2, f"Expected >=2 Supertrend columns, got {len(st_meta)}"

    expected = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
    for m in st_meta:
        col = m["column"]
        if col.startswith("supertl"):
            exp_col = next(c for c in expected.columns if c.startswith("SUPERTl_"))
        elif col.startswith("superts"):
            exp_col = next(c for c in expected.columns if c.startswith("SUPERTs_"))
        elif col.startswith("supertd"):
            exp_col = next(c for c in expected.columns if c.startswith("SUPERTd_"))
        elif col.startswith("supert_"):
            exp_col = next(c for c in expected.columns if c.startswith("SUPERT_"))
        else:
            continue

        pd.testing.assert_series_equal(
            result_df[col].dropna().reset_index(drop=True),
            expected[exp_col].dropna().reset_index(drop=True),
            rtol=1e-6,
            check_names=False,
        )
