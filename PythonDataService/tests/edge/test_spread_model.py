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


def test_option_spread_atm_30d_matches_hand_calc_and_wing_penalty_increases_with_distance_from_atm():
    # S=400, K=400 (delta=0.5), T=30/365, IV=0.20, defaults
    s, k_strike, t, iv = 400.0, 400.0, 30.0 / 365.0, 0.20
    base_args = dict(
        underlying_price=s,
        strike=k_strike,
        time_to_expiry_years=t,
        iv=iv,
    )
    expected = DEFAULT_K * iv * np.sqrt(t) * (1.0 + DEFAULT_ALPHA * 0.0) * s
    atm = option_spread(delta=0.50, **base_args)
    np.testing.assert_allclose(atm, expected, atol=1e-12, err_msg="ATM 30d spread matches the hand calc")
    assert atm > OPTION_SPREAD_FLOOR, "ATM 30d spread is above the floor"
    wing_25 = option_spread(delta=0.25, **base_args)
    wing_75 = option_spread(delta=0.75, **base_args)
    assert wing_25 > atm, "25-delta wing spread exceeds ATM"
    assert wing_75 > atm, "75-delta wing spread exceeds ATM"
    np.testing.assert_allclose(wing_25, wing_75, atol=1e-12, err_msg="the 25- and 75-delta wings are symmetric")
    expected_ratio = 1.0 + DEFAULT_ALPHA * 0.25
    np.testing.assert_allclose(
        wing_25 / atm, expected_ratio, atol=1e-12, err_msg="the wing-to-ATM ratio is 1 + alpha * 0.25"
    )


def test_option_spread_floor_engages_for_low_iv():
    got = option_spread(
        underlying_price=10.0,
        strike=10.0,
        time_to_expiry_years=1 / 365.0,
        iv=0.001,
        delta=0.50,
    )
    assert got == OPTION_SPREAD_FLOOR


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


@pytest.mark.parametrize(
    ("spread", "mid", "quoted_volume", "open_interest", "tradable"),
    [
        (0.05, 4.0, 200, 500, True),
        (2.0, 4.0, 200, 500, False),
        (0.05, 4.0, 10, 500, False),
        (0.05, 4.0, 200, 10, False),
        (0.01, 0.0, 200, 500, False),
    ],
    ids=[
        "passes-all-floors",
        "fails-on-wide-spread",
        "fails-on-low-volume",
        "fails-on-low-open-interest",
        "fails-on-zero-mid",
    ],
)
def test_is_tradable_applies_every_floor(spread, mid, quoted_volume, open_interest, tradable):
    assert bool(is_tradable(spread=spread, mid=mid, quoted_volume=quoted_volume, open_interest=open_interest)) is tradable
