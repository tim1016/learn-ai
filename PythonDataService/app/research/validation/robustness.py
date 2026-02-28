"""Robustness analysis for feature validation.

Computes rolling window stability metrics, regime segmentation,
and train/test split to detect regime bias and overfitting.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

MIN_MONTHLY_OBSERVATIONS = 3
MIN_REGIME_OBSERVATIONS = 5
MIN_MONTHS_FOR_ANALYSIS = 2


@dataclass
class MonthlyICBreakdown:
    """IC statistics for a single calendar month."""

    month: str
    mean_ic: float
    t_stat: float
    observation_count: int


@dataclass
class RollingTStatPoint:
    """Single point in the rolling 6-month smoothed t-stat series."""

    month: str
    t_stat_smoothed: float


@dataclass
class RegimeICResult:
    """IC computed within a specific market regime."""

    regime_label: str
    mean_ic: float
    t_stat: float
    observation_count: int


@dataclass
class TrainTestSplit:
    """Chronological train/test split IC comparison."""

    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_mean_ic: float
    train_t_stat: float
    train_days: int
    test_mean_ic: float
    test_t_stat: float
    test_days: int
    overfit_flag: bool


@dataclass
class RobustnessResult:
    """Complete robustness analysis output."""

    monthly_breakdown: list[MonthlyICBreakdown] = field(default_factory=list)
    pct_positive_months: float = 0.0
    pct_significant_months: float = 0.0
    best_month_ic: float = 0.0
    worst_month_ic: float = 0.0
    stability_label: str = "Unknown"

    rolling_t_stat: list[RollingTStatPoint] = field(default_factory=list)

    volatility_regimes: list[RegimeICResult] = field(default_factory=list)
    trend_regimes: list[RegimeICResult] = field(default_factory=list)

    train_test: TrainTestSplit | None = None


def _compute_ic_stats(ic_array: np.ndarray) -> tuple[float, float]:
    """Compute mean IC and t-stat for an array of IC values."""
    if len(ic_array) < 2:
        return float(np.mean(ic_array)) if len(ic_array) == 1 else 0.0, 0.0

    mean_ic = float(np.mean(ic_array))
    std_ic = float(np.std(ic_array, ddof=1))

    if std_ic > 1e-10:
        t_stat = mean_ic / (std_ic / np.sqrt(len(ic_array)))
    else:
        t_stat = 0.0

    return mean_ic, float(t_stat)


def _stability_label(pct_positive: float) -> str:
    """Map % positive months to a stability classification."""
    if pct_positive >= 0.80:
        return "Suspicious"
    if pct_positive >= 0.70:
        return "Strong"
    if pct_positive >= 0.60:
        return "Tradeable"
    if pct_positive >= 0.50:
        return "Weak"
    return "Noise"


def compute_monthly_ic_breakdown(
    daily_ic_values: list[float],
    daily_ic_dates: list[str],
    significance_level: float = 0.10,
) -> tuple[list[MonthlyICBreakdown], float, float, float, float, str]:
    """Group daily ICs by month, compute per-month mean IC and t-stat.

    Parameters
    ----------
    daily_ic_values : list[float]
        One IC per trading day.
    daily_ic_dates : list[str]
        YYYY-MM-DD dates corresponding to IC values.
    significance_level : float
        p-value threshold for monthly significance.

    Returns
    -------
    tuple
        (monthly_breakdown, pct_positive, pct_significant,
         best_month_ic, worst_month_ic, stability_label)
    """
    if len(daily_ic_values) < MIN_MONTHS_FOR_ANALYSIS:
        return [], 0.0, 0.0, 0.0, 0.0, "Unknown"

    df = pd.DataFrame({
        "ic": daily_ic_values,
        "date": pd.to_datetime(daily_ic_dates),
    })
    df["month"] = df["date"].dt.to_period("M").astype(str)

    monthly: list[MonthlyICBreakdown] = []
    for month_str, group in df.groupby("month", sort=True):
        ics = group["ic"].values
        if len(ics) < MIN_MONTHLY_OBSERVATIONS:
            continue
        mean_ic, t_stat = _compute_ic_stats(ics)
        monthly.append(MonthlyICBreakdown(
            month=str(month_str),
            mean_ic=mean_ic,
            t_stat=t_stat,
            observation_count=len(ics),
        ))

    if not monthly:
        return [], 0.0, 0.0, 0.0, 0.0, "Unknown"

    positive_count = sum(1 for m in monthly if m.mean_ic > 0)
    significant_count = sum(1 for m in monthly if abs(m.t_stat) > 1.65)
    total = len(monthly)

    pct_positive = positive_count / total
    pct_significant = significant_count / total
    mean_ics = [m.mean_ic for m in monthly]
    best_month_ic = max(mean_ics)
    worst_month_ic = min(mean_ics)
    label = _stability_label(pct_positive)

    logger.info(
        "[Robustness] Monthly: %d months, %.0f%% positive, %.0f%% significant, label=%s",
        total, pct_positive * 100, pct_significant * 100, label,
    )

    return monthly, pct_positive, pct_significant, best_month_ic, worst_month_ic, label


def compute_rolling_t_stat(
    monthly_breakdown: list[MonthlyICBreakdown],
    window: int = 6,
) -> list[RollingTStatPoint]:
    """Compute rolling average of monthly t-stats.

    Parameters
    ----------
    monthly_breakdown : list[MonthlyICBreakdown]
        Output from compute_monthly_ic_breakdown.
    window : int
        Rolling window size in months.

    Returns
    -------
    list[RollingTStatPoint]
        One smoothed t-stat per month (starting from month ``window``).
    """
    if len(monthly_breakdown) < window:
        return []

    t_stats = [m.t_stat for m in monthly_breakdown]
    months = [m.month for m in monthly_breakdown]

    result: list[RollingTStatPoint] = []
    for i in range(window - 1, len(t_stats)):
        window_slice = t_stats[i - window + 1 : i + 1]
        smoothed = float(np.mean(window_slice))
        result.append(RollingTStatPoint(month=months[i], t_stat_smoothed=smoothed))

    return result


def compute_regime_analysis(
    daily_ic_values: list[float],
    daily_ic_dates: list[str],
    bars: list[dict],
) -> tuple[list[RegimeICResult], list[RegimeICResult]]:
    """Split ICs by volatility and trend regimes.

    Parameters
    ----------
    daily_ic_values : list[float]
        One IC per trading day.
    daily_ic_dates : list[str]
        YYYY-MM-DD dates corresponding to IC values.
    bars : list[dict]
        OHLCV bars with timestamp (ms), close, volume.

    Returns
    -------
    tuple
        (volatility_regimes, trend_regimes)
    """
    if len(daily_ic_values) < MIN_REGIME_OBSERVATIONS:
        return [], []

    bar_df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)
    bar_df["date"] = pd.to_datetime(bar_df["timestamp"], unit="ms").dt.date
    bar_df["log_return"] = np.log(bar_df["close"] / bar_df["close"].shift(1))

    # Build daily summary from intraday bars
    daily_summary = (
        bar_df.groupby("date")
        .agg(
            close=("close", "last"),
            realized_vol=("log_return", "std"),
        )
        .reset_index()
    )
    daily_summary["date_str"] = daily_summary["date"].astype(str)

    # Build IC lookup
    ic_df = pd.DataFrame({
        "date_str": daily_ic_dates,
        "ic": daily_ic_values,
    })

    merged = ic_df.merge(daily_summary, on="date_str", how="inner")

    if len(merged) < MIN_REGIME_OBSERVATIONS:
        return [], []

    # --- Volatility Regimes ---
    vol_regimes: list[RegimeICResult] = []
    vol_series = merged["realized_vol"].dropna()
    if len(vol_series) >= MIN_REGIME_OBSERVATIONS:
        p33 = vol_series.quantile(0.33)
        p67 = vol_series.quantile(0.67)

        for label, mask in [
            ("Low Vol", merged["realized_vol"] <= p33),
            ("Normal Vol", (merged["realized_vol"] > p33) & (merged["realized_vol"] <= p67)),
            ("High Vol", merged["realized_vol"] > p67),
        ]:
            regime_ics = merged.loc[mask, "ic"].values
            if len(regime_ics) >= MIN_REGIME_OBSERVATIONS:
                mean_ic, t_stat = _compute_ic_stats(regime_ics)
                vol_regimes.append(RegimeICResult(
                    regime_label=label,
                    mean_ic=mean_ic,
                    t_stat=t_stat,
                    observation_count=len(regime_ics),
                ))

    # --- Trend Regimes ---
    trend_regimes: list[RegimeICResult] = []
    if len(merged) >= 20:
        merged_sorted = merged.sort_values("date_str").reset_index(drop=True)
        closes = merged_sorted["close"].values
        ma_20 = pd.Series(closes).rolling(window=20, min_periods=20).mean()
        ma_slope = ma_20.diff(5) / 5

        slope_threshold = ma_slope.dropna().abs().median() * 0.5 if len(ma_slope.dropna()) > 0 else 0.001

        merged_sorted["trend"] = "Sideways"
        merged_sorted.loc[ma_slope > slope_threshold, "trend"] = "Trending Up"
        merged_sorted.loc[ma_slope < -slope_threshold, "trend"] = "Trending Down"

        for label in ["Trending Up", "Sideways", "Trending Down"]:
            regime_ics = merged_sorted.loc[merged_sorted["trend"] == label, "ic"].values
            if len(regime_ics) >= MIN_REGIME_OBSERVATIONS:
                mean_ic, t_stat = _compute_ic_stats(regime_ics)
                trend_regimes.append(RegimeICResult(
                    regime_label=label,
                    mean_ic=mean_ic,
                    t_stat=t_stat,
                    observation_count=len(regime_ics),
                ))

    logger.info(
        "[Robustness] Regimes: %d vol regimes, %d trend regimes",
        len(vol_regimes), len(trend_regimes),
    )

    return vol_regimes, trend_regimes


def compute_train_test_split(
    daily_ic_values: list[float],
    daily_ic_dates: list[str],
    train_ratio: float = 0.70,
) -> TrainTestSplit | None:
    """Chronological 70/30 split of daily ICs.

    Parameters
    ----------
    daily_ic_values : list[float]
        One IC per trading day.
    daily_ic_dates : list[str]
        YYYY-MM-DD dates.
    train_ratio : float
        Fraction of data for training (default 0.70).

    Returns
    -------
    TrainTestSplit or None
        None if insufficient data for a meaningful split.
    """
    n = len(daily_ic_values)
    if n < 10:
        return None

    split_idx = int(n * train_ratio)
    if split_idx < 5 or (n - split_idx) < 5:
        return None

    train_ics = np.array(daily_ic_values[:split_idx])
    test_ics = np.array(daily_ic_values[split_idx:])
    train_dates = daily_ic_dates[:split_idx]
    test_dates = daily_ic_dates[split_idx:]

    train_mean_ic, train_t_stat = _compute_ic_stats(train_ics)
    test_mean_ic, test_t_stat = _compute_ic_stats(test_ics)

    overfit_flag = (
        (abs(train_mean_ic) > 2 * abs(test_mean_ic) and abs(train_mean_ic) > 0.005)
        or (abs(train_t_stat) > 1.65 and abs(test_t_stat) < 1.0)
    )

    logger.info(
        "[Robustness] Train/Test: train IC=%.4f (t=%.2f, %d days), "
        "test IC=%.4f (t=%.2f, %d days), overfit=%s",
        train_mean_ic, train_t_stat, len(train_ics),
        test_mean_ic, test_t_stat, len(test_ics), overfit_flag,
    )

    return TrainTestSplit(
        train_start=train_dates[0],
        train_end=train_dates[-1],
        test_start=test_dates[0],
        test_end=test_dates[-1],
        train_mean_ic=train_mean_ic,
        train_t_stat=train_t_stat,
        train_days=len(train_ics),
        test_mean_ic=test_mean_ic,
        test_t_stat=test_t_stat,
        test_days=len(test_ics),
        overfit_flag=overfit_flag,
    )


def compute_robustness(
    daily_ic_values: list[float],
    daily_ic_dates: list[str],
    bars: list[dict],
) -> RobustnessResult:
    """Orchestrate all robustness computations.

    Parameters
    ----------
    daily_ic_values : list[float]
        One IC per trading day (from ic.py).
    daily_ic_dates : list[str]
        YYYY-MM-DD dates (from ic.py).
    bars : list[dict]
        OHLCV bars for regime detection.

    Returns
    -------
    RobustnessResult
        Complete robustness analysis.
    """
    result = RobustnessResult()

    if len(daily_ic_values) < MIN_MONTHS_FOR_ANALYSIS:
        logger.warning("[Robustness] Not enough IC observations (%d) for analysis", len(daily_ic_values))
        return result

    # Monthly breakdown
    (
        result.monthly_breakdown,
        result.pct_positive_months,
        result.pct_significant_months,
        result.best_month_ic,
        result.worst_month_ic,
        result.stability_label,
    ) = compute_monthly_ic_breakdown(daily_ic_values, daily_ic_dates)

    # Rolling t-stat
    result.rolling_t_stat = compute_rolling_t_stat(result.monthly_breakdown, window=6)

    # Regime analysis
    result.volatility_regimes, result.trend_regimes = compute_regime_analysis(
        daily_ic_values, daily_ic_dates, bars,
    )

    # Train/Test split
    result.train_test = compute_train_test_split(daily_ic_values, daily_ic_dates)

    logger.info(
        "[Robustness] Complete: %d months, %d rolling points, %d vol regimes, %d trend regimes, train/test=%s",
        len(result.monthly_breakdown),
        len(result.rolling_t_stat),
        len(result.volatility_regimes),
        len(result.trend_regimes),
        result.train_test is not None,
    )

    return result
