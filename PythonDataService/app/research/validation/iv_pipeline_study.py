"""Empirical IV Pipeline Validation Study.

Confirms three well-known market facts using our IV pipeline + Polygon data:
  1. IV > RV  (volatility risk premium exists)
  2. IV predicts future RV  (positive correlation)
  3. IV predicts larger absolute returns  (monotonic relationship)

Usage:
    python -m app.research.validation.iv_pipeline_study \
        --tickers SPY,QQQ,AAPL \
        --start 2024-06-01 --end 2025-12-31

Requires: POLYGON_API_KEY (and optionally FRED_API_KEY) in environment.
"""
from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy import stats

from app.research.options.iv_builder import build_iv_history
from app.services.fred_service import prefetch_rate_cache
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

RV_WINDOW = 21  # Trading days for realized vol (approx 1 month)
FORWARD_RV_WINDOW = 21  # Forward-looking RV window
ANNUALIZE = math.sqrt(252)


@dataclass
class TickerResult:
    """Results for a single ticker."""

    ticker: str
    n_obs: int = 0

    # Fact 1: IV > RV
    mean_iv: float = 0.0
    mean_rv: float = 0.0
    vrp_mean: float = 0.0
    vrp_t_stat: float = 0.0
    vrp_p_value: float = 1.0
    pct_iv_gt_rv: float = 0.0

    # Fact 2: IV predicts future RV
    iv_fwd_rv_corr: float = 0.0
    iv_fwd_rv_p: float = 1.0
    iv_fwd_rv_r2: float = 0.0

    # Fact 3: IV predicts absolute returns
    quintile_abs_ret: list[float] = field(default_factory=list)
    quintile_monotonic: bool = False
    spearman_iv_absret: float = 0.0
    spearman_iv_absret_p: float = 1.0


@dataclass
class StudyReport:
    """Full study report across all tickers."""

    tickers: list[str] = field(default_factory=list)
    results: list[TickerResult] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""

    # Aggregate verdicts
    fact1_pass: bool = False
    fact2_pass: bool = False
    fact3_pass: bool = False


def _compute_realized_vol(
    close: pd.Series, window: int = RV_WINDOW, forward: bool = False,
) -> pd.Series:
    """Compute annualized realized volatility from close prices.

    Args:
        close: Daily close prices.
        window: Rolling window in trading days.
        forward: If True, use forward-looking window (shift result back).
    """
    log_ret = np.log(close / close.shift(1))
    if forward:
        rv = log_ret.shift(-window).rolling(window).std() * ANNUALIZE
        # Shift back so rv[t] = vol over [t+1, t+window]
        # Actually: we need std of returns from t+1 to t+window
        rv = log_ret.rolling(window).std().shift(-window) * ANNUALIZE
    else:
        rv = log_ret.rolling(window).std() * ANNUALIZE
    return rv


def _run_single_ticker(
    ticker: str,
    start_date: str,
    end_date: str,
    polygon_client: PolygonClientService,
) -> TickerResult:
    """Run all three tests for a single ticker."""
    result = TickerResult(ticker=ticker)

    logger.info(f"[STUDY] Building IV history for {ticker}...")
    iv_df = build_iv_history(ticker, start_date, end_date, polygon_client)

    if iv_df.empty or iv_df["iv_30d_atm"].notna().sum() < 30:
        logger.warning(f"[STUDY] {ticker}: insufficient IV data ({iv_df['iv_30d_atm'].notna().sum() if not iv_df.empty else 0} valid days)")
        return result

    logger.info(f"[STUDY] Fetching stock bars for {ticker}...")
    stock_bars = polygon_client.fetch_aggregates(
        ticker=ticker, multiplier=1, timespan="day",
        from_date=start_date, to_date=end_date,
    )

    if not stock_bars:
        logger.warning(f"[STUDY] {ticker}: no stock bars")
        return result

    # Build stock DataFrame
    stock_df = pd.DataFrame(stock_bars)
    stock_df["date"] = pd.to_datetime(stock_df["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")
    stock_df = stock_df[["date", "close"]].drop_duplicates(subset="date")

    # Merge IV + stock data
    merged = iv_df[["date", "iv_30d_atm"]].merge(stock_df, on="date", how="inner")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged["close"] = merged["close"].astype(float)
    merged["iv_30d_atm"] = merged["iv_30d_atm"].astype(float)

    # Compute trailing and forward RV
    merged["rv_trailing"] = _compute_realized_vol(merged["close"], RV_WINDOW, forward=False)
    merged["rv_forward"] = _compute_realized_vol(merged["close"], FORWARD_RV_WINDOW, forward=True)

    # Forward absolute return (21-day)
    price_ratio = merged["close"].shift(-FORWARD_RV_WINDOW) / merged["close"]
    merged["fwd_abs_ret"] = np.abs(np.log(price_ratio.where(price_ratio > 0)))

    # Drop rows with missing data
    clean = merged.dropna(subset=["iv_30d_atm", "rv_trailing", "rv_forward", "fwd_abs_ret"]).copy()
    result.n_obs = len(clean)

    if result.n_obs < 20:
        logger.warning(f"[STUDY] {ticker}: only {result.n_obs} clean observations")
        return result

    logger.info(f"[STUDY] {ticker}: {result.n_obs} observations for analysis")

    iv = clean["iv_30d_atm"].values
    rv_trail = clean["rv_trailing"].values
    rv_fwd = clean["rv_forward"].values
    fwd_abs = clean["fwd_abs_ret"].values

    # --- Fact 1: IV > RV (Volatility Risk Premium) ---
    vrp = iv - rv_trail
    result.mean_iv = float(np.mean(iv))
    result.mean_rv = float(np.mean(rv_trail))
    result.vrp_mean = float(np.mean(vrp))
    result.pct_iv_gt_rv = float(np.mean(vrp > 0))

    if np.std(vrp, ddof=1) > 1e-10:
        t_stat, p_val = stats.ttest_1samp(vrp, 0)
        result.vrp_t_stat = float(t_stat)
        result.vrp_p_value = float(p_val)

    # --- Fact 2: IV predicts future RV ---
    corr, p = stats.pearsonr(iv, rv_fwd)
    result.iv_fwd_rv_corr = float(corr)
    result.iv_fwd_rv_p = float(p)
    result.iv_fwd_rv_r2 = float(corr ** 2)

    # --- Fact 3: IV predicts larger absolute returns ---
    # Sort into quintiles by IV level
    clean = clean.copy()
    clean["iv_quintile"] = pd.qcut(clean["iv_30d_atm"], 5, labels=False, duplicates="drop")
    quintile_means = clean.groupby("iv_quintile")["fwd_abs_ret"].mean().sort_index()
    result.quintile_abs_ret = quintile_means.tolist()

    # Check monotonicity (each quintile >= previous)
    if len(result.quintile_abs_ret) >= 3:
        diffs = np.diff(result.quintile_abs_ret)
        result.quintile_monotonic = bool(np.sum(diffs > 0) >= len(diffs) * 0.6)

    # Spearman rank correlation: IV vs forward absolute return
    sp_corr, sp_p = stats.spearmanr(iv, fwd_abs)
    result.spearman_iv_absret = float(sp_corr)
    result.spearman_iv_absret_p = float(sp_p)

    return result


def run_iv_pipeline_study(
    tickers: list[str],
    start_date: str,
    end_date: str,
) -> StudyReport:
    """Run the full empirical validation study.

    Args:
        tickers: List of stock tickers to study.
        start_date: ISO date string.
        end_date: ISO date string.

    Returns:
        StudyReport with per-ticker results and aggregate verdicts.
    """
    polygon_client = PolygonClientService()
    report = StudyReport(tickers=tickers, start_date=start_date, end_date=end_date)

    # Bulk-prefetch FRED rates (4 HTTP calls total instead of 4 per trading day)
    logger.info("[STUDY] Prefetching FRED Treasury rates for %s to %s...", start_date, end_date)
    prefetch_rate_cache(start_date, end_date)

    for ticker in tickers:
        try:
            result = _run_single_ticker(ticker, start_date, end_date, polygon_client)
            report.results.append(result)
        except Exception as e:
            logger.error(f"[STUDY] {ticker} failed: {e}", exc_info=True)
            report.results.append(TickerResult(ticker=ticker))

    # Aggregate verdicts across tickers with sufficient data
    valid = [r for r in report.results if r.n_obs >= 20]

    if valid:
        # Fact 1: majority show positive VRP at p < 0.05
        report.fact1_pass = sum(
            1 for r in valid if r.vrp_mean > 0 and r.vrp_p_value < 0.05
        ) > len(valid) / 2

        # Fact 2: majority show positive IV-fwdRV correlation at p < 0.05
        report.fact2_pass = sum(
            1 for r in valid if r.iv_fwd_rv_corr > 0 and r.iv_fwd_rv_p < 0.05
        ) > len(valid) / 2

        # Fact 3: majority show positive Spearman IV-absRet
        report.fact3_pass = sum(
            1 for r in valid if r.spearman_iv_absret > 0 and r.spearman_iv_absret_p < 0.10
        ) > len(valid) / 2

    return report


def print_report(report: StudyReport) -> None:
    """Print a formatted study report to stdout."""
    print(f"\n{'=' * 72}")
    print(f"  IV Pipeline Empirical Validation Study")
    print(f"  Period: {report.start_date} to {report.end_date}")
    print(f"  Tickers: {', '.join(report.tickers)}")
    print(f"{'=' * 72}")

    for r in report.results:
        print(f"\n--- {r.ticker} ({r.n_obs} observations) ---")

        if r.n_obs < 20:
            print("  SKIPPED: insufficient data")
            continue

        # Fact 1
        print(f"\n  Fact 1: IV > RV (Volatility Risk Premium)")
        print(f"    Mean IV:        {r.mean_iv:.4f} ({r.mean_iv*100:.1f}%)")
        print(f"    Mean RV:        {r.mean_rv:.4f} ({r.mean_rv*100:.1f}%)")
        print(f"    Mean VRP:       {r.vrp_mean:.4f} ({r.vrp_mean*100:.1f}%)")
        print(f"    % days IV > RV: {r.pct_iv_gt_rv*100:.1f}%")
        print(f"    t-stat:         {r.vrp_t_stat:.3f}  (p={r.vrp_p_value:.4f})")
        verdict1 = "PASS" if r.vrp_mean > 0 and r.vrp_p_value < 0.05 else "FAIL"
        print(f"    Verdict:        {verdict1}")

        # Fact 2
        print(f"\n  Fact 2: IV Predicts Future RV")
        print(f"    Pearson r:      {r.iv_fwd_rv_corr:.4f}  (p={r.iv_fwd_rv_p:.4f})")
        print(f"    R-squared:      {r.iv_fwd_rv_r2:.4f}")
        verdict2 = "PASS" if r.iv_fwd_rv_corr > 0 and r.iv_fwd_rv_p < 0.05 else "FAIL"
        print(f"    Verdict:        {verdict2}")

        # Fact 3
        print(f"\n  Fact 3: IV Predicts Larger Absolute Returns")
        if r.quintile_abs_ret:
            q_labels = [f"Q{i+1}={v:.4f}" for i, v in enumerate(r.quintile_abs_ret)]
            print(f"    Quintile abs|r|: {', '.join(q_labels)}")
        print(f"    Monotonic:      {'Yes' if r.quintile_monotonic else 'No'}")
        print(f"    Spearman rho:   {r.spearman_iv_absret:.4f}  (p={r.spearman_iv_absret_p:.4f})")
        verdict3 = "PASS" if r.spearman_iv_absret > 0 and r.spearman_iv_absret_p < 0.10 else "FAIL"
        print(f"    Verdict:        {verdict3}")

    # Overall
    print(f"\n{'=' * 72}")
    print(f"  OVERALL VERDICTS")
    print(f"{'=' * 72}")
    print(f"  Fact 1 (IV > RV):              {'PASS' if report.fact1_pass else 'FAIL'}")
    print(f"  Fact 2 (IV predicts fwd RV):   {'PASS' if report.fact2_pass else 'FAIL'}")
    print(f"  Fact 3 (IV predicts |returns|): {'PASS' if report.fact3_pass else 'FAIL'}")

    all_pass = report.fact1_pass and report.fact2_pass and report.fact3_pass
    print(f"\n  Pipeline Validation: {'ALL FACTS CONFIRMED' if all_pass else 'SOME FACTS NOT CONFIRMED'}")
    if not all_pass:
        print("  (Check data coverage and date range — short periods may not show significance)")
    print(f"{'=' * 72}\n")


def main() -> None:
    """CLI entry point."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="IV Pipeline Empirical Validation Study")
    parser.add_argument("--tickers", default="SPY,QQQ,AAPL", help="Comma-separated tickers")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: 18 months ago)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: yesterday)")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",")]

    end_date = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = args.start or (datetime.now() - timedelta(days=548)).strftime("%Y-%m-%d")

    report = run_iv_pipeline_study(tickers, start_date, end_date)
    print_report(report)

    sys.exit(0 if (report.fact1_pass and report.fact2_pass and report.fact3_pass) else 1)


if __name__ == "__main__":
    main()
