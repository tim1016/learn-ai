"""Indicator Reliability Analysis.

Computes Information Coefficient (IC) for a single indicator across
multiple forward horizons with in-sample/out-of-sample validation,
multiple testing correction, and random baseline comparison.

This is TIME-SERIES IC for a single asset, NOT cross-sectional factor IC.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.research.validation.ic import compute_information_coefficient, ICResult

logger = logging.getLogger(__name__)

# Constants
TRAIN_RATIO = 0.70
RANDOM_SIMULATIONS = 100
MIN_OOS_OBSERVATIONS = 10


@dataclass
class HorizonICAnalysis:
    """IC analysis result for one horizon with IS/OOS split."""

    horizon: int
    # In-sample
    is_mean_ic: float
    is_t_stat: float
    is_p_value: float
    is_nw_t_stat: float | None
    is_nw_p_value: float | None
    is_effective_n: int
    is_daily_ic_values: list[float] = field(default_factory=list)
    is_daily_ic_dates: list[str] = field(default_factory=list)
    # Out-of-sample
    oos_mean_ic: float | None = None
    oos_t_stat: float | None = None
    oos_p_value: float | None = None
    oos_effective_n: int | None = None
    oos_retention: float | None = None
    # Multiple testing corrections
    bonferroni_p: float = 1.0
    fdr_p: float = 1.0
    # Random baseline
    random_baseline_mean: float = 0.0
    random_baseline_std: float = 0.0
    ic_vs_random_zscore: float = 0.0
    # Interpretations (legacy free-text)
    is_interpretation: str = "Unknown"
    oos_interpretation: str | None = None
    # Stability (directional consistency of daily ICs)
    is_hit_rate: float = 0.0
    is_daily_ic_std: float = 0.0
    # Verdict labels (bucketed for scannability)
    strength_label: str = "Noise"
    stability_label: str = "Low"
    direction_label: str = "None"
    # OOS delta: +24% = OOS 24% stronger; -40% = 40% weaker. None if OOS missing.
    retention_delta_pct: float | None = None
    # Slope decision flags (only populated for slope variant, by the router)
    slope_adds_value: bool | None = None
    slope_recommended: bool | None = None


# Bucket thresholds for verdict labels. Tuned to match human intuition for
# time-series IC on intraday data: below 0.03 is noise territory, 0.12+ is
# exceptional. Stability uses hit rate (sign consistency of daily ICs).
_STRENGTH_BUCKETS = [(0.12, "Strong"), (0.07, "Moderate"), (0.03, "Weak")]
_STABILITY_BUCKETS = [(0.58, "High"), (0.52, "Moderate")]
_DIRECTION_THRESHOLD = 0.02


def compute_strength_label(abs_ic: float) -> str:
    """Bucket |IC| into Noise / Weak / Moderate / Strong."""
    for threshold, label in _STRENGTH_BUCKETS:
        if abs_ic >= threshold:
            return label
    return "Noise"


def compute_stability_label(hit_rate: float) -> str:
    """Bucket hit rate into Low / Moderate / High."""
    for threshold, label in _STABILITY_BUCKETS:
        if hit_rate >= threshold:
            return label
    return "Low"


def compute_direction_label(mean_ic: float) -> str:
    """Sign of IC → Mean-Reversion / Momentum / None.

    For oscillator-style indicators (RSI, Stoch) a negative IC means high
    values predict low future returns — that's mean reversion. Positive IC =
    momentum (high values → high future returns).
    """
    if mean_ic < -_DIRECTION_THRESHOLD:
        return "Mean-Reversion"
    if mean_ic > _DIRECTION_THRESHOLD:
        return "Momentum"
    return "None"


def compute_retention_delta_pct(
    is_mean_ic: float,
    oos_mean_ic: float | None,
) -> float | None:
    """(|OOS| / |IS| - 1) * 100. None when OOS missing or IS near-zero."""
    if oos_mean_ic is None or abs(is_mean_ic) < 1e-10:
        return None
    return (abs(oos_mean_ic) / abs(is_mean_ic) - 1.0) * 100.0


def compute_slope_decisions(
    raw: HorizonICAnalysis,
    slope: HorizonICAnalysis,
) -> tuple[bool, bool | None]:
    """Decide if the slope variant adds value and is recommended.

    Returns (slope_adds_value, slope_recommended). ``slope_recommended`` is
    None when OOS data is unavailable — can't recommend without validation.
    """
    raw_abs = abs(raw.is_mean_ic)
    slope_abs = abs(slope.is_mean_ic)

    if raw_abs < 1e-10:
        adds_value = slope_abs > _DIRECTION_THRESHOLD
    else:
        adds_value = slope_abs > raw_abs * 1.20 and slope.fdr_p < raw.fdr_p

    if slope.oos_p_value is None:
        return adds_value, None

    oos_passes = slope.oos_p_value < 0.10 or (
        slope.oos_retention is not None and slope.oos_retention >= 0.6
    )
    return adds_value, adds_value and oos_passes


def apply_multiple_testing_correction(
    p_values: list[float],
) -> tuple[list[float], list[float]]:
    """Apply Bonferroni and FDR (Benjamini-Hochberg) corrections.

    Parameters
    ----------
    p_values : list[float]
        Raw p-values from multiple tests.

    Returns
    -------
    tuple
        (bonferroni_corrected, fdr_corrected) lists.
    """
    n = len(p_values)
    if n == 0:
        return [], []

    # Bonferroni: multiply each p by n, cap at 1.0
    bonferroni = [min(p * n, 1.0) for p in p_values]

    # FDR (Benjamini-Hochberg)
    # 1. Sort p-values and track original indices
    sorted_indices = sorted(range(n), key=lambda i: p_values[i])
    fdr = [0.0] * n

    # 2. Compute adjusted p-values: p * n / rank
    for rank, idx in enumerate(sorted_indices, 1):
        fdr[idx] = min(p_values[idx] * n / rank, 1.0)

    # 3. Enforce monotonicity (going backwards through sorted order)
    for i in range(n - 2, -1, -1):
        idx = sorted_indices[i]
        next_idx = sorted_indices[i + 1]
        fdr[idx] = min(fdr[idx], fdr[next_idx])

    return bonferroni, fdr


def compute_random_baseline_ic(
    df: pd.DataFrame,
    horizon: int,
    n_simulations: int = RANDOM_SIMULATIONS,
) -> tuple[float, float]:
    """Compute mean and std of IC for random shuffled signals.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with timestamp and close columns.
    horizon : int
        Forward horizon for return calculation.
    n_simulations : int
        Number of random permutations to run.

    Returns
    -------
    tuple
        (mean_random_ic, std_random_ic)
    """
    fwd_returns = compute_forward_return(df, horizon)
    valid_mask = fwd_returns.notna()
    n_valid = valid_mask.sum()

    if n_valid < 20:
        return 0.0, 0.01  # Return small std to avoid division issues

    random_ics: list[float] = []

    for _ in range(n_simulations):
        # Generate random signal (shuffled indices)
        random_signal = pd.Series(
            np.random.permutation(len(df)),
            index=df.index,
        )

        ic_result = compute_information_coefficient(
            feature_values=random_signal,
            target_returns=fwd_returns,
            timestamps_ms=df["timestamp"],
            correlation_method="spearman",
        )
        random_ics.append(ic_result.mean_ic)

    mean_ic = float(np.mean(random_ics))
    std_ic = float(np.std(random_ics))

    # Ensure non-zero std for z-score calculation
    if std_ic < 1e-10:
        std_ic = 0.01

    return mean_ic, std_ic


def _interpret_ic_result(
    mean_ic: float,
    p_value: float,
    fdr_p: float,
    is_oos: bool = False,
    oos_retention: float | None = None,
) -> str:
    """Generate interpretation label based on IC and p-values.

    Uses corrected p-values for more honest assessment.
    """
    abs_ic = abs(mean_ic)

    if is_oos:
        # OOS interpretation
        if p_value >= 0.10:
            return "No OOS Signal"
        if oos_retention is not None and oos_retention >= 0.6 and p_value < 0.05:
            return "OOS Validated"
        if oos_retention is not None and oos_retention >= 0.4:
            return "Partial OOS Retention"
        if p_value < 0.10:
            return "Weak OOS Signal"
        return "OOS Degraded"
    else:
        # In-sample interpretation (use FDR-corrected p-value)
        if fdr_p >= 0.10:
            return "No Signal (FDR p >= 0.10)"
        if abs_ic >= 0.03 and fdr_p < 0.05:
            return "Significant (FDR p < 0.05)"
        if abs_ic >= 0.02 and fdr_p < 0.10:
            return "Marginal (FDR p < 0.10)"
        if p_value < 0.05:
            return "In-Sample Only (needs OOS)"
        return "Weak Signal"


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

    return pd.Series(forward_returns, index=df.index)


def compute_indicator_reliability_with_oos(
    df: pd.DataFrame,
    indicator_column: str,
    horizons: list[int],
    include_slope: bool = False,
    train_ratio: float = TRAIN_RATIO,
    random_simulations: int = RANDOM_SIMULATIONS,
) -> tuple[list[HorizonICAnalysis], list[HorizonICAnalysis] | None, dict]:
    """Compute IC with IS/OOS split, multiple testing correction, and random baseline.

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
    train_ratio : float
        Fraction of data for in-sample (default 0.70).
    random_simulations : int
        Number of random shuffles for baseline (default 100).

    Returns
    -------
    tuple
        (raw_results, slope_results, metadata_dict)
    """
    if indicator_column not in df.columns:
        raise ValueError(f"Indicator column '{indicator_column}' not found")

    if "timestamp" not in df.columns:
        raise ValueError("DataFrame must have 'timestamp' column")

    df = df.sort_values("timestamp").reset_index(drop=True)
    n = len(df)

    # Split into train/test
    split_idx = int(n * train_ratio)
    train_df = df.iloc[:split_idx].reset_index(drop=True)
    test_df = df.iloc[split_idx:].reset_index(drop=True)

    # Determine date ranges
    train_start = str(pd.to_datetime(train_df["timestamp"].iloc[0], unit="ms").date())
    train_end = str(pd.to_datetime(train_df["timestamp"].iloc[-1], unit="ms").date())
    test_start = str(pd.to_datetime(test_df["timestamp"].iloc[0], unit="ms").date()) if len(test_df) > 0 else None
    test_end = str(pd.to_datetime(test_df["timestamp"].iloc[-1], unit="ms").date()) if len(test_df) > 0 else None

    metadata = {
        "train_start": train_start,
        "train_end": train_end,
        "test_start": test_start,
        "test_end": test_end,
        "train_bars": len(train_df),
        "test_bars": len(test_df),
        "train_ratio": train_ratio,
    }

    raw_results: list[HorizonICAnalysis] = []
    slope_results: list[HorizonICAnalysis] | None = [] if include_slope else None

    # First pass: compute IS IC for all horizons to get p-values
    is_p_values: list[float] = []

    for horizon in horizons:
        # In-sample IC
        is_fwd_returns = compute_forward_return(train_df, horizon, mask_overnight=True)
        is_ic = compute_information_coefficient(
            feature_values=train_df[indicator_column],
            target_returns=is_fwd_returns,
            timestamps_ms=train_df["timestamp"],
            correlation_method="spearman",
        )

        # Use NW p-value if available, else standard
        is_p = is_ic.nw_p_value if is_ic.nw_p_value is not None else is_ic.ic_p_value
        is_p_values.append(is_p)

    # Apply multiple testing corrections
    bonferroni_ps, fdr_ps = apply_multiple_testing_correction(is_p_values)

    # Second pass: full analysis with corrections
    for i, horizon in enumerate(horizons):
        # In-sample IC (recompute - could cache but kept simple)
        is_fwd_returns = compute_forward_return(train_df, horizon, mask_overnight=True)
        is_ic = compute_information_coefficient(
            feature_values=train_df[indicator_column],
            target_returns=is_fwd_returns,
            timestamps_ms=train_df["timestamp"],
            correlation_method="spearman",
        )

        # Out-of-sample IC
        oos_mean_ic = None
        oos_t_stat = None
        oos_p_value = None
        oos_effective_n = None
        oos_retention = None
        oos_interpretation = None

        if len(test_df) >= MIN_OOS_OBSERVATIONS:
            oos_fwd_returns = compute_forward_return(test_df, horizon, mask_overnight=True)
            oos_ic = compute_information_coefficient(
                feature_values=test_df[indicator_column],
                target_returns=oos_fwd_returns,
                timestamps_ms=test_df["timestamp"],
                correlation_method="spearman",
            )

            oos_mean_ic = oos_ic.mean_ic
            oos_t_stat = oos_ic.ic_t_stat
            oos_p_value = oos_ic.ic_p_value
            oos_effective_n = int(oos_ic.effective_n)

            # OOS retention: |oos_ic| / |is_ic|
            if abs(is_ic.mean_ic) > 1e-10:
                oos_retention = abs(oos_mean_ic) / abs(is_ic.mean_ic)
                # Cap at reasonable values
                oos_retention = min(oos_retention, 2.0)
            else:
                oos_retention = 0.0

            oos_interpretation = _interpret_ic_result(
                oos_mean_ic, oos_p_value, fdr_ps[i],
                is_oos=True, oos_retention=oos_retention,
            )

        # Random baseline (on train data)
        random_mean, random_std = compute_random_baseline_ic(
            train_df, horizon, n_simulations=random_simulations
        )
        z_score = (is_ic.mean_ic - random_mean) / random_std if random_std > 1e-10 else 0.0

        # IS interpretation
        is_interpretation = _interpret_ic_result(
            is_ic.mean_ic, is_ic.ic_p_value, fdr_ps[i], is_oos=False
        )

        raw_results.append(
            HorizonICAnalysis(
                horizon=horizon,
                is_mean_ic=is_ic.mean_ic,
                is_t_stat=is_ic.ic_t_stat,
                is_p_value=is_ic.ic_p_value,
                is_nw_t_stat=is_ic.nw_t_stat,
                is_nw_p_value=is_ic.nw_p_value,
                is_effective_n=int(is_ic.effective_n),
                is_daily_ic_values=is_ic.daily_ic_values,
                is_daily_ic_dates=is_ic.daily_ic_dates,
                oos_mean_ic=oos_mean_ic,
                oos_t_stat=oos_t_stat,
                oos_p_value=oos_p_value,
                oos_effective_n=oos_effective_n,
                oos_retention=oos_retention,
                bonferroni_p=bonferroni_ps[i],
                fdr_p=fdr_ps[i],
                random_baseline_mean=random_mean,
                random_baseline_std=random_std,
                ic_vs_random_zscore=z_score,
                is_interpretation=is_interpretation,
                oos_interpretation=oos_interpretation,
                is_hit_rate=is_ic.hit_rate,
                is_daily_ic_std=is_ic.daily_ic_std,
                strength_label=compute_strength_label(abs(is_ic.mean_ic)),
                stability_label=compute_stability_label(is_ic.hit_rate),
                direction_label=compute_direction_label(is_ic.mean_ic),
                retention_delta_pct=compute_retention_delta_pct(is_ic.mean_ic, oos_mean_ic),
            )
        )

        # Slope analysis if requested
        if include_slope and slope_results is not None:
            slope_col = train_df[indicator_column].diff()

            is_slope_ic = compute_information_coefficient(
                feature_values=slope_col,
                target_returns=is_fwd_returns,
                timestamps_ms=train_df["timestamp"],
                correlation_method="spearman",
            )

            # OOS slope
            oos_slope_mean_ic = None
            oos_slope_t_stat = None
            oos_slope_p_value = None
            oos_slope_effective_n = None
            oos_slope_retention = None
            oos_slope_interpretation = None

            if len(test_df) >= MIN_OOS_OBSERVATIONS:
                slope_col_test = test_df[indicator_column].diff()
                oos_fwd_returns = compute_forward_return(test_df, horizon, mask_overnight=True)
                oos_slope_ic = compute_information_coefficient(
                    feature_values=slope_col_test,
                    target_returns=oos_fwd_returns,
                    timestamps_ms=test_df["timestamp"],
                    correlation_method="spearman",
                )
                oos_slope_mean_ic = oos_slope_ic.mean_ic
                oos_slope_t_stat = oos_slope_ic.ic_t_stat
                oos_slope_p_value = oos_slope_ic.ic_p_value
                oos_slope_effective_n = int(oos_slope_ic.effective_n)

                if abs(is_slope_ic.mean_ic) > 1e-10:
                    oos_slope_retention = abs(oos_slope_mean_ic) / abs(is_slope_ic.mean_ic)
                    oos_slope_retention = min(oos_slope_retention, 2.0)
                else:
                    oos_slope_retention = 0.0

                oos_slope_interpretation = _interpret_ic_result(
                    oos_slope_mean_ic, oos_slope_p_value, fdr_ps[i],
                    is_oos=True, oos_retention=oos_slope_retention,
                )

            slope_is_p = is_slope_ic.nw_p_value if is_slope_ic.nw_p_value else is_slope_ic.ic_p_value
            slope_interp = _interpret_ic_result(
                is_slope_ic.mean_ic, slope_is_p, fdr_ps[i], is_oos=False
            )

            slope_results.append(
                HorizonICAnalysis(
                    horizon=horizon,
                    is_mean_ic=is_slope_ic.mean_ic,
                    is_t_stat=is_slope_ic.ic_t_stat,
                    is_p_value=is_slope_ic.ic_p_value,
                    is_nw_t_stat=is_slope_ic.nw_t_stat,
                    is_nw_p_value=is_slope_ic.nw_p_value,
                    is_effective_n=int(is_slope_ic.effective_n),
                    is_daily_ic_values=is_slope_ic.daily_ic_values,
                    is_daily_ic_dates=is_slope_ic.daily_ic_dates,
                    oos_mean_ic=oos_slope_mean_ic,
                    oos_t_stat=oos_slope_t_stat,
                    oos_p_value=oos_slope_p_value,
                    oos_effective_n=oos_slope_effective_n,
                    oos_retention=oos_slope_retention,
                    bonferroni_p=bonferroni_ps[i],  # Same correction for slope
                    fdr_p=fdr_ps[i],
                    random_baseline_mean=0.0,  # Skip random baseline for slope
                    random_baseline_std=0.0,
                    ic_vs_random_zscore=0.0,
                    is_interpretation=slope_interp,
                    oos_interpretation=oos_slope_interpretation,
                    is_hit_rate=is_slope_ic.hit_rate,
                    is_daily_ic_std=is_slope_ic.daily_ic_std,
                    strength_label=compute_strength_label(abs(is_slope_ic.mean_ic)),
                    stability_label=compute_stability_label(is_slope_ic.hit_rate),
                    direction_label=compute_direction_label(is_slope_ic.mean_ic),
                    retention_delta_pct=compute_retention_delta_pct(
                        is_slope_ic.mean_ic, oos_slope_mean_ic
                    ),
                )
            )

    logger.info(
        "[Reliability] Analyzed %s across %d horizons (train=%d, test=%d bars)",
        indicator_column,
        len(horizons),
        len(train_df),
        len(test_df),
    )

    return raw_results, slope_results, metadata


def find_best_horizon(results: list[HorizonICAnalysis]) -> int | None:
    """Find horizon with strongest OOS signal (or IS if no OOS).

    Priority:
    1. Highest |OOS IC| with OOS p < 0.10
    2. Highest |IS IC| with FDR p < 0.10 (if no OOS significant)
    """
    if not results:
        return None

    # First try: OOS significant results
    oos_candidates = [
        r for r in results
        if r.oos_p_value is not None and r.oos_p_value < 0.10
    ]

    if oos_candidates:
        best = max(oos_candidates, key=lambda r: abs(r.oos_mean_ic or 0))
        return best.horizon

    # Fallback: IS significant after FDR
    is_candidates = [r for r in results if r.fdr_p < 0.10]

    if is_candidates:
        best = max(is_candidates, key=lambda r: abs(r.is_mean_ic))
        return best.horizon

    return None


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


def generate_warnings(
    results: list[HorizonICAnalysis],
    test_bars: int,
    num_horizons: int,
) -> list[str]:
    """Generate warning messages based on analysis results."""
    warnings: list[str] = []

    # Check for OOS data sufficiency
    if test_bars < MIN_OOS_OBSERVATIONS:
        warnings.append(
            f"Insufficient out-of-sample data ({test_bars} bars). "
            "All results are in-sample only and may be overfit."
        )

    # Check if any horizon survives Bonferroni
    any_bonferroni = any(r.bonferroni_p < 0.05 for r in results)
    if not any_bonferroni and num_horizons > 1:
        warnings.append(
            f"After Bonferroni correction for {num_horizons} tests, "
            "no horizons remain significant at p < 0.05. "
            "Consider this exploratory."
        )

    # Check for OOS degradation
    for r in results:
        if r.oos_retention is not None and r.oos_retention < 0.4 and r.is_nw_p_value and r.is_nw_p_value < 0.05:
            warnings.append(
                f"{r.horizon}-bar horizon shows significant IS IC but "
                f"only {r.oos_retention:.0%} OOS retention. Likely overfit."
            )
            break  # Only add one such warning

    # Check if best IC is barely above random
    best_z = max((abs(r.ic_vs_random_zscore) for r in results), default=0)
    if best_z < 2.0:
        warnings.append(
            "Best IC is less than 2 standard deviations above random baseline. "
            "Signal may not be distinguishable from noise."
        )

    return warnings


# ─── Tranche 2: decay curve & regime conditioning ──────────────────────────

# Regime analysis needs at least this many bars per bucket before we trust the
# IC estimate; below this we return null for that regime.
MIN_REGIME_BARS = 50
DEFAULT_VOL_WINDOW = 20
MAX_DECAY_HORIZON = 60


def _ic_stderr(ic: ICResult) -> float:
    """Derive a stderr for the IC estimate.

    Prefers NW-implied stderr (|mean / nw_t|) to account for serial
    correlation; falls back to daily_ic_std / sqrt(n_eff) if NW isn't usable.
    """
    if ic.nw_t_stat and abs(ic.nw_t_stat) > 1e-10:
        return float(abs(ic.mean_ic / ic.nw_t_stat))
    n = max(int(ic.effective_n), 1)
    return float(ic.daily_ic_std / math.sqrt(n))


@dataclass
class DecayCurvePoint:
    horizon: int
    ic: float
    p_value: float
    ic_stderr: float


def compute_ic_decay_curve(
    train_df: pd.DataFrame,
    indicator_column: str,
    max_horizon: int,
) -> list[DecayCurvePoint]:
    """Compute IC at every integer horizon 1..max_horizon on a single series.

    Intentionally single-pass (no random baseline, no multiple-testing
    correction) — this is a diagnostic to see *where* the signal lives along
    the horizon axis. Use the main pipeline for the rigorous test.
    """
    max_horizon = min(max(max_horizon, 1), MAX_DECAY_HORIZON)

    feature = train_df[indicator_column]
    timestamps = train_df["timestamp"]

    curve: list[DecayCurvePoint] = []
    for horizon in range(1, max_horizon + 1):
        fwd = compute_forward_return(train_df, horizon, mask_overnight=True)
        ic = compute_information_coefficient(
            feature_values=feature,
            target_returns=fwd,
            timestamps_ms=timestamps,
            correlation_method="spearman",
        )
        curve.append(
            DecayCurvePoint(
                horizon=horizon,
                ic=float(ic.mean_ic),
                p_value=float(ic.ic_p_value),
                ic_stderr=_ic_stderr(ic),
            )
        )
    return curve


def split_by_volatility_regime(
    df: pd.DataFrame,
    window: int = DEFAULT_VOL_WINDOW,
) -> tuple[pd.Series, pd.Series]:
    """Split bars into high-vol / low-vol by rolling realized vol median.

    Returns (high_mask, low_mask) aligned to ``df``'s index. Bars inside the
    rolling warmup (vol is NaN) are excluded from both masks. The split is
    in-sample (median computed ex-post) — valid for diagnostic "when does
    this signal work" questions, NOT for generating tradable signals.
    """
    if "close" not in df.columns:
        raise ValueError("DataFrame must have 'close' column for regime split")

    log_returns = np.log(df["close"] / df["close"].shift(1))
    rolling_vol = log_returns.rolling(window=window).std()

    valid = rolling_vol.notna()
    if not valid.any():
        empty = pd.Series(False, index=df.index)
        return empty, empty

    median_vol = float(rolling_vol[valid].median())
    high_mask = (rolling_vol > median_vol) & valid
    low_mask = (rolling_vol <= median_vol) & valid
    return high_mask, low_mask


@dataclass
class RegimeICPoint:
    horizon: int
    mean_ic: float
    t_stat: float
    p_value: float
    effective_n: int
    hit_rate: float
    bars_in_regime: int


def compute_regime_ic(
    train_df: pd.DataFrame,
    indicator_column: str,
    horizons: list[int],
    window: int = DEFAULT_VOL_WINDOW,
) -> dict[str, list[RegimeICPoint] | None]:
    """IC per horizon for high-vol and low-vol subsets of the IS period.

    Returns ``{"high_vol": [...], "low_vol": [...]}`` where each value is a
    list of RegimeICPoint or None if the bucket has too few bars. Forward
    returns are computed on the full training series before masking so the
    "horizon" retains its real-time meaning (h bars ahead in wall-clock bars,
    regardless of regime transitions in-between).
    """
    high_mask, low_mask = split_by_volatility_regime(train_df, window=window)

    feature = train_df[indicator_column]
    timestamps = train_df["timestamp"]

    regime_results: dict[str, list[RegimeICPoint] | None] = {
        "high_vol": None,
        "low_vol": None,
    }

    for name, mask in (("high_vol", high_mask), ("low_vol", low_mask)):
        if int(mask.sum()) < MIN_REGIME_BARS:
            continue

        points: list[RegimeICPoint] = []
        for horizon in horizons:
            fwd = compute_forward_return(train_df, horizon, mask_overnight=True)
            ic = compute_information_coefficient(
                feature_values=feature[mask],
                target_returns=fwd[mask],
                timestamps_ms=timestamps[mask],
                correlation_method="spearman",
            )
            points.append(
                RegimeICPoint(
                    horizon=horizon,
                    mean_ic=float(ic.mean_ic),
                    t_stat=float(ic.ic_t_stat),
                    p_value=float(ic.ic_p_value),
                    effective_n=int(ic.effective_n),
                    hit_rate=float(ic.hit_rate),
                    bars_in_regime=int(mask.sum()),
                )
            )
        regime_results[name] = points

    return regime_results
