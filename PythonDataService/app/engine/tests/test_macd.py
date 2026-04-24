"""Tests for MovingAverageConvergenceDivergence.

Same three-layer coverage as the ADX tests:

1. Hand-computed micro-tests — warmup boundaries, exact MACD / signal
   / histogram values for a small monotone series.
2. Cross-reference vs pandas-ta's ``ta.macd`` on a 500-bar synthetic
   series, pinned at ``atol=1e-9`` because both implementations use
   SMA-seeded EMAs with identical math post-warmup.
3. Golden-fixture regression against the committed CSVs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

from app.engine.indicators.macd import MovingAverageConvergenceDivergence

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "golden" / "macd_12_26_9"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _synthetic_closes(count: int, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    vals = [150.0]
    for _ in range(count - 1):
        vals.append(vals[-1] + rng.normal(0.0, 0.3))
    base = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    idx = [base + pd.Timedelta(minutes=15 * i) for i in range(count)]
    return pd.Series(vals, index=pd.DatetimeIndex(idx, name="timestamp"), name="close")


def _run_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    macd = MovingAverageConvergenceDivergence("MACD", fast, slow, signal)
    m: list[float | None] = []
    s: list[float | None] = []
    h: list[float | None] = []
    for ts, v in closes.items():
        macd.update(ts.to_pydatetime(), Decimal(str(v)))
        m.append(float(macd.macd) if macd.macd is not None else None)
        s.append(float(macd.signal) if macd.signal is not None else None)
        h.append(float(macd.histogram) if macd.histogram is not None else None)
    return pd.DataFrame({"macd": m, "signal": s, "histogram": h}, index=closes.index)


# ---------------------------------------------------------------------------
# Layer 1: hand-computed micro-tests
# ---------------------------------------------------------------------------


def test_warmup_emits_macd_after_slow_signal_after_slow_plus_signal_minus_1():
    """MACD line first available at samples == slow_period.
    Signal line first available at samples == slow_period + signal_period - 1.
    """
    macd = MovingAverageConvergenceDivergence("MACD", fast_period=3, slow_period=5, signal_period=3)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for i in range(4):
        macd.update(t + timedelta(minutes=15 * i), Decimal(100 + i))
        assert macd.macd is None, f"macd ready too early at sample {i + 1}"
        assert not macd.is_ready
    # Sample 5: macd line emitted; signal EMA receives its 1st sample.
    macd.update(t + timedelta(minutes=15 * 4), Decimal(104))
    assert macd.macd is not None
    assert macd.signal is None
    assert not macd.is_ready
    # Sample 6: signal EMA receives its 2nd sample — still warming.
    macd.update(t + timedelta(minutes=15 * 5), Decimal(105))
    assert macd.signal is None
    assert not macd.is_ready
    # Sample 7 = slow + signal - 1: signal EMA receives its 3rd sample
    # and becomes ready.
    macd.update(t + timedelta(minutes=15 * 6), Decimal(106))
    assert macd.is_ready
    assert macd.signal is not None
    assert macd.histogram is not None


def test_current_value_is_macd_line():
    """LEAN convention: indicator.Current.Value is the MACD line."""
    macd = MovingAverageConvergenceDivergence("MACD", 3, 5, 3)
    t = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for i in range(10):
        macd.update(t + timedelta(minutes=15 * i), Decimal(100 + i))
    assert macd.current_value is not None
    assert macd.macd is not None
    assert macd.current_value == macd.macd


def test_reject_fast_ge_slow():
    with pytest.raises(ValueError):
        MovingAverageConvergenceDivergence("MACD", fast_period=26, slow_period=12)
    with pytest.raises(ValueError):
        MovingAverageConvergenceDivergence("MACD", fast_period=12, slow_period=12)


# ---------------------------------------------------------------------------
# Layer 2: cross-reference vs pandas-ta
# ---------------------------------------------------------------------------


def test_matches_pandas_ta_macd_strict():
    """pandas-ta's ta.macd uses SMA-seeded EMAs by default, matching
    our implementation. Post-warmup, values should agree to float
    precision. Pinned at ``atol=1e-9``."""
    closes = _synthetic_closes(500)
    ours = _run_macd(closes, 12, 26, 9)

    ref = ta.macd(closes, fast=12, slow=26, signal=9)
    # pandas-ta column names: MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
    ref_macd = ref["MACD_12_26_9"].to_numpy()
    ref_sig = ref["MACDs_12_26_9"].to_numpy()
    ref_hist = ref["MACDh_12_26_9"].to_numpy()

    ours_macd = np.array([v if v is not None else np.nan for v in ours["macd"].tolist()])
    ours_sig = np.array([v if v is not None else np.nan for v in ours["signal"].tolist()])
    ours_hist = np.array([v if v is not None else np.nan for v in ours["histogram"].tolist()])

    np.testing.assert_allclose(ours_macd, ref_macd, atol=1e-9, rtol=0, equal_nan=True)
    np.testing.assert_allclose(ours_sig, ref_sig, atol=1e-9, rtol=0, equal_nan=True)
    np.testing.assert_allclose(ours_hist, ref_hist, atol=1e-9, rtol=0, equal_nan=True)


# ---------------------------------------------------------------------------
# Layer 3: golden fixture regression
# ---------------------------------------------------------------------------


def test_golden_fixture_regression():
    input_csv = FIXTURE_DIR / "input.csv"
    output_csv = FIXTURE_DIR / "output.csv"
    if not input_csv.exists() or not output_csv.exists():
        pytest.skip("MACD golden fixture not yet generated")

    df_in = pd.read_csv(input_csv, parse_dates=["timestamp"])
    if df_in["timestamp"].dt.tz is None:
        df_in["timestamp"] = df_in["timestamp"].dt.tz_localize("UTC")
    closes = pd.Series(df_in["close"].to_numpy(), index=pd.DatetimeIndex(df_in["timestamp"]))

    ours = _run_macd(closes)
    df_out = pd.read_csv(output_csv)

    np.testing.assert_allclose(
        ours["macd"].astype(float).to_numpy(),
        df_out["macd"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        ours["signal"].astype(float).to_numpy(),
        df_out["signal"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        ours["histogram"].astype(float).to_numpy(),
        df_out["histogram"].astype(float).to_numpy(),
        atol=1e-9,
        rtol=0,
        equal_nan=True,
    )
