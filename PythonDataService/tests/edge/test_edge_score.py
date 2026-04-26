"""Tests for Edge Score composite."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engine.edge.edge_score import (
    DEFAULT_WEIGHTS,
    edge_score,
    s_iv_percentile,
    s_regime,
    s_trend,
    s_vrp,
)


def test_default_weights_sum_to_one():
    assert pytest.approx(sum(DEFAULT_WEIGHTS.values()), abs=1e-9) == 1.0


def test_s_vrp_negative_when_vrp_high():
    n = 300
    vrp = pd.Series(np.concatenate([np.zeros(n - 1), [1.0]]))
    s = s_vrp(vrp, lookback=252)
    assert s.iloc[-1] < 0


def test_s_iv_percentile_negative_when_iv_at_top():
    n = 300
    iv = pd.Series(np.linspace(0.10, 0.30, n))
    s = s_iv_percentile(iv, lookback=252)
    # Top of percentile range → bounded at exactly -1 (rank == 1.0).
    assert s.iloc[-1] < 0
    assert s.iloc[-1] >= -1.0
    # And the second-to-last is less negative than the last (monotone).
    assert s.iloc[-1] <= s.iloc[-2]


def test_s_trend_in_negative_zero_band():
    slope = pd.Series([0.0, 1.0, 2.0, 5.0, 10.0])
    atr = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0])
    s = s_trend(slope, atr)
    assert (s <= 0).all()
    assert (s >= -1).all()
    # stronger trend = stronger negative penalty
    assert s.iloc[-1] < s.iloc[1]


def test_s_regime_uses_score_map():
    labels = pd.Series([0, 1, 2, 0, 1])
    s = s_regime(labels, score_map={0: 0.0, 1: -0.5, 2: 0.5})
    np.testing.assert_array_equal(s.to_numpy(), [0.0, -0.5, 0.5, 0.0, -0.5])


def test_edge_score_action_positive_when_components_long_vol():
    n = 300
    rng = np.random.default_rng(0)
    vrp = pd.Series(rng.normal(0.0, 0.001, n))
    vrp.iloc[-1] = -0.05  # negative VRP → long vol favored
    iv30 = pd.Series(np.linspace(0.30, 0.10, n))  # falling IV → low percentile at end
    slope = pd.Series(np.zeros(n))
    atr = pd.Series(np.ones(n))
    labels = pd.Series([2] * n)  # choppy-high-vol = +0.5
    res = edge_score(
        vrp=vrp,
        iv30=iv30,
        trend_slope=slope,
        atr=atr,
        regime_labels=labels,
    )
    assert res.action.iloc[-1] == 1


def test_edge_score_rejects_weights_that_dont_sum_to_one():
    with pytest.raises(ValueError, match="weights must sum"):
        edge_score(
            vrp=pd.Series([0.0]),
            iv30=pd.Series([0.20]),
            trend_slope=pd.Series([0.0]),
            atr=pd.Series([1.0]),
            regime_labels=pd.Series([0]),
            weights={"vrp": 0.5, "regime": 0.5, "iv_pct": 0.5, "trend": 0.5},
        )
