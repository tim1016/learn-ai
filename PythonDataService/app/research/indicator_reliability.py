"""Indicator Reliability Analysis.

Computes Information Coefficient (IC) for a single indicator across
multiple forward horizons to determine its predictive power.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.research.validation.ic import compute_information_coefficient, ICResult
from app.services.dataset_service import calculate_dynamic_indicators

logger = logging.getLogger(__name__)


@dataclass
class HorizonICAnalysis:
    """IC analysis result for one horizon."""

    horizon: int
    mean_ic: float
    t_stat: float
    p_value: float
    nw_t_stat: float | None
    nw_p_value: float | None
    effective_n: int
    interpretation: str
    daily_ic_values: list[float]
    daily_ic_dates: list[str]


def _interpret_ic_result(
    mean_ic: float,
    nw_t_stat: float | None,
    nw_p_value: float | None,
    t_stat: float,
    p_value: float,
) -> str:
    """Generate human-readable interpretation of IC result."""
    # Use Newey-West stats if available, otherwise standard
    used_t = nw_t_stat if nw_t_stat is not None else t_stat
    used_p = nw_p_value if nw_p_value is not None else p_value
    abs_ic = abs(mean_ic)

    # Determine significance level
    if used_p < 0.01:
        sig_level = "highly significant"
    elif used_p < 0.05:
        sig_level = "significant"
    elif used_p < 0.10:
        sig_level = "weakly significant"
    else:
        sig_level = "not significant"

    # Determine strength
    if abs_ic >= 0.05:
        strength = "Strong Signal"
        emoji = "🟢"
    elif abs_ic >= 0.03:
        strength = "Moderate Signal"
        emoji = "🟢"
    elif abs_ic >= 0.02:
        strength = "Weak Signal"
        emoji = "🟡"
    elif abs_ic >= 0.01:
        strength = "Marginal"
        emoji = "🟡"
    else:
        strength = "Noise"
        emoji = "🔴"

    # Combine significance with strength
    if used_p >= 0.10:
        return f"{emoji} {strength} (p={used_p:.3f})"

    if abs_ic >= 0.03 and used_p < 0.05:
        return f"{emoji} RELIABLE ✓ ({sig_level})"
    elif abs_ic >= 0.02 and used_p < 0.10:
        return f"{emoji} {strength} ({sig_level})"
    else:
        return f"{emoji} {strength} ({sig_level})"


def compute_forward_return(
    df: pd.DataFrame,
    horizon: int,
    mask_overnight: bool = True,
) -> pd.Series:
    """Compute forward log returns with variable horizon.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with 'timestamp' (ms) and 'close' columns.
    horizon : int
        Number of bars to look forward.
    mask_overnight : bool
        If True, mask returns that span across trading days.

    Returns
    -------
    pd.Series
        Forward log returns; NaN where horizon crosses day boundary.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)

    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.date

    close = df["close"].values
    dates = df["date"].values
    n = len(df)

    forward_returns = np.full(n, np.nan)

    for i in range(n - horizon):
        if mask_overnight and dates[i] != dates[i + horizon]:
            continue
        if close[i] <= 0 or close[i + horizon] <= 0:
            continue
        forward_returns[i] = np.log(close[i + horizon] / close[i])

    result = pd.Series(forward_returns, index=df.index)
    valid_count = result.notna().sum()
    logger.debug(
        "[Reliability] Forward return (h=%d): %d valid / %d total",
        horizon,
        valid_count,
        n,
    )
    return result


def compute_indicator_reliability(
    df: pd.DataFrame,
    indicator_column: str,
    horizons: list[int],
    include_slope: bool = False,
) -> tuple[list[HorizonICAnalysis], list[HorizonICAnalysis] | None]:
    """Compute IC for a single indicator column across multiple horizons.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV bars with the indicator column already calculated.
    indicator_column : str
        Name of the indicator column to analyze.
    horizons : list[int]
        List of forward horizons (in bars) to analyze.
    include_slope : bool
        If True, also compute IC on the indicator's 1-bar change.

    Returns
    -------
    tuple
        (raw_results, slope_results) where slope_results is None if include_slope=False.
    """
    if indicator_column not in df.columns:
        raise ValueError(f"Indicator column '{indicator_column}' not found in DataFrame")

    # Ensure timestamp is available
    if "timestamp" not in df.columns:
        raise ValueError("DataFrame must have 'timestamp' column")

    df = df.sort_values("timestamp").reset_index(drop=True)

    raw_results: list[HorizonICAnalysis] = []
    slope_results: list[HorizonICAnalysis] | None = [] if include_slope else None

    for horizon in horizons:
        # Compute forward returns for this horizon
        fwd_returns = compute_forward_return(df, horizon, mask_overnight=True)

        # Raw indicator IC
        ic_result = compute_information_coefficient(
            feature_values=df[indicator_column],
            target_returns=fwd_returns,
            timestamps_ms=df["timestamp"],
            correlation_method="spearman",
            min_nw_lag=0,
            rolling_window=None,
        )

        interpretation = _interpret_ic_result(
            ic_result.mean_ic,
            ic_result.nw_t_stat,
            ic_result.nw_p_value,
            ic_result.ic_t_stat,
            ic_result.ic_p_value,
        )

        raw_results.append(
            HorizonICAnalysis(
                horizon=horizon,
                mean_ic=ic_result.mean_ic,
                t_stat=ic_result.ic_t_stat,
                p_value=ic_result.ic_p_value,
                nw_t_stat=ic_result.nw_t_stat,
                nw_p_value=ic_result.nw_p_value,
                effective_n=int(ic_result.effective_n),
                interpretation=interpretation,
                daily_ic_values=ic_result.daily_ic_values,
                daily_ic_dates=ic_result.daily_ic_dates,
            )
        )

        # Slope IC (if requested)
        if include_slope and slope_results is not None:
            slope_col = df[indicator_column].diff()

            slope_ic = compute_information_coefficient(
                feature_values=slope_col,
                target_returns=fwd_returns,
                timestamps_ms=df["timestamp"],
                correlation_method="spearman",
                min_nw_lag=0,
                rolling_window=None,
            )

            slope_interp = _interpret_ic_result(
                slope_ic.mean_ic,
                slope_ic.nw_t_stat,
                slope_ic.nw_p_value,
                slope_ic.ic_t_stat,
                slope_ic.ic_p_value,
            )

            slope_results.append(
                HorizonICAnalysis(
                    horizon=horizon,
                    mean_ic=slope_ic.mean_ic,
                    t_stat=slope_ic.ic_t_stat,
                    p_value=slope_ic.ic_p_value,
                    nw_t_stat=slope_ic.nw_t_stat,
                    nw_p_value=slope_ic.nw_p_value,
                    effective_n=int(slope_ic.effective_n),
                    interpretation=slope_interp,
                    daily_ic_values=slope_ic.daily_ic_values,
                    daily_ic_dates=slope_ic.daily_ic_dates,
                )
            )

    logger.info(
        "[Reliability] Analyzed %s across %d horizons",
        indicator_column,
        len(horizons),
    )

    return raw_results, slope_results


def find_best_horizon(results: list[HorizonICAnalysis]) -> int | None:
    """Find the horizon with the strongest signal (highest |IC| with significance)."""
    if not results:
        return None

    best = None
    best_score = -1.0

    for r in results:
        # Score = |IC| weighted by significance
        p = r.nw_p_value if r.nw_p_value is not None else r.p_value
        if p >= 0.10:
            continue  # Skip non-significant results

        score = abs(r.mean_ic) * (1 - p)  # Higher IC and lower p = better
        if score > best_score:
            best_score = score
            best = r.horizon

    return best


def get_indicator_category(indicator_name: str) -> str | None:
    """Look up the pandas-ta category for an indicator."""
    try:
        import pandas_ta as ta

        for category, indicators in ta.Category.items():
            if indicator_name.lower() in [i.lower() for i in indicators]:
                return category
        return None
    except Exception:
        return None


def format_indicator_display_name(
    indicator_name: str,
    params: dict[str, Any],
) -> str:
    """Generate a human-readable display name like 'RSI (14)' or 'MACD (12, 26, 9)'."""
    name_upper = indicator_name.upper()

    if not params:
        return name_upper

    # Common parameter orderings
    if indicator_name.lower() == "macd":
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        signal = params.get("signal", 9)
        return f"{name_upper} ({fast}, {slow}, {signal})"
    elif indicator_name.lower() == "stoch":
        k = params.get("k", 14)
        d = params.get("d", 3)
        return f"{name_upper} ({k}, {d})"
    elif indicator_name.lower() == "bbands":
        length = params.get("length", 20)
        std = params.get("std", 2.0)
        return f"Bollinger ({length}, {std})"
    elif "length" in params:
        return f"{name_upper} ({params['length']})"
    else:
        # Generic: list all params
        param_str = ", ".join(str(v) for v in params.values())
        return f"{name_upper} ({param_str})"
