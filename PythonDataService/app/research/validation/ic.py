"""Information Coefficient (IC) computation.

The IC measures the daily Spearman rank correlation between a feature
and the 15-minute forward log return.

Formulas
--------
    IC_d = Spearman(rank(feature_d), rank(return_d))
    mean_IC = (1/N) * sum(IC_d)
    t = mean_IC / (std_IC / sqrt(N))
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


@dataclass
class ICResult:
    """Information Coefficient validation results."""

    mean_ic: float
    ic_t_stat: float
    ic_p_value: float
    daily_ic_values: list[float] = field(default_factory=list)
    daily_ic_dates: list[str] = field(default_factory=list)


def compute_information_coefficient(
    feature_values: pd.Series,
    target_returns: pd.Series,
    timestamps_ms: pd.Series,
    correlation_method: str = "spearman",
) -> ICResult:
    """Compute daily Information Coefficient.

    Parameters
    ----------
    feature_values : pd.Series
        Computed feature values (aligned with bars).
    target_returns : pd.Series
        15-minute forward log returns (aligned with bars).
    timestamps_ms : pd.Series
        Bar timestamps in milliseconds since epoch.
    correlation_method : str
        Correlation method (default ``"spearman"``).

    Returns
    -------
    ICResult
        Mean IC, t-stat, p-value, and per-day IC values.
    """
    df = pd.DataFrame(
        {
            "feature": feature_values.values,
            "target": target_returns.values,
            "date": pd.to_datetime(timestamps_ms, unit="ms").dt.date,
        }
    )

    daily_ics: list[float] = []
    daily_dates: list[str] = []

    for date, day_df in df.groupby("date"):
        clean = day_df[["feature", "target"]].dropna()
        if len(clean) < 5:
            continue

        if clean["feature"].std() < 1e-12 or clean["target"].std() < 1e-12:
            continue

        if correlation_method == "spearman":
            corr, _ = stats.spearmanr(clean["feature"], clean["target"])
        else:
            corr = clean["feature"].corr(clean["target"])

        if np.isnan(corr):
            continue

        daily_ics.append(float(corr))
        daily_dates.append(str(date))

    if len(daily_ics) < 2:
        logger.warning("[Research] Not enough days with valid ICs (%d)", len(daily_ics))
        return ICResult(mean_ic=0.0, ic_t_stat=0.0, ic_p_value=1.0)

    ic_array = np.array(daily_ics)
    mean_ic = float(np.mean(ic_array))
    std_ic = float(np.std(ic_array, ddof=1))
    n = len(ic_array)

    if std_ic > 1e-10:
        t_stat = mean_ic / (std_ic / np.sqrt(n))
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), n - 1)))
    else:
        t_stat = 0.0
        p_value = 1.0

    logger.info(
        "[Research] IC: mean=%.4f, t=%.4f, p=%.4f, days=%d",
        mean_ic, t_stat, p_value, n,
    )

    return ICResult(
        mean_ic=mean_ic,
        ic_t_stat=t_stat,
        ic_p_value=p_value,
        daily_ic_values=daily_ics,
        daily_ic_dates=daily_dates,
    )
