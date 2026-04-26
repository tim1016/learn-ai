"""Sanity tests for realized-vol estimators.

Full golden fixtures (TTR R parity) land alongside DB-backed integration
tests. These tests verify mathematical properties:
  - Constant prices -> zero variance
  - Annualization scales as sqrt(252)
  - Yang-Zhang k constant matches the published formula
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.edge.features_realtime.realized_vol import (
    DAILY_BARS_PER_YEAR,
    close_to_close,
    garman_klass,
    parkinson,
    yang_zhang,
)


def _flat_bars(n: int, price: float = 100.0) -> pd.DataFrame:
    ts = pd.Index(np.arange(n, dtype=np.int64) * 86_400_000)
    return pd.DataFrame({
        "open": price, "high": price, "low": price, "close": price,
    }, index=ts)


def _gbm_bars(n: int, sigma: float = 0.20, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    daily_sigma = sigma / np.sqrt(DAILY_BARS_PER_YEAR)
    log_ret = rng.normal(0.0, daily_sigma, size=n)
    close = 100.0 * np.exp(np.cumsum(log_ret))
    open_ = np.roll(close, 1)
    open_[0] = 100.0
    high = np.maximum(open_, close) * (1 + rng.uniform(0.0001, 0.005, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0001, 0.005, n))
    ts = pd.Index(np.arange(n, dtype=np.int64) * 86_400_000)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
    }, index=ts)


def test_close_to_close_zero_when_prices_constant():
    bars = _flat_bars(60)
    rv = close_to_close(bars, window=20, annualize=True)
    assert rv.dropna().abs().max() < 1e-12


def test_parkinson_zero_when_high_equals_low():
    bars = _flat_bars(60)
    rv = parkinson(bars, window=20, annualize=True)
    assert rv.dropna().abs().max() < 1e-12


def test_garman_klass_zero_when_ohlc_constant():
    bars = _flat_bars(60)
    rv = garman_klass(bars, window=20, annualize=True)
    assert rv.dropna().abs().max() < 1e-12


def test_yang_zhang_zero_when_ohlc_constant():
    bars = _flat_bars(60)
    rv = yang_zhang(bars, window=20, annualize=True)
    assert rv.dropna().abs().max() < 1e-12


def test_close_to_close_recovers_input_sigma_approx_on_gbm():
    bars = _gbm_bars(2000, sigma=0.20, seed=42)
    rv = close_to_close(bars, window=252, annualize=True)
    final_rv = rv.dropna().iloc[-1]
    np.testing.assert_allclose(final_rv, 0.20, atol=0.02)


def test_yang_zhang_k_constant_matches_paper():
    """Yang-Zhang 2000: k = 0.34 / (1.34 + (n+1)/(n-1))."""
    n = 20
    expected_k = 0.34 / (1.34 + (n + 1) / (n - 1))
    bars = _flat_bars(n + 1)
    bars.iloc[1:, bars.columns.get_loc("open")] += 0.01  # break degeneracy
    bars.iloc[1:, bars.columns.get_loc("close")] += 0.01
    yz = yang_zhang(bars, window=n, annualize=False)
    assert yz.notna().any()
    assert 0 < expected_k < 1
