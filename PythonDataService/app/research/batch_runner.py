"""Cross-sectional batch runner for options features.

Tests the same feature across multiple tickers to determine
if the effect is cross-sectionally consistent.

Long-running by nature (one IV-history rebuild + IC test per ticker),
so the runner accepts optional ``on_phase``, ``on_log``, ``on_progress``,
and ``cancel_check`` callables. The Jobs/SSE wrapper in
``app/routers/jobs.py`` plugs ``ProgressEmitter`` into these so the
data-lab-style run-dock can show per-ticker phase events as they happen.
The callbacks are no-ops by default — direct synchronous callers (tests,
GraphQL one-shot) get the same behaviour as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.research.options.iv_builder import build_iv_history
from app.research.options_runner import run_options_feature_research
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

CROSS_SECTIONAL_THRESHOLD = 0.60  # 60% pass rate = consistent

# Optional progress callbacks used by the Jobs/SSE wrapper. Default no-ops
# keep synchronous callers (existing GraphQL endpoint, tests) unaffected.
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]
"""(current, total, message) — current is 0..N completed, total is N tickers."""
CancelCallback = Callable[[], bool]


def _noop_phase(phase: str) -> None:
    return None


def _noop_log(message: str) -> None:
    return None


def _noop_progress(current: int, total: int, message: str) -> None:
    return None


def _no_cancel() -> bool:
    return False


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
    on_phase: PhaseCallback = _noop_phase,
    on_log: LogCallback = _noop_log,
    on_progress: ProgressCallback = _noop_progress,
    cancel_check: CancelCallback = _no_cancel,
) -> CrossSectionalReport:
    """Run a feature across multiple tickers and aggregate results.

    Processes tickers sequentially with per-ticker status logging.
    Optional callbacks let an SSE wrapper surface per-ticker phase / log
    / progress events; cancel_check() returning True aborts the loop.
    """
    report = CrossSectionalReport(feature_name=feature_name)
    n = len(tickers)

    ticker_results: list[dict[str, Any]] = []
    ic_values: list[float] = []

    on_phase("starting")
    on_log(f"Cross-sectional study: feature={feature_name}, target={target_type}, tickers={n}")
    on_log(f"Date range: {start_date} → {end_date}")

    for i, ticker in enumerate(tickers):
        if cancel_check():
            on_log(f"Cancelled before {ticker}")
            break

        logger.info(
            "[Batch] Processing ticker %d/%d: %s",
            i + 1,
            n,
            ticker,
        )

        phase_id = f"ticker_{i + 1}_{ticker}"
        on_phase(phase_id)
        on_progress(i, n, f"Processing {ticker} ({i + 1}/{n})")
        on_log(f"[{i + 1}/{n}] {ticker}: building IV history...")

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
                on_log(f"[{i + 1}/{n}] {ticker}: SKIP — no IV data")
                on_progress(i + 1, n, f"{ticker}: skipped (no IV data)")
                continue

            result["data_points"] = len(iv_df)
            on_log(f"[{i + 1}/{n}] {ticker}: {len(iv_df)} IV days; fetching daily bars...")

            if cancel_check():
                on_log(f"Cancelled mid-{ticker}")
                ticker_results.append(result)
                break

            # Step 2: Get stock daily bars
            stock_bars = polygon_client.fetch_aggregates(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                from_date=start_date,
                to_date=end_date,
                adjusted=True,
            )

            if not stock_bars:
                result["error"] = "No stock data available"
                ticker_results.append(result)
                on_log(f"[{i + 1}/{n}] {ticker}: SKIP — no stock bars")
                on_progress(i + 1, n, f"{ticker}: skipped (no stock data)")
                continue

            on_log(f"[{i + 1}/{n}] {ticker}: {len(stock_bars)} daily bars; running IC validation...")

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

            verdict = "PASS" if research_report.passed_validation else "FAIL"
            on_log(
                f"[{i + 1}/{n}] {ticker}: {verdict} — IC={research_report.mean_ic:.4f}, "
                f"NW t={research_report.nw_t_stat:.2f}, p={research_report.nw_p_value:.4f}, "
                f"N_eff={research_report.effective_n:.0f}"
            )
            on_progress(i + 1, n, f"{ticker}: {verdict} (IC={research_report.mean_ic:.4f})")

            logger.info(
                "[Batch] %s: IC=%.4f, passed=%s",
                ticker,
                research_report.mean_ic,
                research_report.passed_validation,
            )

        except Exception as e:
            result["error"] = str(e)
            on_log(f"[{i + 1}/{n}] {ticker}: ERROR — {e}")
            on_progress(i + 1, n, f"{ticker}: error")
            logger.error("[Batch] Error processing %s: %s", ticker, str(e))

        ticker_results.append(result)

    # Aggregate results
    report.ticker_results = ticker_results
    report.tickers_tested = len(tickers)
    report.tickers_passed = sum(1 for r in ticker_results if r["passed_validation"])
    report.pass_rate = report.tickers_passed / report.tickers_tested if report.tickers_tested > 0 else 0.0
    report.cross_sectional_consistent = report.pass_rate >= CROSS_SECTIONAL_THRESHOLD
    report.aggregate_ic = float(np.mean(ic_values)) if ic_values else 0.0

    report.summary = (
        f"Feature '{feature_name}' tested across {report.tickers_tested} tickers: "
        f"{report.tickers_passed} passed ({report.pass_rate:.0%}). "
        f"Aggregate IC: {report.aggregate_ic:.4f}. "
        f"{'Cross-sectionally CONSISTENT' if report.cross_sectional_consistent else 'NOT consistent (likely noise)'}."
    )

    on_phase("aggregating")
    on_log(report.summary)
    on_progress(n, n, "Aggregating results")
    logger.info("[Batch] %s", report.summary)

    return report
