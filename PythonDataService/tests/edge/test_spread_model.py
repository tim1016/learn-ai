"""Parity tests for app.engine.edge.spread_model."""

from __future__ import annotations

import numpy as np
import pytest

from app.engine.edge.spread_model import (
    DEFAULT_ALPHA,
    DEFAULT_K,
    OPTION_SPREAD_FLOOR,
    is_tradable,
    option_spread,
    stock_spread,
)


def test_option_spread_atm_30d_matches_hand_calc():
    # S=400, K=400 (delta=0.5), T=30/365, IV=0.20, defaults
    s, k_strike, t, iv = 400.0, 400.0, 30.0 / 365.0, 0.20
    expected = DEFAULT_K * iv * np.sqrt(t) * (1.0 + DEFAULT_ALPHA * 0.0) * s
    got = option_spread(
        underlying_price=s,
        strike=k_strike,
        time_to_expiry_years=t,
        iv=iv,
        delta=0.50,
    )
    np.testing.assert_allclose(got, expected, atol=1e-12)
    assert got > OPTION_SPREAD_FLOOR


def test_option_spread_floor_engages_for_low_iv():
    got = option_spread(
        underlying_price=10.0,
        strike=10.0,
        time_to_expiry_years=1 / 365.0,
        iv=0.001,
        delta=0.50,
    )
    assert got == OPTION_SPREAD_FLOOR


def test_option_spread_wing_penalty_increases_with_distance_from_atm():
    base_args = dict(
        underlying_price=400.0,
        strike=400.0,
        time_to_expiry_years=30.0 / 365.0,
        iv=0.20,
    )
    atm = option_spread(delta=0.50, **base_args)
    wing_25 = option_spread(delta=0.25, **base_args)
    wing_75 = option_spread(delta=0.75, **base_args)
    assert wing_25 > atm
    assert wing_75 > atm
    np.testing.assert_allclose(wing_25, wing_75, atol=1e-12)
    expected_ratio = 1.0 + DEFAULT_ALPHA * 0.25
    np.testing.assert_allclose(wing_25 / atm, expected_ratio, atol=1e-12)


def test_option_spread_sqrt_t_scaling():
    base_args = dict(
        underlying_price=400.0,
        strike=400.0,
        iv=0.20,
        delta=0.50,
    )
    short = option_spread(time_to_expiry_years=30.0 / 365.0, **base_args)
    long = option_spread(time_to_expiry_years=120.0 / 365.0, **base_args)
    np.testing.assert_allclose(long / short, np.sqrt(120.0 / 30.0), atol=1e-12)


def test_option_spread_rejects_non_positive_t():
    with pytest.raises(ValueError, match="time_to_expiry_years"):
        option_spread(
            underlying_price=400.0,
            strike=400.0,
            time_to_expiry_years=0.0,
            iv=0.20,
            delta=0.5,
        )


def test_option_spread_vectorized_inputs():
    deltas = np.array([0.25, 0.50, 0.75])
    spreads = option_spread(
        underlying_price=400.0,
        strike=400.0,
        time_to_expiry_years=30 / 365.0,
        iv=0.20,
        delta=deltas,
    )
    assert spreads.shape == deltas.shape
    np.testing.assert_allclose(spreads[0], spreads[2], atol=1e-12)
    assert spreads[1] < spreads[0]


def test_stock_spread_default_1bp():
    px = 400.0
    expected = px * 1e-4  # 1bp
    np.testing.assert_allclose(stock_spread(px), expected, atol=1e-12)


def test_stock_spread_rejects_non_positive_price():
    with pytest.raises(ValueError, match="price"):
        stock_spread(0.0)


def test_is_tradable_passes_all_floors():
    assert is_tradable(spread=0.05, mid=4.0, quoted_volume=200, open_interest=500)


def test_is_tradable_fails_on_wide_spread():
    assert not is_tradable(spread=2.0, mid=4.0, quoted_volume=200, open_interest=500)


def test_is_tradable_fails_on_low_volume():
    assert not is_tradable(spread=0.05, mid=4.0, quoted_volume=10, open_interest=500)


def test_is_tradable_fails_on_low_open_interest():
    assert not is_tradable(spread=0.05, mid=4.0, quoted_volume=200, open_interest=10)


def test_is_tradable_fails_on_zero_mid():
    assert not is_tradable(spread=0.01, mid=0.0, quoted_volume=200, open_interest=500)
