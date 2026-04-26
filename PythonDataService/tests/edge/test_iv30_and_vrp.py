"""Tests for delta_inversion, iv30_constructor, and vrp."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.features_realtime.delta_inversion import (
    strike_and_iv_for_delta,
    strike_for_delta_constant_vol,
)
from app.engine.edge.features_realtime.iv30_constructor import (
    iv30_atm_50d,
    iv_change,
    iv_vol,
    skew_25d,
    term_slope,
    variance_interpolated_iv,
)
from app.engine.edge.vrp import compute_vrp, vrp_signal


def test_strike_for_delta_50d_atm_at_zero_rates():
    K = strike_for_delta_constant_vol(
        S=400.0,
        T=30.0 / 365.0,
        r=0.0,
        q=0.0,
        sigma=0.20,
        target_delta=0.50,
    )
    # 50Δ ATM strike at zero rates: K = S * exp(σ²T/2 - σ√T · N⁻¹(0.5))
    # N⁻¹(0.5) = 0; so K = S * exp(σ²T/2)
    expected = 400.0 * np.exp(0.5 * 0.20**2 * 30.0 / 365.0)
    np.testing.assert_allclose(K, expected, rtol=1e-9)


def test_strike_for_delta_25d_call_higher_than_atm():
    atm = strike_for_delta_constant_vol(
        S=400.0,
        T=30.0 / 365.0,
        r=0.0,
        q=0.0,
        sigma=0.20,
        target_delta=0.50,
    )
    otm_call = strike_for_delta_constant_vol(
        S=400.0,
        T=30.0 / 365.0,
        r=0.0,
        q=0.0,
        sigma=0.20,
        target_delta=0.25,
    )
    assert otm_call > atm  # 25Δ call is OTM


def test_strike_for_delta_rejects_out_of_range():
    with pytest.raises(ValueError):
        strike_for_delta_constant_vol(
            S=400.0,
            T=30.0 / 365.0,
            r=0.0,
            q=0.0,
            sigma=0.20,
            target_delta=1.5,
        )


def test_strike_and_iv_for_delta_with_flat_surface():
    flat = lambda K, T: 0.20  # noqa: E731
    K, sigma = strike_and_iv_for_delta(
        S=400.0,
        T=30.0 / 365.0,
        r=0.0,
        q=0.0,
        target_delta=0.50,
        surface=flat,
    )
    assert sigma == 0.20
    assert 395.0 < K < 405.0


def test_variance_interpolated_iv_midpoint():
    sigma = variance_interpolated_iv(
        sigma_t1=0.20,
        t1_years=20 / 365.0,
        sigma_t2=0.30,
        t2_years=40 / 365.0,
        target_t_years=30 / 365.0,
    )
    var_30 = (0.5 * 0.20**2 * 20 + 0.5 * 0.30**2 * 40) / 30
    expected = np.sqrt(var_30)
    np.testing.assert_allclose(sigma, expected, atol=1e-12)


def test_iv30_atm_50d_picks_straddling_expiries():
    iv_by_expiry = pd.Series({20: 0.20, 40: 0.30, 60: 0.32})
    iv30 = iv30_atm_50d(iv_by_expiry, target_days=30)
    assert 0.20 < iv30 < 0.30
    assert iv30 is not None


def test_iv30_atm_50d_extrapolates_when_no_straddle():
    iv_by_expiry = pd.Series({40: 0.30, 60: 0.32})
    iv30 = iv30_atm_50d(iv_by_expiry, target_days=30)
    assert iv30 == 0.30  # nearest = 40d


def test_skew_25d_typical_equity_positive():
    assert skew_25d(0.25, 0.18) > 0


def test_term_slope_contango_positive():
    assert term_slope(iv_30d=0.20, iv_60d=0.22) > 0


def test_iv_change_first_difference():
    s = pd.Series([0.20, 0.21, 0.19, 0.25])
    out = iv_change(s).dropna().tolist()
    np.testing.assert_allclose(out, [0.01, -0.02, 0.06], atol=1e-12)


def test_iv_vol_rolling_std():
    s = pd.Series(np.arange(30, dtype=np.float64))
    iv_v = iv_vol(s, window=10)
    assert iv_v.notna().sum() == 21
    assert iv_v.dropna().iloc[-1] > 0


def test_compute_vrp_simple_case():
    iv = pd.Series([0.20, 0.25, 0.30])
    rv = pd.Series([0.10, 0.20, 0.40])
    out = compute_vrp(iv, rv).to_numpy()
    np.testing.assert_allclose(out, [0.20**2 - 0.10**2, 0.25**2 - 0.20**2, 0.30**2 - 0.40**2], atol=1e-12)


def test_vrp_signal_emits_long_vol_when_zscore_low():
    n = 300
    rng = np.random.default_rng(0)
    iv_arr = np.full(n, 0.20)
    rv_arr = rng.normal(0.18, 0.01, size=n)
    rv_arr[-1] = 0.30  # spike → VRP very negative
    iv = pd.Series(iv_arr)
    rv = pd.Series(rv_arr)
    sig = vrp_signal(iv=iv, rv=rv, lookback=252, threshold=1.0)
    assert sig.side.iloc[-1] == 1
