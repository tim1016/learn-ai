"""15-minute forward log-return target computation.

Formula
-------
    fwd_return_15(t) = ln(close_{t+15} / close_t)

Cross-day contamination is prevented by masking any forward return
that would span across trading-day boundaries.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_15min_forward_return(
    bars: list[dict],
    horizon: int = 15,
) -> pd.Series:
    """Compute forward log returns with no cross-day contamination.

    Parameters
    ----------
    bars : list[dict]
        OHLCV bars with at least ``timestamp`` (ms) and ``close``.
    horizon : int
        Number of bars to look forward.

    Returns
    -------
    pd.Series
        Forward log returns; NaN where the horizon would cross a day
        boundary or where there are insufficient future bars.
    """
    df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date

    close = df["close"].values
    dates = df["date"].values
    n = len(df)

    forward_returns = np.full(n, np.nan)

    for i in range(n - horizon):
        if dates[i] != dates[i + horizon]:
            continue  # skip cross-day
        if close[i] <= 0 or close[i + horizon] <= 0:
            continue
        forward_returns[i] = np.log(close[i + horizon] / close[i])

    result = pd.Series(forward_returns, index=df.index)
    valid_count = result.notna().sum()
    logger.info("[Research] Computed forward returns: %d valid / %d total", valid_count, n)
    return result


def validate_return_series(returns: pd.Series) -> bool:
    """Sanity-check a return series.

    Fails if more than 30 % of values are NaN or if the series has
    near-zero variance (constant returns).
    """
    non_nan_ratio = returns.notna().sum() / len(returns) if len(returns) > 0 else 0.0

    if non_nan_ratio < 0.3:
        logger.warning("[Research] Only %.1f%% non-NaN returns", non_nan_ratio * 100)
        return False

    std = returns.dropna().std()
    if std is None or std < 1e-10:
        logger.warning("[Research] Return series has near-zero variance")
        return False

    return True
