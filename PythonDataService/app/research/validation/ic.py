"""Information Coefficient (IC) computation.

The IC measures the daily Spearman rank correlation between a feature
and the 15-minute forward log return.

Formulas
--------
    IC_d = Spearman(rank(feature_d), rank(return_d))
    mean_IC = (1/N) * sum(IC_d)
    t = mean_IC / (std_IC / sqrt(N))
    t_NW = mean_IC / sqrt(NW_var / N)   (Newey-West HAC corrected)
    N_eff = N / (1 + 2 * sum(rho_k))    (effective sample size)
"""
from __future__ import annotations

import logging
import math
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
    nw_t_stat: float = 0.0
    nw_p_value: float = 1.0
    effective_n: float = 0.0


def _compute_newey_west_t_stat(
    ic_array: np.ndarray, min_lag: int = 0,
) -> tuple[float, float]:
    """Compute Newey-West HAC-corrected t-stat for the IC series.

    Uses a Bartlett kernel with automatic lag selection to account for
    serial correlation in daily IC values.

    Args:
        ic_array: Array of daily IC values
        min_lag: Minimum lag to enforce (e.g. 5 for daily options data)

    Returns (nw_t_stat, nw_p_value).
    """
    n = len(ic_array)
    if n < 3:
        return 0.0, 1.0

    mean_ic = float(np.mean(ic_array))
    demeaned = ic_array - mean_ic

    # Automatic lag selection: Andrews (1991) rule for Bartlett kernel
    max_lag = max(_andrews_lag(n), min_lag)
    max_lag = min(max_lag, n - 2)

    # Gamma_0: variance
    gamma_0 = float(np.sum(demeaned**2) / n)

    # Weighted autocovariances with Bartlett kernel
    weighted_sum = 0.0
    for j in range(1, max_lag + 1):
        gamma_j = float(np.sum(demeaned[j:] * demeaned[:-j]) / n)
        bartlett_weight = 1 - j / (max_lag + 1)
        weighted_sum += bartlett_weight * gamma_j

    nw_var = gamma_0 + 2 * weighted_sum

    if nw_var <= 1e-20:
        return 0.0, 1.0

    nw_se = math.sqrt(nw_var / n)
    nw_t = mean_ic / nw_se
    nw_p = float(2 * (1 - stats.t.cdf(abs(nw_t), n - 1)))

    return float(nw_t), nw_p


def _andrews_lag(n: int) -> int:
    """Andrews (1991) automatic bandwidth selection for Bartlett kernel."""
    return max(1, int(math.floor(4 * (n / 100) ** (2 / 9))))


def _compute_effective_sample_size(ic_array: np.ndarray, min_lag: int = 0) -> float:
    """Compute effective sample size accounting for autocorrelation.

    N_eff = N / (1 + 2 * sum(rho_k)) where rho_k is the autocorrelation
    at lag k, using Andrews (1991) bandwidth for consistency with Newey-West.
    Summation truncated when autocorrelation drops below 0.05 or turns negative.
    """
    n = len(ic_array)
    if n < 3:
        return float(n)

    mean_ic = np.mean(ic_array)
    demeaned = ic_array - mean_ic
    var = float(np.sum(demeaned**2) / n)

    if var < 1e-20:
        return float(n)

    # Use Andrews (1991) bandwidth, consistent with Newey-West
    max_lag = max(_andrews_lag(n), min_lag)
    max_lag = min(max_lag, n - 2)
    rho_sum = 0.0

    for k in range(1, max_lag + 1):
        rho_k = float(np.sum(demeaned[k:] * demeaned[:-k]) / n) / var
        if rho_k < 0.05:
            break
        rho_sum += rho_k

    denominator = 1 + 2 * rho_sum
    if denominator < 1.0:
        denominator = 1.0

    return n / denominator


def _compute_rolling_ic(
    feature_values: pd.Series,
    target_returns: pd.Series,
    timestamps_ms: pd.Series,
    correlation_method: str,
    rolling_window: int,
    min_nw_lag: int,
) -> ICResult:
    """Compute rolling-window IC for daily/time-series data.

    Instead of grouping by day (cross-sectional), compute Spearman rank
    correlation within rolling windows of ``rolling_window`` observations.
    """
    df = pd.DataFrame({
        "feature": feature_values.values,
        "target": target_returns.values,
        "timestamp": timestamps_ms.values,
    }).dropna(subset=["feature", "target"])

    if len(df) < rolling_window:
        logger.warning(
            "[Research] Not enough data for rolling IC (%d < %d)",
            len(df), rolling_window,
        )
        return ICResult(mean_ic=0.0, ic_t_stat=0.0, ic_p_value=1.0)

    rolling_ics: list[float] = []
    rolling_dates: list[str] = []

    # Step through non-overlapping windows
    step = max(1, rolling_window // 2)  # 50% overlap for more IC observations
    for start in range(0, len(df) - rolling_window + 1, step):
        window = df.iloc[start : start + rolling_window]
        feat = window["feature"]
        tgt = window["target"]

        if feat.std() < 1e-12 or tgt.std() < 1e-12:
            continue

        if correlation_method == "spearman":
            corr, _ = stats.spearmanr(feat, tgt)
        else:
            corr = feat.corr(tgt)

        if np.isnan(corr):
            continue

        rolling_ics.append(float(corr))
        mid_ts = int(window["timestamp"].iloc[rolling_window // 2])
        rolling_dates.append(
            str(pd.to_datetime(mid_ts, unit="ms").date())
        )

    if len(rolling_ics) < 2:
        logger.warning(
            "[Research] Not enough rolling windows with valid ICs (%d)",
            len(rolling_ics),
        )
        return ICResult(mean_ic=0.0, ic_t_stat=0.0, ic_p_value=1.0)

    ic_array = np.array(rolling_ics)
    mean_ic = float(np.mean(ic_array))
    std_ic = float(np.std(ic_array, ddof=1))
    n = len(ic_array)

    if std_ic > 1e-10:
        t_stat = mean_ic / (std_ic / np.sqrt(n))
        p_value = float(2 * (1 - stats.t.cdf(abs(t_stat), n - 1)))
    else:
        t_stat = 0.0
        p_value = 1.0

    nw_t_stat, nw_p_value = _compute_newey_west_t_stat(ic_array, min_lag=min_nw_lag)
    effective_n = _compute_effective_sample_size(ic_array, min_lag=min_nw_lag)

    logger.info(
        "[Research] Rolling IC (w=%d): mean=%.4f, t=%.4f (NW=%.4f), "
        "p=%.4f (NW=%.4f), windows=%d (effective=%.0f)",
        rolling_window, mean_ic, t_stat, nw_t_stat,
        p_value, nw_p_value, n, effective_n,
    )

    return ICResult(
        mean_ic=mean_ic,
        ic_t_stat=t_stat,
        ic_p_value=p_value,
        daily_ic_values=rolling_ics,
        daily_ic_dates=rolling_dates,
        nw_t_stat=nw_t_stat,
        nw_p_value=nw_p_value,
        effective_n=effective_n,
    )


def compute_information_coefficient(
    feature_values: pd.Series,
    target_returns: pd.Series,
    timestamps_ms: pd.Series,
    correlation_method: str = "spearman",
    min_nw_lag: int = 0,
    rolling_window: int | None = None,
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
    min_nw_lag : int
        Minimum Newey-West lag to enforce.
    rolling_window : int | None
        If set, use rolling-window IC (for daily time-series data)
        instead of daily cross-sectional grouping.

    Returns
    -------
    ICResult
        Mean IC, t-stat, p-value, and per-day IC values.
    """
    if rolling_window is not None:
        return _compute_rolling_ic(
            feature_values, target_returns, timestamps_ms,
            correlation_method, rolling_window, min_nw_lag,
        )

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

    # Newey-West corrected t-stat (accounts for serial correlation)
    nw_t_stat, nw_p_value = _compute_newey_west_t_stat(ic_array, min_lag=min_nw_lag)
    effective_n = _compute_effective_sample_size(ic_array, min_lag=min_nw_lag)

    logger.info(
        "[Research] IC: mean=%.4f, t=%.4f (NW=%.4f), p=%.4f (NW=%.4f), "
        "days=%d (effective=%.0f)",
        mean_ic, t_stat, nw_t_stat, p_value, nw_p_value, n, effective_n,
    )

    return ICResult(
        mean_ic=mean_ic,
        ic_t_stat=t_stat,
        ic_p_value=p_value,
        daily_ic_values=daily_ics,
        daily_ic_dates=daily_dates,
        nw_t_stat=nw_t_stat,
        nw_p_value=nw_p_value,
        effective_n=effective_n,
    )
