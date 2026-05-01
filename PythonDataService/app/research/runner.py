"""Research experiment orchestrator.

Coordinates: data → feature → target → IC → stationarity → quantiles → report.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd

from app.ml.preprocessing.stationarity import run_stationarity_tests
from app.research.config import ResearchConfig
from app.research.feature_spec import FeatureValidationSpec, get_spec
from app.research.feature_validation import (
    FeatureValidationVerdict,
    evaluate_feature_validation,
)
from app.research.features.ta_features import TechnicalFeatures
from app.research.target import compute_15min_forward_return, validate_return_series
from app.research.validation.ic import compute_information_coefficient
from app.research.validation.quantile import compute_quantile_analysis
from app.research.validation.robustness import RobustnessResult, compute_robustness

logger = logging.getLogger(__name__)


@dataclass
class ResearchReport:
    """Complete feature validation report."""

    ticker: str
    feature_name: str
    start_date: str
    end_date: str
    bars_used: int = 0

    # IC results
    mean_ic: float = 0.0
    ic_t_stat: float = 0.0
    ic_p_value: float = 1.0
    ic_values: list[float] = field(default_factory=list)
    ic_dates: list[str] = field(default_factory=list)
    nw_t_stat: float = 0.0
    nw_p_value: float = 1.0
    effective_n: float = 0.0

    # Stationarity results
    adf_pvalue: float = 1.0
    kpss_pvalue: float = 0.0
    is_stationary: bool = False

    # Quantile results
    quantile_bins: list[dict] = field(default_factory=list)
    is_monotonic: bool = False
    monotonicity_ratio: float = 0.0

    # Robustness
    robustness: RobustnessResult | None = None

    # Per-feature validation contract & multi-screen verdict (replaces the
    # legacy single-boolean ``passed_validation``; that boolean stays for
    # back-compat but is now derived from the verdict).
    feature_spec: FeatureValidationSpec | None = None
    validation_verdict: FeatureValidationVerdict | None = None

    # Overall
    passed_validation: bool = False
    error: str | None = None


def run_feature_research(
    ticker: str,
    feature_name: str,
    bars: list[dict],
    start_date: str,
    end_date: str,
    config: ResearchConfig | None = None,
) -> ResearchReport:
    """Run a complete feature validation experiment.

    Parameters
    ----------
    ticker : str
        Stock symbol (e.g. "AAPL").
    feature_name : str
        Feature to validate (must be in FeatureName enum).
    bars : list[dict]
        OHLCV bars with timestamp, open, high, low, close, volume.
    start_date, end_date : str
        ISO date strings for the research window.
    config : ResearchConfig, optional
        Research parameters (uses defaults if None).

    Returns
    -------
    ResearchReport
        Full validation results including IC, stationarity, and quantiles.
        On error, ``error`` is set and ``passed_validation`` is False.
    """
    if config is None:
        config = ResearchConfig()

    report = ResearchReport(
        ticker=ticker,
        feature_name=feature_name,
        start_date=start_date,
        end_date=end_date,
    )

    try:
        logger.info(
            "[Research] Starting: %s %s [%s to %s] (%d bars)",
            ticker,
            feature_name,
            start_date,
            end_date,
            len(bars),
        )

        if len(bars) < config.min_series_length:
            raise ValueError(f"Not enough bars: {len(bars)} < {config.min_series_length} minimum")

        report.bars_used = len(bars)
        df = pd.DataFrame(bars).sort_values("timestamp").reset_index(drop=True)

        # Step 1: Compute 15-min forward log return target
        target_returns = compute_15min_forward_return(bars, config.horizon)
        if not validate_return_series(target_returns):
            raise ValueError("Target return series failed validation (too many NaNs or zero variance)")

        # Step 2: Compute feature
        feature_values = TechnicalFeatures.compute_feature(feature_name, bars)

        # Step 3: Information Coefficient
        ic_result = compute_information_coefficient(
            feature_values,
            target_returns,
            df["timestamp"],
            correlation_method=config.ic_correlation_method,
        )
        report.mean_ic = ic_result.mean_ic
        report.ic_t_stat = ic_result.ic_t_stat
        report.ic_p_value = ic_result.ic_p_value
        report.ic_values = ic_result.daily_ic_values
        report.ic_dates = ic_result.daily_ic_dates
        report.nw_t_stat = ic_result.nw_t_stat
        report.nw_p_value = ic_result.nw_p_value
        report.effective_n = ic_result.effective_n

        # Step 4: Stationarity test on the feature series
        clean_feature = feature_values.dropna().values
        if len(clean_feature) >= 20:
            stationarity = run_stationarity_tests(
                clean_feature,
                adf_significance=config.adf_significance,
                kpss_significance=config.kpss_significance,
            )
            report.adf_pvalue = stationarity.adf_pvalue
            report.kpss_pvalue = stationarity.kpss_pvalue
            report.is_stationary = stationarity.is_stationary
        else:
            logger.warning("[Research] Feature series too short for stationarity test")

        # Step 5: Quantile analysis
        quantile_result = compute_quantile_analysis(
            feature_values,
            target_returns,
            n_bins=config.n_bins,
            monotonicity_threshold=config.monotonicity_threshold,
        )
        report.quantile_bins = [asdict(b) for b in quantile_result.bins]
        report.is_monotonic = quantile_result.is_monotonic
        report.monotonicity_ratio = quantile_result.monotonicity_ratio

        # Step 6: Robustness analysis
        if len(ic_result.daily_ic_values) >= 2:
            report.robustness = compute_robustness(
                daily_ic_values=ic_result.daily_ic_values,
                daily_ic_dates=ic_result.daily_ic_dates,
                bars=bars,
            )

        # Step 7: Per-feature validation verdict (4 screens + 0/1/2/3 ladder).
        # Replaces the old single-boolean gate. ``passed_validation`` is
        # retained as `stage >= 1` for callers that haven't migrated.
        spec = get_spec(feature_name)
        report.feature_spec = spec

        # Use NW p-value when available as it accounts for autocorrelation
        effective_p = ic_result.nw_p_value if ic_result.nw_p_value < 1.0 else ic_result.ic_p_value

        train_test = report.robustness.train_test if report.robustness is not None else None
        train_test_present = train_test is not None
        test_days = train_test.test_days if train_test is not None else 0
        test_mean_ic = train_test.test_mean_ic if train_test is not None else 0.0
        oos_retention = train_test.oos_retention if train_test is not None else 0.0

        regimes_observed = (
            len(report.robustness.volatility_regimes) + len(report.robustness.trend_regimes)
            if report.robustness is not None
            else 0
        )

        report.validation_verdict = evaluate_feature_validation(
            spec=spec,
            mean_ic=ic_result.mean_ic,
            nw_p_value=effective_p,
            effective_n=ic_result.effective_n,
            is_stationary=report.is_stationary,
            is_monotonic=quantile_result.is_monotonic,
            quantile_bins=report.quantile_bins,
            train_test_present=train_test_present,
            test_days=test_days,
            test_mean_ic=test_mean_ic,
            oos_retention=oos_retention,
            regimes_observed=regimes_observed,
        )

        report.passed_validation = report.validation_verdict.stage_info.stage >= 1

        logger.info(
            "[Research] Complete: %s %s — stage=%d (%s) IC=%.4f, NW p=%.4f, "
            "stationary=%s, monotonic=%s, OOS retention=%.0f%%",
            ticker,
            feature_name,
            report.validation_verdict.stage_info.stage,
            report.validation_verdict.stage_info.label,
            report.mean_ic,
            effective_p,
            report.is_stationary,
            report.is_monotonic,
            oos_retention * 100.0,
        )

    except Exception as e:
        logger.error("[Research] Error: %s", str(e), exc_info=True)
        report.error = str(e)
        report.passed_validation = False

    return report
