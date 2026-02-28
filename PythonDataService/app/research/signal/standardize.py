"""Z-score standardization and threshold filtering."""
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
