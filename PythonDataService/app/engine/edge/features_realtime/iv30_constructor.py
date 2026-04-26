"""Construct constant-maturity IV30 ATM, 25Δ skew, term-structure slope, and vol-of-vol.

For a given (timestamp, set of expiries with σ at deltas), this module:
1. Interpolates IV between expiries in *variance space* per the CBOE VIX whitepaper:
       σ²_target * T_target = w · σ²_T1 · T1 + (1-w) · σ²_T2 · T2
   where w = (T2 - T_target) / (T2 - T1).
2. Computes 25Δ skew: σ_25ΔP - σ_25ΔC at the 30d term.
3. Computes term-slope: σ_50Δ,60d - σ_50Δ,30d.
4. Computes vol-of-vol: ΔIV30 and rolling-std(IV30, 20).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def variance_interpolated_iv(
    *, sigma_t1: float, t1_years: float,
    sigma_t2: float, t2_years: float, target_t_years: float,
) -> float:
    """Variance-time-weighted interpolation between two expiries to a constant maturity."""
    if not (t1_years <= target_t_years <= t2_years):
        raise ValueError(f"target {target_t_years} not in [{t1_years}, {t2_years}]")
    if t2_years == t1_years:
        return float(sigma_t1)
    w1 = (t2_years - target_t_years) / (t2_years - t1_years)
    w2 = (target_t_years - t1_years) / (t2_years - t1_years)
    var_t1 = sigma_t1 ** 2 * t1_years
    var_t2 = sigma_t2 ** 2 * t2_years
    var_target = w1 * var_t1 + w2 * var_t2
    return float(np.sqrt(var_target / target_t_years))


def iv30_atm_50d(iv_by_expiry: pd.Series, target_days: int = 30) -> float | None:
    """Pick the two expiries straddling target_days (in days) and interpolate.

    iv_by_expiry: Series indexed by expiry-in-days, values = ATM IV (50Δ).
    Returns None if no straddling expiries exist.
    """
    if iv_by_expiry.empty:
        return None
    days = iv_by_expiry.index.to_numpy()
    if (days < target_days).any() and (days >= target_days).any():
        t1_days = int(days[days < target_days].max())
        t2_days = int(days[days >= target_days].min())
    else:
        # Extrapolate: pick the nearest expiry as a flat assumption.
        nearest = int(days[np.argmin(np.abs(days - target_days))])
        return float(iv_by_expiry.loc[nearest])
    return variance_interpolated_iv(
        sigma_t1=float(iv_by_expiry.loc[t1_days]), t1_years=t1_days / 365.0,
        sigma_t2=float(iv_by_expiry.loc[t2_days]), t2_years=t2_days / 365.0,
        target_t_years=target_days / 365.0,
    )


def skew_25d(iv_25d_put: float, iv_25d_call: float) -> float:
    """Risk-reversal skew: σ_25ΔP - σ_25ΔC. Positive = puts more expensive (typical equity)."""
    return float(iv_25d_put - iv_25d_call)


def term_slope(iv_30d: float, iv_60d: float) -> float:
    """σ(60d) - σ(30d). Positive = contango (calm); negative = backwardation (stress)."""
    return float(iv_60d - iv_30d)


def iv_change(iv_series: pd.Series) -> pd.Series:
    """First difference of IV30 — captures pace of fear adjustment."""
    return iv_series.diff()


def iv_vol(iv_series: pd.Series, window: int = 20) -> pd.Series:
    """Rolling std of IV30 — captures vol-of-vol regime."""
    return iv_series.rolling(window, min_periods=window).std(ddof=1)
