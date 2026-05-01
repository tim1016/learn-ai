"""Research experiment orchestrator.

Coordinates: data → feature → target → IC → stationarity → quantiles → report.

Long-running for minute-bar studies, so the orchestrator accepts optional
``on_phase`` / ``on_log`` / ``on_progress`` / ``cancel_check`` callables.
The Jobs/SSE wrapper in ``app/routers/jobs.py`` plugs ``ProgressEmitter``
into these so the run-progress panel shows what stage we're in. The
callables are no-ops by default — synchronous callers (the existing
GraphQL endpoint, tests) get the same behaviour as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
from app.research.target import (
    TargetResult,
    compute_forward_log_return,
    validate_return_series,
)
from app.research.validation.ic import compute_information_coefficient
from app.research.validation.quantile import compute_quantile_analysis
from app.research.validation.robustness import RobustnessResult, compute_robustness

logger = logging.getLogger(__name__)


# Optional progress callbacks used by the Jobs/SSE wrapper. Default no-ops
# keep synchronous callers (existing GraphQL endpoint, tests) unaffected.
PhaseCallback = Callable[[str], None]
LogCallback = Callable[[str, str], None]
"""(message, level) — level is "info" / "warn" / "error"."""
ProgressCallback = Callable[[int, int, str, "str | None"], None]
"""(current, total, unit, message)."""
CancelCallback = Callable[[], bool]


def _noop_phase(phase: str) -> None:
    return None


def _noop_log(message: str, level: str = "info") -> None:
    return None


def _noop_progress(current: int, total: int, unit: str = "windows", message: str | None = None) -> None:
    return None


def _no_cancel() -> bool:
    return False


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

    # Target metadata — what was actually computed (horizon in minutes,
    # bar spacing, timezone, valid ratio, drop-reason breakdown). Used
    # by the UI disclosure to make "wrong target" mismatches visible.
    target: TargetResult | None = None

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
    on_phase: PhaseCallback = _noop_phase,
    on_log: LogCallback = _noop_log,
    on_progress: ProgressCallback = _noop_progress,
    cancel_check: CancelCallback = _no_cancel,
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
    on_phase, on_log, on_progress, cancel_check :
        Optional callbacks for the Jobs/SSE wrapper. Default no-ops.

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

        # ── Phase: forward returns ───────────────────────────────────
        cancel_check()
        on_phase("compute_target")
        on_log(
            f"Computing forward log returns over {len(bars):,} bars "
            f"(horizon = {config.horizon_minutes} min)"
        )
        target_result = compute_forward_log_return(
            bars=bars,
            horizon_minutes=config.horizon_minutes,
        )
        report.target = target_result
        target_returns = target_result.values
        if not validate_return_series(target_returns):
            raise ValueError(
                "Target return series failed validation "
                f"({target_result.valid_count} valid / {target_result.total_count} total; "
                f"reasons={target_result.invalid_reason_counts})."
            )

        # ── Phase: feature ───────────────────────────────────────────
        on_phase("compute_feature")
        on_log(f"Computing feature '{feature_name}' on {ticker}")
        feature_values = TechnicalFeatures.compute_feature(feature_name, bars)

        # ── Phase: IC ────────────────────────────────────────────────
        cancel_check()
        on_phase("compute_ic")
        on_log("Measuring information coefficient (rolling daily IC + Newey-West t-stat)")
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
        on_log(
            f"IC = {ic_result.mean_ic:+.4f} (Newey-West t = {ic_result.nw_t_stat:.2f}, "
            f"p = {ic_result.nw_p_value:.4f}, effective N = {ic_result.effective_n:.0f})"
        )

        # ── Phase: stationarity ──────────────────────────────────────
        on_phase("stationarity")
        clean_feature = feature_values.dropna().values
        if len(clean_feature) >= 20:
            on_log("Running ADF and KPSS stationarity tests on the feature series")
            stationarity = run_stationarity_tests(
                clean_feature,
                adf_significance=config.adf_significance,
                kpss_significance=config.kpss_significance,
            )
            report.adf_pvalue = stationarity.adf_pvalue
            report.kpss_pvalue = stationarity.kpss_pvalue
            report.is_stationary = stationarity.is_stationary
            on_log(
                f"{'Stationary' if stationarity.is_stationary else 'Non-stationary'} "
                f"(ADF p = {stationarity.adf_pvalue:.4f}, KPSS p = {stationarity.kpss_pvalue:.4f})"
            )
        else:
            on_log("Feature series too short for stationarity test — skipping", level="warn")
            logger.warning("[Research] Feature series too short for stationarity test")

        # ── Phase: quantile ──────────────────────────────────────────
        cancel_check()
        on_phase("quantile")
        on_log(f"Splitting feature into {config.n_bins} quantile buckets and checking monotonicity")
        quantile_result = compute_quantile_analysis(
            feature_values,
            target_returns,
            n_bins=config.n_bins,
            monotonicity_threshold=config.monotonicity_threshold,
        )
        report.quantile_bins = [asdict(b) for b in quantile_result.bins]
        report.is_monotonic = quantile_result.is_monotonic
        report.monotonicity_ratio = quantile_result.monotonicity_ratio
        on_log(
            f"{'Monotonic' if quantile_result.is_monotonic else 'Non-monotonic'} "
            f"(monotonicity ratio = {quantile_result.monotonicity_ratio:.2f})"
        )

        # ── Phase: robustness ────────────────────────────────────────
        cancel_check()
        on_phase("robustness")
        if len(ic_result.daily_ic_values) >= 2:
            on_log("Running robustness checks: monthly stability, regime breakdown, train/test split")
            report.robustness = compute_robustness(
                daily_ic_values=ic_result.daily_ic_values,
                daily_ic_dates=ic_result.daily_ic_dates,
                bars=bars,
            )
            if report.robustness is not None and report.robustness.train_test is not None:
                tt = report.robustness.train_test
                on_log(
                    f"Out-of-sample retention = {tt.oos_retention:.0%} "
                    f"(train IC {tt.train_mean_ic:+.4f} → test IC {tt.test_mean_ic:+.4f}, "
                    f"{tt.test_days} test days)"
                )
        else:
            on_log("Not enough daily IC values for robustness analysis — skipping", level="warn")

        # ── Phase: validate ──────────────────────────────────────────
        on_phase("validate")
        on_log("Scoring feature against per-feature validation contract (4 screens, 0/1/2/3 ladder)")
        spec = get_spec(feature_name)
        report.feature_spec = spec

        # Use NW p-value when available as it accounts for autocorrelation
        effective_p = ic_result.nw_p_value if ic_result.nw_p_value < 1.0 else ic_result.ic_p_value

        train_test = report.robustness.train_test if report.robustness is not None else None
        train_test_present = train_test is not None
        test_days = train_test.test_days if train_test is not None else 0
        test_mean_ic = train_test.test_mean_ic if train_test is not None else 0.0
        oos_retention = train_test.oos_retention if train_test is not None else 0.0

        regimes_observed = 0
        regime_sign_flip_fraction = 0.0
        if report.robustness is not None:
            all_regimes = (
                report.robustness.volatility_regimes + report.robustness.trend_regimes
            )
            regimes_observed = len(all_regimes)
            if regimes_observed > 0 and ic_result.mean_ic != 0:
                # Sign of the headline IC; a regime "flips" when its IC sign
                # disagrees. Anchored on the headline so the screen reflects
                # "does the spec-direction story hold across regimes" rather
                # than just bucket-by-bucket positivity.
                overall_sign = 1 if ic_result.mean_ic > 0 else -1
                flips = sum(
                    1 for r in all_regimes
                    if r.mean_ic != 0 and (1 if r.mean_ic > 0 else -1) != overall_sign
                )
                regime_sign_flip_fraction = flips / regimes_observed

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
            regime_sign_flip_fraction=regime_sign_flip_fraction,
        )

        # v2 review: passed_validation must collapse to "research-grade
        # or better", not "any statistical association". Stage 1 means
        # "in-sample IC is real but trading is dead-on-arrival" — that
        # is **not** a passed-validation result for unmigrated callers.
        report.passed_validation = report.validation_verdict.stage_info.stage >= 2

        stage_label = report.validation_verdict.stage_info.label
        on_log(
            f"Verdict: Stage {report.validation_verdict.stage_info.stage} — {stage_label}",
            level="info" if report.passed_validation else "warn",
        )
        on_progress(1, 1, "stages", "Validation complete")

        logger.info(
            "[Research] Complete: %s %s — stage=%d (%s) IC=%.4f, NW p=%.4f, "
            "stationary=%s, monotonic=%s, OOS retention=%.0f%%, "
            "regime flips=%.0f%%",
            ticker,
            feature_name,
            report.validation_verdict.stage_info.stage,
            report.validation_verdict.stage_info.label,
            report.mean_ic,
            effective_p,
            report.is_stationary,
            report.is_monotonic,
            oos_retention * 100.0,
            regime_sign_flip_fraction * 100.0,
        )

    except Exception as e:
        # Defer-to-wrapper: when the SSE wrapper's cancel_check raises
        # JobCancelled, we let it propagate so run_in_thread emits
        # job.cancelled instead of job.failed. We can't import
        # JobCancelled here (research → jobs would be a layering
        # inversion), so we sniff by exception class name.
        if type(e).__name__ == "JobCancelled":
            raise
        logger.error("[Research] Error: %s", str(e), exc_info=True)
        report.error = str(e)
        report.passed_validation = False

    return report
