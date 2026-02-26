"""Quantile analysis for feature monotonicity check.

Divides feature values into N bins (default 5) and computes the mean
forward return in each bin.  A useful predictive feature should show
monotonically increasing (or decreasing) mean returns across quantiles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class QuantileBin:
    """Statistics for a single quantile bucket."""

    bin_number: int
    lower_bound: float
    upper_bound: float
    mean_return: float
    count: int


@dataclass
class QuantileResult:
    """Quantile analysis results."""

    bins: list[QuantileBin] = field(default_factory=list)
    is_monotonic: bool = False
    monotonicity_ratio: float = 0.0


def compute_quantile_analysis(
    feature_values: pd.Series,
    target_returns: pd.Series,
    n_bins: int = 5,
    monotonicity_threshold: float = 0.75,
) -> QuantileResult:
    """Bucket feature into quantiles and check return monotonicity.

    Parameters
    ----------
    feature_values : pd.Series
        Computed feature values.
    target_returns : pd.Series
        15-minute forward log returns.
    n_bins : int
        Number of quantile buckets.
    monotonicity_threshold : float
        Fraction of increasing steps required to consider the feature
        monotonic (e.g. 0.75 means 3 of 4 steps must increase).

    Returns
    -------
    QuantileResult
        Bin details and monotonicity assessment.
    """
    df = pd.DataFrame({"feature": feature_values, "target": target_returns}).dropna()

    if len(df) < n_bins * 5:
        logger.warning("[Research] Not enough data for quantile analysis (%d rows)", len(df))
        return QuantileResult()

    try:
        df["quantile"] = pd.qcut(df["feature"], q=n_bins, labels=False, duplicates="drop")
    except ValueError:
        logger.warning("[Research] Could not create quantile bins (too many duplicates)")
        return QuantileResult()

    bins: list[QuantileBin] = []
    mean_returns: list[float] = []

    for q in sorted(df["quantile"].unique()):
        bin_data = df[df["quantile"] == q]
        mean_ret = float(bin_data["target"].mean())
        bins.append(
            QuantileBin(
                bin_number=int(q),
                lower_bound=float(bin_data["feature"].min()),
                upper_bound=float(bin_data["feature"].max()),
                mean_return=mean_ret,
                count=len(bin_data),
            )
        )
        mean_returns.append(mean_ret)

    if len(mean_returns) < 2:
        return QuantileResult(bins=bins)

    diffs = np.diff(mean_returns)
    increasing_steps = int(np.sum(diffs > 0))
    mono_ratio = increasing_steps / len(diffs)

    # Also check decreasing monotonicity (feature may be inversely predictive)
    decreasing_steps = int(np.sum(diffs < 0))
    dec_ratio = decreasing_steps / len(diffs)
    best_ratio = max(mono_ratio, dec_ratio)

    is_mono = best_ratio >= monotonicity_threshold

    logger.info(
        "[Research] Quantile: %d bins, mono_ratio=%.2f (inc=%.2f dec=%.2f), pass=%s",
        len(bins), best_ratio, mono_ratio, dec_ratio, is_mono,
    )

    return QuantileResult(
        bins=bins,
        is_monotonic=is_mono,
        monotonicity_ratio=best_ratio,
    )
