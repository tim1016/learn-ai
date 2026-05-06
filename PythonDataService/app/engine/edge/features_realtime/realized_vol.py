"""Realized volatility estimators (close-to-close, Parkinson, Garman-Klass, Yang-Zhang).

Formula: Close-to-close σ = std(log(Close_t/Close_{t-1})) * sqrt(252); Parkinson H = sqrt((1/(4N·ln2)) * Σ ln(Hi/Lo)²) * sqrt(252); Garman-Klass and Yang-Zhang per referenced papers
Reference: Parkinson, M. (1980) "The Extreme Value Method for Estimating the Variance of the Rate of Return" J. Business 53(1); Garman, M.B. & Klass, M.J. (1980) "On the Estimation of Security Price Volatilities from Historical Data" J. Business 53(1); Yang, D. & Zhang, Q. (2000) "Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices" J. Business 73(3)
Canonical implementation: app/engine/edge/features_realtime/realized_vol.py
Validated against: NONE — pending

Math provenance:
- Parkinson, M. (1980). "The Extreme Value Method for Estimating the Variance
  of the Rate of Return." Journal of Business 53(1).
- Garman, M. B., Klass, M. J. (1980). "On the Estimation of Security Price
  Volatilities from Historical Data." Journal of Business 53(1).
- Yang, D., Zhang, Q. (2000). "Drift-Independent Volatility Estimation Based
  on High, Low, Open, and Close Prices." Journal of Business 73(3).

All estimators return per-period variance unless `annualize=True`. Annualization
multiplies by `bars_per_year` (default 252 for daily). For 15-min RTH bars,
pass bars_per_year=252*26.

Hard rule: this module is in features_realtime/ — never .shift(-N).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DAILY_BARS_PER_YEAR = 252


def _annualize(per_period_var: pd.Series, bars_per_year: int) -> pd.Series:
    return np.sqrt(per_period_var * bars_per_year)


def close_to_close(
    bars: pd.DataFrame,
    *,
    window: int,
    annualize: bool = True,
    bars_per_year: int = DAILY_BARS_PER_YEAR,
) -> pd.Series:
    """Standard CtC: var = (1/(n-1)) Σ (r_t - mean(r))^2, where r = ln(C_t/C_{t-1})."""
    log_ret = np.log(bars["close"] / bars["close"].shift(1))
    var = log_ret.rolling(window, min_periods=window).var(ddof=1)
    return _annualize(var, bars_per_year) if annualize else var


def parkinson(
    bars: pd.DataFrame,
    *,
    window: int,
    annualize: bool = True,
    bars_per_year: int = DAILY_BARS_PER_YEAR,
) -> pd.Series:
    """Parkinson (1980): var = (1/(4n ln 2)) Σ (ln(H/L))^2.

    Assumes zero drift; ignores overnight gaps. ~5× more efficient than CtC.
    """
    hl_sq = np.log(bars["high"] / bars["low"]) ** 2
    var = hl_sq.rolling(window, min_periods=window).mean() / (4.0 * np.log(2.0))
    return _annualize(var, bars_per_year) if annualize else var


def garman_klass(
    bars: pd.DataFrame,
    *,
    window: int,
    annualize: bool = True,
    bars_per_year: int = DAILY_BARS_PER_YEAR,
) -> pd.Series:
    """Garman-Klass (1980): var = (1/n) Σ [0.5 (ln H/L)^2 - (2 ln 2 - 1)(ln C/O)^2]."""
    hl = np.log(bars["high"] / bars["low"]) ** 2
    co = np.log(bars["close"] / bars["open"]) ** 2
    inner = 0.5 * hl - (2.0 * np.log(2.0) - 1.0) * co
    var = inner.rolling(window, min_periods=window).mean()
    return _annualize(var, bars_per_year) if annualize else var


def yang_zhang(
    bars: pd.DataFrame,
    *,
    window: int,
    annualize: bool = True,
    bars_per_year: int = DAILY_BARS_PER_YEAR,
) -> pd.Series:
    """Yang-Zhang (2000): drift-independent, gap-aware.

    var_YZ = sigma_O^2 + k * sigma_C^2 + (1-k) * sigma_RS^2
    where:
      sigma_O^2  = var of ln(O_t / C_{t-1})  (overnight)
      sigma_C^2  = var of ln(C_t / O_t)      (open-to-close)
      sigma_RS^2 = Rogers-Satchell estimator
      k          = 0.34 / (1.34 + (n+1)/(n-1))   per Yang-Zhang 2000

    Default and recommended estimator for daily VRP and regime features.
    """
    if window < 2:
        raise ValueError("window must be >= 2 for Yang-Zhang")
    log_oc = np.log(bars["open"] / bars["close"].shift(1))  # overnight
    log_co = np.log(bars["close"] / bars["open"])  # open-to-close
    log_ho = np.log(bars["high"] / bars["open"])
    log_lo = np.log(bars["low"] / bars["open"])
    log_hc = np.log(bars["high"] / bars["close"])
    log_lc = np.log(bars["low"] / bars["close"])

    rs = log_ho * log_hc + log_lo * log_lc

    sigma_o2 = log_oc.rolling(window, min_periods=window).var(ddof=1)
    sigma_c2 = log_co.rolling(window, min_periods=window).var(ddof=1)
    sigma_rs2 = rs.rolling(window, min_periods=window).mean()

    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    var = sigma_o2 + k * sigma_c2 + (1 - k) * sigma_rs2
    var = var.clip(lower=0)  # numerical floor; YZ can dip slightly negative on small samples
    return _annualize(var, bars_per_year) if annualize else var
