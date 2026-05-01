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

Inferential machinery (added 2026-05-01 after methodology review):

* **Per-ticker validity classification.** Each ticker gets a
  ``validity`` tag — ``valid`` / ``invalid_iv`` / ``invalid_data`` /
  ``error`` — so the aggregate denominators can exclude tickers whose
  IC never ran. Previously a ticker with bad IV diagnostics still
  showed up in the table as ``FAIL — N_eff=0, p=1.0``, conflating
  "feature failed" with "data was unusable".
* **Binomial null test** replaces the hard-coded 60 % pass-rate
  threshold. Given ``k`` of ``n_valid`` tickers passed at per-ticker
  α = 0.05, the test reports ``P(k or more | null of zero true IC)``.
* **Precision-weighted aggregate IC** with a Lo (2002) confidence
  interval. Weights are per-ticker N_eff so SPY (long sample) does not
  weigh equally with a thin small-cap.
* **N_eff_assets** via the eigenvalue method on the cross-asset
  correlation matrix. Treats SPY/QQQ/AAPL as the ~1.5 independent
  observations they actually are rather than 3.
* **Graduation ladder (Stage 0–3)** mirroring the Signal Engine
  ladder, with concrete numerical thresholds derived from the
  external methodology review.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

from app.research.options.iv_builder import build_iv_history
from app.research.options_runner import run_options_feature_research
from app.services.polygon_client import PolygonClientService

logger = logging.getLogger(__name__)

# Legacy threshold — kept to populate ``cross_sectional_consistent`` for
# backward compatibility with consumers that still read it. New consumers
# should use the binomial test result + graduation ladder instead.
CROSS_SECTIONAL_THRESHOLD = 0.60

# Per-ticker null α for the binomial test on the pass count.
PER_TICKER_ALPHA = 0.05

# Below this per-ticker N_eff, the ticker is flagged ``low_confidence``
# even when it computes a valid IC. Used by the UI to render a soft
# warning ("rate is technically defined but precision is poor") on
# rows where the headline IC shouldn't be trusted at face value.
LOW_CONFIDENCE_NEFF_THRESHOLD = 10.0

# Minimum per-ticker IC observations before we trust the IC-time-series
# correlation matrix for the ``N_eff_assets`` calculation. Below this the
# IC sample per ticker is too short to estimate cross-ticker correlation
# reliably and we fall back to the daily-stock-returns correlation.
N_EFF_ASSETS_IC_OBS_MIN = 5

# Stage 0 kill-switch thresholds (authority: see PR description).
STAGE0_VALID_TICKERS_MIN = 2
STAGE0_MEAN_NEFF_MIN = 20.0
STAGE0_AGGREGATE_IC_FLOOR = 0.02
STAGE0_BINOMIAL_P_MAX = 0.20

# Stage 2 thresholds.
STAGE2_VALID_TICKERS_MIN = 5
STAGE2_NEFF_ASSETS_MIN = 3.0
STAGE2_AGGREGATE_IC_MIN = 0.03
STAGE2_BINOMIAL_P_MAX = 0.05

# Stage 3 thresholds.
STAGE3_VALID_TICKERS_MIN = 10
STAGE3_NEFF_ASSETS_MIN = 5.0
STAGE3_AGGREGATE_IC_MIN = 0.05


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


# ─── Result dataclasses ───────────────────────────────────────────────────


@dataclass
class TickerValidity:
    """Counts of ticker outcomes by validity class."""

    valid: int = 0
    invalid_iv: int = 0
    invalid_data: int = 0
    errored: int = 0


@dataclass
class AggregateIC:
    """Precision-weighted aggregate IC with Lo-style confidence interval.

    The SE is computed as ``1 / sqrt(sum(w_i))`` with ``w_i = N_eff_i``,
    which assumes per-ticker IC variance ≈ ``1 / N_eff_i``. That is a
    Stage 1 / Stage 2 approximation; the full Lo (2002) form
    ``(1 + IC²) / N_eff_i`` is a Stage 3 upgrade. The
    ``se_approximation_note`` field carries the disclaimer the UI
    renders next to the CI so the reader knows what's pinned and what's
    approximated.
    """

    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    weighting_method: str = "precision"
    se_approximation_note: str = (
        "SE assumes per-ticker IC variance ≈ 1/N_eff (Stage 1 approximation). "
        "Lo (2002)'s full (1 + IC²)/N_eff form is a Stage 3 upgrade."
    )
    n_tickers_used: int = 0
    sum_weights: float = 0.0
    valid: bool = False


@dataclass
class BinomialNullTest:
    """One-sided binomial test on the count of tickers that passed.

    Under H_0 of zero true IC across all tickers, each ticker's
    individual pass is a Bernoulli trial with p = ``alpha_per_ticker``.
    The reported ``p_value`` is P(K ≥ k_passed) under that null,
    using ``n_eff_assets`` instead of the raw count when correlated
    asset returns inflate the effective sample.
    """

    n_valid: int = 0
    n_eff_assets: float = 0.0
    n_passed: int = 0
    alpha_per_ticker: float = PER_TICKER_ALPHA
    p_value: float = 1.0
    significant: bool = False


@dataclass
class CrossSectionalCriterion:
    """One programmable criterion in the graduation ladder."""

    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


@dataclass
class CrossSectionalStageInfo:
    """Where a feature sits on the cross-sectional 0/1/2/3 ladder."""

    stage: int = 0
    """0 = Rejected, 1 = Weak, 2 = Research candidate, 3 = Promotion."""
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    failed_criteria: list[CrossSectionalCriterion] = field(default_factory=list)
    """Stage 0 criteria that triggered the rejection. Empty for Stage ≥ 1."""
    advance_criteria: list[CrossSectionalCriterion] = field(default_factory=list)
    """What's needed to advance from the current stage."""


@dataclass
class CrossSectionalReport:
    """Aggregate result of a cross-sectional run."""

    feature_name: str = ""
    target_type: str = ""

    # Denominators — split so consumers don't conflate "raw requested"
    # with "actually had usable data".
    tickers_tested_raw: int = 0
    tickers_valid: int = 0
    tickers_passed: int = 0
    pass_rate: float = 0.0
    """tickers_passed / tickers_valid (not / tickers_tested_raw)."""

    validity_summary: TickerValidity = field(default_factory=TickerValidity)

    # Aggregates
    aggregate_ic: float = 0.0
    """Precision-weighted mean IC across valid tickers. Same field name
    as legacy for backward compat, but the value is now weighted."""
    aggregate_ic_uniform: float = 0.0
    """Unweighted mean — kept for diagnostic comparison only."""
    aggregate_ic_ci: AggregateIC = field(default_factory=AggregateIC)

    # Statistical machinery
    binomial_test: BinomialNullTest = field(default_factory=BinomialNullTest)
    n_eff_assets: float = 0.0
    """Effective number of independent assets via the eigenvalue method
    on the cross-asset correlation matrix."""
    n_eff_assets_method: str = "returns"
    """Which correlation drove ``n_eff_assets`` — ``"ic"`` (per-ticker
    IC time series, the Stage-2-correct version) or ``"returns"`` (daily
    stock returns, Stage-1 fallback when IC sample per ticker is too
    short for a stable estimate). Surfaced in the UI tooltip so the
    reader knows which dependence structure was used."""

    # Legacy convenience
    cross_sectional_consistent: bool = False
    """Kept for backward compat; new consumers should read
    ``binomial_test.significant`` and ``stage_info.stage`` instead."""

    # Graduation
    stage_info: CrossSectionalStageInfo = field(default_factory=CrossSectionalStageInfo)

    # Per-ticker rows (now with ``validity`` field)
    ticker_results: list[dict[str, Any]] = field(default_factory=list)

    summary: str = ""


# ─── Main entry ───────────────────────────────────────────────────────────


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
    """Run a feature across multiple tickers and aggregate results."""
    n = len(tickers)

    ticker_results: list[dict[str, Any]] = []
    # Per-ticker daily-return series, keyed by ticker, used for the
    # cross-asset correlation matrix at aggregation time. Only populated
    # for valid tickers.
    per_ticker_returns: dict[str, pd.Series] = {}
    # Per-ticker IC time series, keyed by ticker. Used by the IC-based
    # N_eff_assets when every valid ticker has enough IC observations;
    # otherwise we fall back to per_ticker_returns.
    per_ticker_ic_series: dict[str, pd.Series] = {}

    on_phase("starting")
    on_log(f"Cross-sectional study: feature={feature_name}, target={target_type}, tickers={n}")
    on_log(f"Date range: {start_date} → {end_date}")

    for i, ticker in enumerate(tickers):
        if cancel_check():
            on_log(f"Cancelled before {ticker}")
            break

        logger.info("[Batch] Processing ticker %d/%d: %s", i + 1, n, ticker)

        phase_id = f"ticker_{i + 1}_{ticker}"
        on_phase(phase_id)
        on_progress(i, n, f"Processing {ticker} ({i + 1}/{n})")
        on_log(f"[{i + 1}/{n}] {ticker}: building IV history...")

        result: dict[str, Any] = _empty_ticker_result(ticker)

        try:
            iv_df = build_iv_history(
                underlying=ticker,
                start_date=start_date,
                end_date=end_date,
                polygon_client=polygon_client,
            )

            if iv_df.empty:
                result["error"] = "No IV data could be derived"
                result["validity"] = "invalid_data"
                ticker_results.append(result)
                on_log(f"[{i + 1}/{n}] {ticker}: INVALID — no IV data")
                on_progress(i + 1, n, f"{ticker}: invalid (no IV data)")
                continue

            result["data_points"] = len(iv_df)
            on_log(f"[{i + 1}/{n}] {ticker}: {len(iv_df)} IV days; fetching daily bars...")

            if cancel_check():
                on_log(f"Cancelled mid-{ticker}")
                ticker_results.append(result)
                break

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
                result["validity"] = "invalid_data"
                ticker_results.append(result)
                on_log(f"[{i + 1}/{n}] {ticker}: INVALID — no stock bars")
                on_progress(i + 1, n, f"{ticker}: invalid (no stock data)")
                continue

            on_log(f"[{i + 1}/{n}] {ticker}: {len(stock_bars)} daily bars; running IC validation...")

            iv_data = iv_df.to_dict(orient="records")
            for row in iv_data:
                row["atm_iv"] = row.get("iv_30d_atm")
                row["iv_otm_put"] = row.get("iv_30d_put")
                row["iv_otm_call"] = row.get("iv_30d_call")

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
                # IC pipeline raised — typically IV diagnostics. The
                # research_report carries the error message but never
                # ran the IC computation, so the ticker is INVALID,
                # not "FAIL".
                result["error"] = research_report.error
                result["validity"] = (
                    "invalid_iv"
                    if "diagnostic" in research_report.error.lower() or "iv" in research_report.error.lower()
                    else "error"
                )
                on_log(f"[{i + 1}/{n}] {ticker}: INVALID — {research_report.error}")
                on_progress(i + 1, n, f"{ticker}: invalid")
            else:
                result["validity"] = "valid"
                # Flag tickers whose effective sample is too thin for
                # the per-ticker IC to be trusted at face value. The
                # row still counts toward the aggregate (N_eff weights
                # it down naturally) but the UI annotates it.
                result["low_confidence"] = (
                    research_report.effective_n < LOW_CONFIDENCE_NEFF_THRESHOLD
                )

                # Stash daily returns + per-ticker IC series for the
                # N_eff_assets calculation below.
                returns = _extract_daily_returns(stock_bars)
                if not returns.empty:
                    per_ticker_returns[ticker] = returns
                if research_report.ic_values and research_report.ic_dates:
                    per_ticker_ic_series[ticker] = pd.Series(
                        research_report.ic_values,
                        index=research_report.ic_dates,
                    )

                verdict = "PASS" if research_report.passed_validation else "FAIL"
                on_log(
                    f"[{i + 1}/{n}] {ticker}: {verdict} — IC={research_report.mean_ic:.4f}, "
                    f"NW t={research_report.nw_t_stat:.2f}, p={research_report.nw_p_value:.4f}, "
                    f"N_eff={research_report.effective_n:.0f}"
                    f"{' (low confidence)' if result['low_confidence'] else ''}"
                )
                on_progress(i + 1, n, f"{ticker}: {verdict} (IC={research_report.mean_ic:.4f})")

            logger.info(
                "[Batch] %s: validity=%s IC=%.4f passed=%s",
                ticker,
                result["validity"],
                research_report.mean_ic,
                research_report.passed_validation,
            )

        except Exception as e:
            # Defer-to-wrapper: when the SSE wrapper's cancel_check raises
            # JobCancelled mid-ticker (e.g. the inner `if cancel_check():`
            # at the data-fetch boundary), let it propagate so
            # run_in_thread emits job.cancelled instead of marking the
            # ticker as a per-row "error" and letting the loop finish
            # `completed`. We can't import JobCancelled here (research →
            # jobs would be a layering inversion), so we sniff by class
            # name — same pattern as runner.py / signal/engine.py.
            if type(e).__name__ == "JobCancelled":
                raise
            result["error"] = str(e)
            result["validity"] = "error"
            on_log(f"[{i + 1}/{n}] {ticker}: ERROR — {e}")
            on_progress(i + 1, n, f"{ticker}: error")
            logger.error("[Batch] Error processing %s: %s", ticker, str(e))

        ticker_results.append(result)

    # ── Aggregation ──────────────────────────────────────────────────────
    on_phase("aggregating")

    valid_results = [r for r in ticker_results if r["validity"] == "valid"]
    validity_summary = _summarize_validity(ticker_results)

    aggregate_ic_ci = _compute_weighted_aggregate_ic(valid_results)
    aggregate_ic_uniform = (
        float(np.mean([r["mean_ic"] for r in valid_results])) if valid_results else 0.0
    )

    n_eff_assets, n_eff_assets_method = _compute_n_eff_assets_with_method(
        ic_series=per_ticker_ic_series,
        returns=per_ticker_returns,
    )

    binomial = _compute_binomial_null_test(valid_results, n_eff_assets)

    stage_info = _compute_stage_info(
        valid_results=valid_results,
        aggregate=aggregate_ic_ci,
        binomial=binomial,
        n_eff_assets=n_eff_assets,
    )

    tickers_passed = sum(1 for r in valid_results if r["passed_validation"])
    pass_rate = tickers_passed / len(valid_results) if valid_results else 0.0

    summary = _generate_summary(
        feature_name=feature_name,
        validity=validity_summary,
        tickers_passed=tickers_passed,
        pass_rate=pass_rate,
        aggregate_ic=aggregate_ic_ci.point,
        binomial=binomial,
        stage=stage_info.stage,
        stage_label=stage_info.label,
    )

    on_log(summary)
    on_progress(n, n, "Aggregating results")
    logger.info("[Batch] %s", summary)

    return CrossSectionalReport(
        feature_name=feature_name,
        target_type=target_type,
        tickers_tested_raw=len(tickers),
        tickers_valid=len(valid_results),
        tickers_passed=tickers_passed,
        pass_rate=pass_rate,
        validity_summary=validity_summary,
        aggregate_ic=aggregate_ic_ci.point,
        aggregate_ic_uniform=aggregate_ic_uniform,
        aggregate_ic_ci=aggregate_ic_ci,
        binomial_test=binomial,
        n_eff_assets=n_eff_assets,
        n_eff_assets_method=n_eff_assets_method,
        cross_sectional_consistent=pass_rate >= CROSS_SECTIONAL_THRESHOLD,
        stage_info=stage_info,
        ticker_results=ticker_results,
        summary=summary,
    )


# ─── Helpers ──────────────────────────────────────────────────────────────


def _empty_ticker_result(ticker: str) -> dict[str, Any]:
    return {
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
        "validity": "valid",  # overwritten if anything fails
        "low_confidence": False,
    }


def _summarize_validity(results: list[dict[str, Any]]) -> TickerValidity:
    s = TickerValidity()
    for r in results:
        v = r.get("validity", "valid")
        if v == "valid":
            s.valid += 1
        elif v == "invalid_iv":
            s.invalid_iv += 1
        elif v == "invalid_data":
            s.invalid_data += 1
        else:
            s.errored += 1
    return s


def _extract_daily_returns(stock_bars: list[dict[str, Any]]) -> pd.Series:
    """Daily simple returns indexed by date string. Used only to build
    the cross-asset correlation matrix for ``N_eff_assets``."""
    if not stock_bars:
        return pd.Series(dtype=float)
    df = pd.DataFrame(stock_bars).sort_values("timestamp").reset_index(drop=True)
    if "close" not in df.columns or "timestamp" not in df.columns:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")
    df["ret"] = df["close"].astype(float).pct_change()
    return pd.Series(df["ret"].values, index=df["date"].values).dropna()


def _compute_weighted_aggregate_ic(valid_results: list[dict[str, Any]]) -> AggregateIC:
    """Precision-weighted mean of per-ticker mean IC, with Lo-style CI.

    Each ticker's IC is itself a sample mean over its own observations; its
    sampling variance under H_0 is approximately ``1 / N_eff_i``. The
    inverse-variance (precision) weight is therefore ``w_i = N_eff_i``.
    The weighted mean has SE = ``1 / sqrt(sum(w_i))``.

    Returns ``valid=False`` when no ticker has positive N_eff — the UI
    must render "insufficient sample" instead of a misleading band.
    """
    weights: list[float] = []
    ics: list[float] = []
    for r in valid_results:
        w = float(r.get("effective_n", 0.0))
        if w <= 0 or not np.isfinite(w):
            continue
        weights.append(w)
        ics.append(float(r["mean_ic"]))

    if not weights:
        return AggregateIC(valid=False)

    sum_w = float(np.sum(weights))
    if sum_w <= 0:
        return AggregateIC(valid=False, n_tickers_used=len(weights), sum_weights=sum_w)

    weighted_mean = float(np.sum(np.array(weights) * np.array(ics)) / sum_w)
    se = float(1.0 / np.sqrt(sum_w))
    z = float(scipy_stats.norm.ppf(0.975))  # 95 % two-sided

    return AggregateIC(
        point=weighted_mean,
        se=se,
        ci_lower=weighted_mean - z * se,
        ci_upper=weighted_mean + z * se,
        confidence_level=0.95,
        weighting_method="precision",
        n_tickers_used=len(weights),
        sum_weights=sum_w,
        valid=True,
    )


def _compute_n_eff_assets(per_ticker_returns: dict[str, pd.Series]) -> float:
    """Effective number of independent assets via eigenvalue decomposition
    of the cross-asset correlation matrix.

    For correlation matrix R with eigenvalues λ_i,
    ``N_eff = (Σλ)² / Σλ²``. For a perfectly correlated set this is 1;
    for orthogonal series it equals the raw count. Caps at the raw
    count and floors at 1.

    Generic over the input series — pass returns or per-ticker IC time
    series; the method is the same. ``_compute_n_eff_assets_with_method``
    picks which to use based on sample sizes.
    """
    n_raw = len(per_ticker_returns)
    if n_raw < 2:
        return float(n_raw)

    df = pd.DataFrame(per_ticker_returns)
    df = df.dropna()
    if len(df) < 5 or df.shape[1] < 2:
        return float(n_raw)

    corr = df.corr().to_numpy()
    if not np.all(np.isfinite(corr)):
        return float(n_raw)

    eigvals = np.linalg.eigvalsh(corr)
    eigvals = eigvals[eigvals > 1e-10]
    if eigvals.size == 0:
        return float(n_raw)

    sum_lam = float(np.sum(eigvals))
    sum_lam_sq = float(np.sum(eigvals**2))
    if sum_lam_sq <= 0:
        return float(n_raw)

    n_eff = (sum_lam**2) / sum_lam_sq
    return float(min(max(n_eff, 1.0), n_raw))


def _compute_n_eff_assets_with_method(
    ic_series: dict[str, pd.Series],
    returns: dict[str, pd.Series],
) -> tuple[float, str]:
    """Pick the right input matrix for ``N_eff_assets`` and report which.

    The Stage-2-correct version measures correlation across the
    *per-ticker IC time series* — that is the actual dependence
    structure being tested for cross-sectional consistency. But IC
    series are short (typically 5–20 rolling-window observations),
    so the correlation matrix on them is unstable when any ticker's
    IC sample falls below ``N_EFF_ASSETS_IC_OBS_MIN``.

    Decision rule:

    * If every ticker has ≥ ``N_EFF_ASSETS_IC_OBS_MIN`` IC observations,
      use the IC correlation matrix and tag the result ``"ic"``.
    * Otherwise fall back to daily-returns correlation and tag the
      result ``"returns"`` so the UI can disclose which structure was
      used.

    Returns ``(n_eff, method)``.
    """
    if ic_series and all(len(s) >= N_EFF_ASSETS_IC_OBS_MIN for s in ic_series.values()):
        return _compute_n_eff_assets(ic_series), "ic"
    return _compute_n_eff_assets(returns), "returns"


def _compute_binomial_null_test(
    valid_results: list[dict[str, Any]],
    n_eff_assets: float,
    alpha: float = PER_TICKER_ALPHA,
) -> BinomialNullTest:
    """One-sided binomial test on the count of tickers that passed.

    Replaces the hard-coded 60 % pass-rate threshold. With
    ``n`` = effective number of independent assets, ``k`` = number that
    passed, and per-ticker false-positive rate ``alpha`` under the null,
    reports ``P(K ≥ k | Binomial(n, alpha))``. Below 0.05 means the
    pass count is significantly more than chance under the null
    of zero true IC.
    """
    n_valid = len(valid_results)
    if n_valid == 0:
        return BinomialNullTest(alpha_per_ticker=alpha)

    n_passed = sum(1 for r in valid_results if r["passed_validation"])

    # If n_eff_assets is meaningfully smaller than n_valid (correlated
    # universe), rescale BOTH the trial count AND the success count by
    # the same factor. Bumping only n_for_test would silently drop the
    # correction when k > n_eff. The whole point of the eigenvalue
    # adjustment is that "k of n_valid passes" carries less evidence
    # when the assets aren't independent.
    if n_eff_assets > 0 and n_eff_assets < n_valid:
        scale = n_eff_assets / float(n_valid)
        n_for_test = max(round(n_eff_assets), 1)
        n_passed_for_test = max(round(n_passed * scale), 0)
    else:
        n_for_test = n_valid
        n_passed_for_test = n_passed

    n_passed_for_test = min(n_passed_for_test, n_for_test)

    p_value = float(scipy_stats.binom.sf(n_passed_for_test - 1, n_for_test, alpha))
    significant = p_value < alpha

    return BinomialNullTest(
        n_valid=n_valid,
        n_eff_assets=n_eff_assets,
        n_passed=n_passed,
        alpha_per_ticker=alpha,
        p_value=p_value,
        significant=significant,
    )


def _compute_stage_info(
    valid_results: list[dict[str, Any]],
    aggregate: AggregateIC,
    binomial: BinomialNullTest,
    n_eff_assets: float,
) -> CrossSectionalStageInfo:
    """Determine 0/1/2/3 stage and the criteria to advance from it."""
    n_valid = len(valid_results)
    mean_neff = (
        float(np.mean([r["effective_n"] for r in valid_results])) if valid_results else 0.0
    )
    abs_ic = abs(aggregate.point) if aggregate.valid else 0.0
    ci_excludes_zero = aggregate.valid and (aggregate.ci_upper < 0 or aggregate.ci_lower > 0)
    ci_includes_zero = aggregate.valid and not ci_excludes_zero

    # ── Stage 0 — Reject ────────────────────────────────────────────
    failed: list[CrossSectionalCriterion] = []
    if n_valid < STAGE0_VALID_TICKERS_MIN:
        failed.append(
            CrossSectionalCriterion(
                name="Valid tickers",
                description="At least 2 tickers must produce a valid IC computation",
                current_value=float(n_valid),
                required_repr=f"≥ {STAGE0_VALID_TICKERS_MIN}",
                met=False,
            )
        )
    if valid_results and mean_neff < STAGE0_MEAN_NEFF_MIN:
        failed.append(
            CrossSectionalCriterion(
                name="Mean N_eff per ticker",
                description="Per-ticker effective sample size must average ≥ 20",
                current_value=mean_neff,
                required_repr=f"≥ {STAGE0_MEAN_NEFF_MIN:.0f}",
                met=False,
            )
        )
    if aggregate.valid and ci_includes_zero and abs_ic < STAGE0_AGGREGATE_IC_FLOOR:
        failed.append(
            CrossSectionalCriterion(
                name="Aggregate IC magnitude",
                description=(
                    f"When the 95% CI overlaps zero (current behaviour), "
                    f"|IC| must exceed {STAGE0_AGGREGATE_IC_FLOOR:.2f} to be "
                    f"considered economically meaningful. Equivalently: a CI "
                    f"that excludes zero passes regardless of |IC|."
                ),
                current_value=abs_ic,
                required_repr=f"|IC| > {STAGE0_AGGREGATE_IC_FLOOR:.2f} (since CI overlaps 0)",
                met=False,
            )
        )
    if binomial.p_value > STAGE0_BINOMIAL_P_MAX:
        failed.append(
            CrossSectionalCriterion(
                name="Binomial null test",
                description="Pass count must beat random chance under the null",
                current_value=binomial.p_value,
                required_repr=f"p ≤ {STAGE0_BINOMIAL_P_MAX:.2f}",
                met=False,
            )
        )

    if failed:
        return CrossSectionalStageInfo(
            stage=0,
            label="Rejected",
            description=(
                "Failed one or more Stage 0 kill criteria. The cross-sectional "
                "evidence is consistent with no true effect — try a different "
                "feature, target type, or expand the universe."
            ),
            next_stage_label="",
            failed_criteria=failed,
            advance_criteria=[],
        )

    # ── Stage 3 — Promotion candidate ──────────────────────────────
    stage3_ok = (
        n_valid >= STAGE3_VALID_TICKERS_MIN
        and n_eff_assets >= STAGE3_NEFF_ASSETS_MIN
        and abs_ic >= STAGE3_AGGREGATE_IC_MIN
        and ci_excludes_zero
        and binomial.significant
    )
    if stage3_ok:
        return CrossSectionalStageInfo(
            stage=3,
            label="Promotion Candidate",
            description=(
                "Strong cross-sectional evidence: independent assets, large "
                "aggregate IC, CI excluding zero, and pass count significantly "
                "above null. Cross-sectional sizing is appropriate."
            ),
            next_stage_label="",
        )

    # ── Stage 2 — Research candidate ───────────────────────────────
    stage2_ok = (
        n_valid >= STAGE2_VALID_TICKERS_MIN
        and n_eff_assets >= STAGE2_NEFF_ASSETS_MIN
        and abs_ic >= STAGE2_AGGREGATE_IC_MIN
        and ci_excludes_zero
        and binomial.p_value < STAGE2_BINOMIAL_P_MAX
    )
    if stage2_ok:
        return CrossSectionalStageInfo(
            stage=2,
            label="Research Candidate",
            description=(
                "Defensible cross-sectional claim: feature works across multiple "
                "independent assets at conventional significance."
            ),
            next_stage_label="Promotion Candidate",
            advance_criteria=[
                CrossSectionalCriterion(
                    name="Valid tickers",
                    description="Larger universe for stable cross-sectional claim",
                    current_value=float(n_valid),
                    required_repr=f"≥ {STAGE3_VALID_TICKERS_MIN}",
                    met=n_valid >= STAGE3_VALID_TICKERS_MIN,
                ),
                CrossSectionalCriterion(
                    name="N_eff_assets (eigenvalue)",
                    description="Independence of asset universe",
                    current_value=n_eff_assets,
                    required_repr=f"≥ {STAGE3_NEFF_ASSETS_MIN:.1f}",
                    met=n_eff_assets >= STAGE3_NEFF_ASSETS_MIN,
                ),
                CrossSectionalCriterion(
                    name="Aggregate IC (precision-weighted)",
                    description="Effect size",
                    current_value=abs_ic,
                    required_repr=f"|IC| ≥ {STAGE3_AGGREGATE_IC_MIN:.2f}",
                    met=abs_ic >= STAGE3_AGGREGATE_IC_MIN,
                ),
            ],
        )

    # ── Stage 1 — Weak ─────────────────────────────────────────────
    return CrossSectionalStageInfo(
        stage=1,
        label="Weak Candidate",
        description=(
            "Survived the Stage 0 kill switch but does not yet support a "
            "cross-sectional claim. Inspect per-ticker results, expand the "
            "universe, or try a different target."
        ),
        next_stage_label="Research Candidate",
        advance_criteria=[
            CrossSectionalCriterion(
                name="Valid tickers",
                description="Need ≥ 5 tickers with usable data",
                current_value=float(n_valid),
                required_repr=f"≥ {STAGE2_VALID_TICKERS_MIN}",
                met=n_valid >= STAGE2_VALID_TICKERS_MIN,
            ),
            CrossSectionalCriterion(
                name="N_eff_assets (eigenvalue)",
                description="Independence of asset universe",
                current_value=n_eff_assets,
                required_repr=f"≥ {STAGE2_NEFF_ASSETS_MIN:.1f}",
                met=n_eff_assets >= STAGE2_NEFF_ASSETS_MIN,
            ),
            CrossSectionalCriterion(
                name="Aggregate IC (precision-weighted)",
                description="Effect size",
                current_value=abs_ic,
                required_repr=f"|IC| ≥ {STAGE2_AGGREGATE_IC_MIN:.2f}",
                met=abs_ic >= STAGE2_AGGREGATE_IC_MIN,
            ),
            CrossSectionalCriterion(
                name="Aggregate IC CI",
                description="95 % CI must exclude zero",
                current_value=abs_ic,
                required_repr="CI excludes 0",
                met=ci_excludes_zero,
            ),
            CrossSectionalCriterion(
                name="Binomial null test",
                description="Pass count beats random chance",
                current_value=binomial.p_value,
                required_repr=f"p < {STAGE2_BINOMIAL_P_MAX:.2f}",
                met=binomial.p_value < STAGE2_BINOMIAL_P_MAX,
            ),
        ],
    )


def _generate_summary(
    feature_name: str,
    validity: TickerValidity,
    tickers_passed: int,
    pass_rate: float,
    aggregate_ic: float,
    binomial: BinomialNullTest,
    stage: int,
    stage_label: str,
) -> str:
    parts = [
        f"Feature '{feature_name}': {validity.valid} valid tickers "
        f"({validity.invalid_iv} invalid IV, {validity.invalid_data} no data, "
        f"{validity.errored} errored).",
        f"{tickers_passed} of {validity.valid} passed validation ({pass_rate:.0%}).",
        f"Precision-weighted IC = {aggregate_ic:.4f}.",
        f"Binomial null test: p = {binomial.p_value:.3f} (n_eff_assets = {binomial.n_eff_assets:.1f}).",
        f"Stage {stage} — {stage_label}.",
    ]
    return " ".join(parts)
