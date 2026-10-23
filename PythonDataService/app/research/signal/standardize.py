"""Z-score standardization and threshold filtering.

Formula: z = (x − mean_train) / std_train; threshold filter = z > threshold → +1, z < −threshold → −1, else 0.
Reference: Standard sample z-score (ddof=1); threshold filter is an internal signal rule.
Canonical implementation: app/research/signal/standardize.py
Validated against: PythonDataService/tests/fixtures/test_research_fixtures.py::TestRP004SignalZscore (RP-004, z-score only, atol=1e-9, rtol=0).

The threshold filter has behavior coverage in tests/research/test_signal_engine.py::TestThresholdFilter; no external fixture is claimed for it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_train_zscore(
    feature: pd.Series,
    train_mask: pd.Series,
    flip_sign: bool,
) -> pd.Series:
    """Z-score using train-period statistics only.

    z_t = (f_t - mu_train) / sigma_train. Flip sign if negative IC.
    """
    train_vals = feature[train_mask].dropna()
    mu = train_vals.mean()
    sigma = train_vals.std()

    if sigma < 1e-10 or np.isnan(sigma):
        return pd.Series(np.nan, index=feature.index)

    z = (feature - mu) / sigma
    if flip_sign:
        z = -z

    return z


def apply_threshold_filter(z_scores: pd.Series, threshold: float) -> pd.Series:
    """trade_t = sign(z_t) if |z_t| > threshold, else 0."""
    signal = np.where(
        np.abs(z_scores) > threshold,
        np.sign(z_scores),
        0.0,
    )
    return pd.Series(signal, index=z_scores.index)
