"""Tests for app.research.signal.standardize."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.signal.standardize import (
    apply_threshold_filter,
    compute_train_zscore,
)


def test_compute_train_zscore_uses_train_mean_and_std():
    feature = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
    # Train mask = first 5 points (mu=3, sigma=sqrt(2.5))
    train_mask = pd.Series([True, True, True, True, True, False])

    z = compute_train_zscore(feature, train_mask, flip_sign=False)

    expected_mu = feature[train_mask].mean()
    expected_sigma = feature[train_mask].std()
    expected = (feature - expected_mu) / expected_sigma
    np.testing.assert_allclose(z.to_numpy(), expected.to_numpy(), atol=1e-12, rtol=0)


def test_compute_train_zscore_flip_sign_negates_output():
    feature = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    train_mask = pd.Series([True] * 5)

    z_positive = compute_train_zscore(feature, train_mask, flip_sign=False)
    z_flipped = compute_train_zscore(feature, train_mask, flip_sign=True)

    np.testing.assert_allclose(z_flipped.to_numpy(), -z_positive.to_numpy(), atol=1e-12, rtol=0)


@pytest.mark.parametrize(
    ("feature_values", "train_flags"),
    [
        pytest.param((2.0, 2.0, 2.0, 2.0), (True,) * 4, id="zero_sigma_returns_all_nan"),
        # A single train value yields sigma=NaN from pd.Series.std (ddof=1).
        pytest.param((1.0, 2.0, 3.0), (True, False, False), id="nan_sigma_returns_all_nan"),
    ],
)
def test_compute_train_zscore_degenerate_sigma_returns_all_nan(feature_values, train_flags):
    feature = pd.Series(list(feature_values))
    train_mask = pd.Series(list(train_flags))

    z = compute_train_zscore(feature, train_mask, flip_sign=False)

    assert z.isna().all()


@pytest.mark.parametrize(
    ("z_values", "expected_values"),
    [
        # |z|>1 → sign(z); else 0.
        pytest.param(
            (-2.5, -1.0, 0.0, 0.5, 1.5, 3.0),
            (-1.0, 0.0, 0.0, 0.0, 1.0, 1.0),
            id="produces_sign_outside_threshold",
        ),
        # threshold is strict-greater — boundary values filter to 0.
        pytest.param((-1.0, 1.0), (0.0, 0.0), id="boundary_is_strict"),
    ],
)
def test_apply_threshold_filter_signal(z_values, expected_values):
    z = pd.Series(list(z_values))

    signal = apply_threshold_filter(z, threshold=1.0)

    np.testing.assert_allclose(signal.to_numpy(), np.array(expected_values), atol=1e-12, rtol=0)


def test_apply_threshold_filter_preserves_index():
    z = pd.Series([2.0, -2.0], index=pd.Index(["a", "b"]))

    signal = apply_threshold_filter(z, threshold=1.0)

    assert list(signal.index) == ["a", "b"]
    assert signal.loc["a"] == pytest.approx(1.0, abs=0, rel=0)
    assert signal.loc["b"] == pytest.approx(-1.0, abs=0, rel=0)
