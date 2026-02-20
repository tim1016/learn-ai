from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from statsmodels.tsa.stattools import adfuller, kpss

logger = logging.getLogger(__name__)


@dataclass
class StationarityResult:
    """Results from ADF and KPSS stationarity tests."""

    adf_statistic: float
    adf_pvalue: float
    kpss_statistic: float
    kpss_pvalue: float
    is_stationary: bool

    @property
    def summary(self) -> str:
        status = "STATIONARY" if self.is_stationary else "NON-STATIONARY"
        return (
            f"{status} | ADF p={self.adf_pvalue:.4f}, "
            f"KPSS p={self.kpss_pvalue:.4f}"
        )


def run_stationarity_tests(
    series: np.ndarray,
    adf_significance: float = 0.05,
    kpss_significance: float = 0.05,
) -> StationarityResult:
    """Run ADF and KPSS stationarity tests on a 1D time series.

    Stationarity conclusion:
      - ADF tests H0: series has a unit root (non-stationary).
        Reject H0 (p < alpha) → evidence of stationarity.
      - KPSS tests H0: series is stationary.
        Fail to reject H0 (p > alpha) → evidence of stationarity.
      - Series is considered stationary when ADF rejects AND KPSS does not reject.

    Args:
        series: 1D array of values to test.
        adf_significance: Significance level for ADF test.
        kpss_significance: Significance level for KPSS test.

    Returns:
        StationarityResult with test statistics and conclusion.
    """
    series = np.asarray(series).flatten()

    if len(series) < 20:
        logger.warning("[ML] Stationarity: series too short (<20), skipping tests")
        return StationarityResult(
            adf_statistic=0.0,
            adf_pvalue=1.0,
            kpss_statistic=0.0,
            kpss_pvalue=0.0,
            is_stationary=False,
        )

    # ADF test
    adf_result = adfuller(series, autolag="AIC")
    adf_statistic = float(adf_result[0])
    adf_pvalue = float(adf_result[1])

    # KPSS test (suppress UserWarning about p-value bounds)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        kpss_result = kpss(series, regression="c", nlags="auto")
    kpss_statistic = float(kpss_result[0])
    kpss_pvalue = float(kpss_result[1])

    # Stationary if ADF rejects unit root AND KPSS fails to reject stationarity
    adf_rejects = adf_pvalue < adf_significance
    kpss_fails_to_reject = kpss_pvalue > kpss_significance
    is_stationary = adf_rejects and kpss_fails_to_reject

    result = StationarityResult(
        adf_statistic=adf_statistic,
        adf_pvalue=adf_pvalue,
        kpss_statistic=kpss_statistic,
        kpss_pvalue=kpss_pvalue,
        is_stationary=is_stationary,
    )

    if is_stationary:
        logger.info(f"[ML] Stationarity: {result.summary}")
    else:
        logger.warning(
            f"[ML] Stationarity: {result.summary} — "
            "Consider using log returns or differencing to achieve stationarity."
        )

    return result
