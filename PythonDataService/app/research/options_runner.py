"""Options feature research runner.

Adapted from the stock feature runner for daily-frequency options signals.
Supports multiple target types: directional, volatility, absolute return.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict

import numpy as np
import pandas as pd

from app.ml.preprocessing.stationarity import run_stationarity_tests
from app.research.config import ResearchConfig
from app.research.features.options_features import OptionsFeatures
from app.research.options.diagnostics import run_iv_diagnostics
from app.research.runner import ResearchReport
from app.research.target import validate_return_series
from app.research.validation.ic import compute_information_coefficient
from app.research.validation.quantile import compute_quantile_analysis
from app.research.validation.robustness import compute_robustness

logger = logging.getLogger(__name__)

MIN_NW_LAG = 5  # Minimum Newey-West lag for daily options data (autocorrelated)
ROLLING_IC_WINDOW = 20  # 20-day rolling window for time-series IC (daily data)


def _compute_daily_forward_return(
    stock_bars: list[dict],
    target_type: str = "directional",
) -> pd.Series:
    """Compute daily forward returns for options research.

    Args:
        stock_bars: Daily OHLCV bars
        target_type:
            "directional" - ln(close_{t+1} / close_t)
            "volatility"  - forward 5-day realized vol
            "abs_return"  - |ln(close_{t+1} / close_t)|
    """
    df = pd.DataFrame(stock_bars).sort_values("timestamp").reset_index(drop=True)
    close = df["close"].astype(float)

    if target_type == "directional":
        # 1-day forward log return
        target = np.log(close.shift(-1) / close)
    elif target_type == "volatility":
        # Forward 5-day realized vol (annualized)
        log_ret = np.log(close / close.shift(1))
        target = log_ret.shift(-5).rolling(window=5).std() * math.sqrt(252)
    elif target_type == "abs_return":
        # Absolute 1-day forward return
        target = np.log(close.shift(-1) / close).abs()
    else:
        raise ValueError(f"Unknown target_type: {target_type}")

    target.name = f"fwd_{target_type}"
    return target


def _build_daily_timestamps(stock_bars: list[dict]) -> pd.Series:
    """Build daily timestamp series from stock bars (ms epoch)."""
    df = pd.DataFrame(stock_bars).sort_values("timestamp").reset_index(drop=True)
    return df["timestamp"]


def run_options_feature_research(
    ticker: str,
    feature_name: str,
    iv_data: list[dict],
    stock_daily_bars: list[dict],
    start_date: str,
    end_date: str,
    target_type: str = "directional",
    config: ResearchConfig | None = None,
) -> ResearchReport:
    """Run options feature research with IV data.

    Same pipeline as stock feature research but:
    - Daily granularity (not minute)
    - IV data instead of OHLCV for feature computation
    - Configurable target type (directional, volatility, abs_return)
    - Newey-West lag >= 5 (daily autocorrelation)
    - IV diagnostics before analysis
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
            "[Options Research] Starting: %s %s [%s to %s] (%d IV points, target=%s)",
            ticker, feature_name, start_date, end_date, len(iv_data), target_type,
        )

        # Step 0: IV diagnostics — validate data before research
        iv_df = pd.DataFrame(iv_data)
        if "atm_iv" in iv_df.columns and "iv_30d_atm" not in iv_df.columns:
            iv_df = iv_df.rename(columns={
                "atm_iv": "iv_30d_atm",
                "iv_otm_put": "iv_30d_put",
                "iv_otm_call": "iv_30d_call",
            })

        diagnostics = run_iv_diagnostics(iv_df)
        if not diagnostics.valid:
            warnings_str = "; ".join(diagnostics.warnings)
            raise ValueError(f"IV data failed diagnostics: {warnings_str}")

        # Minimum data requirement (lower for daily vs minute data)
        min_daily_points = 30
        if len(iv_df) < min_daily_points:
            raise ValueError(
                f"Not enough IV data points: {len(iv_df)} < {min_daily_points}"
            )

        report.bars_used = len(iv_df)

        # Step 1: Compute forward target from stock data
        target_returns = _compute_daily_forward_return(stock_daily_bars, target_type)

        # Align IV data and stock data by date
        stock_df = pd.DataFrame(stock_daily_bars).sort_values("timestamp").reset_index(drop=True)
        stock_df["date"] = pd.to_datetime(stock_df["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")

        iv_df["date"] = iv_df["date"].astype(str)

        # Merge on date
        merged = stock_df.merge(iv_df, on="date", how="inner", suffixes=("_stock", "_iv"))
        if len(merged) < min_daily_points:
            raise ValueError(
                f"Not enough aligned data after merge: {len(merged)} < {min_daily_points}"
            )

        # Recompute target on merged data
        close = merged["close"].astype(float) if "close" in merged.columns else merged["close_stock"].astype(float)
        if target_type == "directional":
            target_returns = np.log(close.shift(-1) / close)
        elif target_type == "volatility":
            log_ret = np.log(close / close.shift(1))
            target_returns = log_ret.shift(-5).rolling(window=5).std() * math.sqrt(252)
        elif target_type == "abs_return":
            target_returns = np.log(close.shift(-1) / close).abs()

        target_returns = pd.Series(target_returns.values, index=merged.index)

        if not validate_return_series(target_returns):
            raise ValueError("Target return series failed validation")

        # Step 2: Compute options feature
        iv_aligned = merged[["iv_30d_atm", "iv_30d_put", "iv_30d_call"]].copy() if "iv_30d_atm" in merged.columns else merged.rename(columns={
            "atm_iv": "iv_30d_atm", "iv_otm_put": "iv_30d_put", "iv_otm_call": "iv_30d_call"
        })[["iv_30d_atm", "iv_30d_put", "iv_30d_call"]].copy()
        iv_aligned.index = merged.index

        stock_for_vrp = None
        if feature_name == "vrp_5":
            stock_for_vrp = merged[["close"]].copy() if "close" in merged.columns else merged[["close_stock"]].rename(columns={"close_stock": "close"}).copy()
            stock_for_vrp.index = merged.index

        feature_values = OptionsFeatures.compute_feature(
            feature_name, iv_aligned, stock_for_vrp, mode="research" if target_type != "directional" else "signal"
        )

        # Step 3: Information Coefficient (daily)
        timestamps = merged["timestamp"].values if "timestamp" in merged.columns else merged["timestamp_stock"].values
        timestamps_series = pd.Series(timestamps, index=merged.index)

        ic_result = compute_information_coefficient(
            feature_values,
            target_returns,
            timestamps_series,
            correlation_method=config.ic_correlation_method,
            min_nw_lag=MIN_NW_LAG,
            rolling_window=ROLLING_IC_WINDOW,
        )
        report.mean_ic = ic_result.mean_ic
        report.ic_t_stat = ic_result.ic_t_stat
        report.ic_p_value = ic_result.ic_p_value
        report.ic_values = ic_result.daily_ic_values
        report.ic_dates = ic_result.daily_ic_dates
        report.nw_t_stat = ic_result.nw_t_stat
        report.nw_p_value = ic_result.nw_p_value
        report.effective_n = ic_result.effective_n

        # Step 4: Stationarity
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

        # Step 6: Robustness (daily IC values — each "day" in options is one observation)
        if len(ic_result.daily_ic_values) >= 2:
            # For daily options data, create synthetic bars for robustness
            daily_bars = []
            for i, row in merged.iterrows():
                ts = row.get("timestamp", row.get("timestamp_stock", 0))
                c = row.get("close", row.get("close_stock", 0))
                v = row.get("volume", row.get("volume_stock", 0))
                daily_bars.append({
                    "timestamp": int(ts),
                    "open": float(c),
                    "high": float(c),
                    "low": float(c),
                    "close": float(c),
                    "volume": float(v) if v else 0.0,
                })
            report.robustness = compute_robustness(
                daily_ic_values=ic_result.daily_ic_values,
                daily_ic_dates=ic_result.daily_ic_dates,
                bars=daily_bars,
            )

        # Step 7: Validation gate
        effective_p = ic_result.nw_p_value if ic_result.nw_p_value < 1.0 else ic_result.ic_p_value
        report.passed_validation = (
            abs(ic_result.mean_ic) >= 0.03
            and effective_p < config.ic_significance
            and report.is_stationary
            and quantile_result.is_monotonic
        )

        logger.info(
            "[Options Research] Complete: %s %s (target=%s) — passed=%s "
            "(IC=%.4f, NW-t=%.2f, NW-p=%.4f, stationary=%s, monotonic=%s)",
            ticker, feature_name, target_type, report.passed_validation,
            report.mean_ic, report.nw_t_stat, report.nw_p_value,
            report.is_stationary, report.is_monotonic,
        )

    except Exception as e:
        logger.error("[Options Research] Error: %s", str(e), exc_info=True)
        report.error = str(e)
        report.passed_validation = False

    return report
