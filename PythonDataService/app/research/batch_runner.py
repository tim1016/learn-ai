"""Cross-sectional batch runner for options features.

Tests the same feature across multiple tickers to determine
if the effect is cross-sectionally consistent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.research.options.iv_builder import build_iv_history
from app.research.options_runner import run_options_feature_research
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

CROSS_SECTIONAL_THRESHOLD = 0.60  # 60% pass rate = consistent


@dataclass
class CrossSectionalReport:
    feature_name: str = ""
    tickers_tested: int = 0
    tickers_passed: int = 0
    pass_rate: float = 0.0
    cross_sectional_consistent: bool = False
    ticker_results: list[dict[str, Any]] = field(default_factory=list)
    aggregate_ic: float = 0.0
    summary: str = ""


def run_cross_sectional_study(
    feature_name: str,
    tickers: list[str],
    start_date: str,
    end_date: str,
    polygon_client: PolygonClientService,
    target_type: str = "directional",
) -> CrossSectionalReport:
    """Run a feature across multiple tickers and aggregate results.

    Processes tickers sequentially with per-ticker status logging.
    """
    report = CrossSectionalReport(feature_name=feature_name)

    ticker_results: list[dict[str, Any]] = []
    ic_values: list[float] = []

    for i, ticker in enumerate(tickers):
        logger.info(
            "[Batch] Processing ticker %d/%d: %s",
            i + 1, len(tickers), ticker,
        )

        result: dict[str, Any] = {
            "ticker": ticker,
            "mean_ic": 0.0,
            "ic_t_stat": 0.0,
            "ic_p_value": 1.0,
            "nw_t_stat": 0.0,
            "nw_p_value": 1.0,
            "effective_n": 0.0,
            "is_stationary": False,
            "passed_validation": False,
            "data_points": 0,
            "error": None,
        }

        try:
            # Step 1: Build IV history for this ticker
            iv_df = build_iv_history(
                underlying=ticker,
                start_date=start_date,
                end_date=end_date,
                polygon_client=polygon_client,
            )

            if iv_df.empty:
                result["error"] = "No IV data could be derived"
                ticker_results.append(result)
                continue

            result["data_points"] = len(iv_df)

            # Step 2: Get stock daily bars
            stock_bars = polygon_client.fetch_aggregates(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                from_date=start_date,
                to_date=end_date,
            )

            if not stock_bars:
                result["error"] = "No stock data available"
                ticker_results.append(result)
                continue

            # Step 3: Convert IV data to list of dicts for the runner
            iv_data = iv_df.to_dict(orient="records")

            # Rename columns for runner compatibility
            for row in iv_data:
                row["atm_iv"] = row.get("iv_30d_atm")
                row["iv_otm_put"] = row.get("iv_30d_put")
                row["iv_otm_call"] = row.get("iv_30d_call")

            # Step 4: Run options feature research
            research_report = run_options_feature_research(
                ticker=ticker,
                feature_name=feature_name,
                iv_data=iv_data,
                stock_daily_bars=stock_bars,
                start_date=start_date,
                end_date=end_date,
                target_type=target_type,
            )

            result["mean_ic"] = research_report.mean_ic
            result["ic_t_stat"] = research_report.ic_t_stat
            result["ic_p_value"] = research_report.ic_p_value
            result["nw_t_stat"] = research_report.nw_t_stat
            result["nw_p_value"] = research_report.nw_p_value
            result["effective_n"] = research_report.effective_n
            result["is_stationary"] = research_report.is_stationary
            result["passed_validation"] = research_report.passed_validation

            if research_report.error:
                result["error"] = research_report.error

            if research_report.mean_ic != 0.0:
                ic_values.append(research_report.mean_ic)

            logger.info(
                "[Batch] %s: IC=%.4f, passed=%s",
                ticker, research_report.mean_ic, research_report.passed_validation,
            )

        except Exception as e:
            result["error"] = str(e)
            logger.error("[Batch] Error processing %s: %s", ticker, str(e))

        ticker_results.append(result)

    # Aggregate results
    report.ticker_results = ticker_results
    report.tickers_tested = len(tickers)
    report.tickers_passed = sum(1 for r in ticker_results if r["passed_validation"])
    report.pass_rate = (
        report.tickers_passed / report.tickers_tested
        if report.tickers_tested > 0 else 0.0
    )
    report.cross_sectional_consistent = report.pass_rate >= CROSS_SECTIONAL_THRESHOLD
    report.aggregate_ic = float(np.mean(ic_values)) if ic_values else 0.0

    report.summary = (
        f"Feature '{feature_name}' tested across {report.tickers_tested} tickers: "
        f"{report.tickers_passed} passed ({report.pass_rate:.0%}). "
        f"Aggregate IC: {report.aggregate_ic:.4f}. "
        f"{'Cross-sectionally CONSISTENT' if report.cross_sectional_consistent else 'NOT consistent (likely noise)'}."
    )

    logger.info("[Batch] %s", report.summary)

    return report
