"""Tests for the cross-sectional batch_runner inferential helpers.

Covers the four pieces of statistical machinery added after the
external review:

* validity classification & summary
* precision-weighted aggregate IC + Lo-style CI
* eigenvalue-based N_eff_assets
* binomial null test on pass count
* graduation-stage assignment (0/1/2/3)

Each helper is unit-tested in isolation. The full
``run_cross_sectional_study`` pipeline is exercised through the SSE
endpoint smoke test in development; a hermetic full-pipeline test
would require mocking Polygon + IV-builder and is deferred.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.batch_runner import (
    N_EFF_ASSETS_IC_OBS_MIN,
    PER_TICKER_ALPHA,
    STAGE0_AGGREGATE_IC_FLOOR,
    STAGE0_BINOMIAL_P_MAX,
    STAGE0_MEAN_NEFF_MIN,
    STAGE2_AGGREGATE_IC_MIN,
    AggregateIC,
    BinomialNullTest,
    _compute_binomial_null_test,
    _compute_n_eff_assets,
    _compute_n_eff_assets_with_method,
    _compute_stage_info,
    _compute_weighted_aggregate_ic,
    _summarize_validity,
)
from app.research.validation.ic import _select_hac_lag

# ─── Validity classification ─────────────────────────────────────


def _ticker_row(ticker: str, validity: str, **overrides) -> dict:
    base = {
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
        "validity": validity,
    }
    base.update(overrides)
    return base


def test_summarize_validity_counts_each_class_separately():
    rows = [
        _ticker_row("A", "valid"),
        _ticker_row("B", "valid"),
        _ticker_row("C", "invalid_iv"),
        _ticker_row("D", "invalid_data"),
        _ticker_row("E", "error"),
    ]

    summary = _summarize_validity(rows)

    assert summary.valid == 2
    assert summary.invalid_iv == 1
    assert summary.invalid_data == 1
    assert summary.errored == 1


# ─── Precision-weighted aggregate IC ────────────────────────────


def test_weighted_aggregate_ic_weights_by_n_eff():
    """Tickers with higher N_eff should dominate the precision-weighted mean."""
    rows = [
        _ticker_row("A", "valid", mean_ic=0.05, effective_n=200.0),
        _ticker_row("B", "valid", mean_ic=-0.05, effective_n=10.0),  # tiny weight
    ]

    result = _compute_weighted_aggregate_ic(rows)

    assert result.valid is True
    # Unweighted mean would be 0.0; weighted should lean strongly toward A's +0.05.
    assert result.point > 0.04
    assert result.n_tickers_used == 2
    assert result.sum_weights == pytest.approx(210.0)
    assert result.ci_lower < result.point < result.ci_upper


def test_weighted_aggregate_ic_invalid_when_no_positive_weights():
    rows = [_ticker_row("A", "valid", mean_ic=0.05, effective_n=0.0)]

    result = _compute_weighted_aggregate_ic(rows)

    assert result.valid is False


def test_weighted_aggregate_ic_ci_brackets_point_estimate():
    rows = [
        _ticker_row("A", "valid", mean_ic=0.04, effective_n=100.0),
        _ticker_row("B", "valid", mean_ic=0.06, effective_n=100.0),
    ]

    result = _compute_weighted_aggregate_ic(rows)

    assert result.ci_lower < result.point < result.ci_upper
    # SE = 1 / sqrt(200) ≈ 0.0707; 95 % half-width ≈ 1.96 * 0.0707 ≈ 0.139
    half_width = (result.ci_upper - result.ci_lower) / 2
    assert half_width == pytest.approx(0.1386, abs=0.01)


# ─── N_eff_assets via eigenvalue method ─────────────────────────


def test_n_eff_assets_orthogonal_returns_equals_raw_count():
    rng = np.random.default_rng(seed=42)
    dates = [f"2025-{m:02d}-{d:02d}" for m in (1, 2, 3) for d in range(1, 21)]
    returns = {
        "A": pd.Series(rng.normal(size=len(dates)), index=dates),
        "B": pd.Series(rng.normal(size=len(dates)), index=dates),
        "C": pd.Series(rng.normal(size=len(dates)), index=dates),
    }

    n_eff = _compute_n_eff_assets(returns)

    # Orthogonal (random) returns → N_eff close to the raw count of 3.
    assert n_eff == pytest.approx(3.0, abs=0.5)


def test_n_eff_assets_perfectly_correlated_returns_collapses_to_one():
    """Three identical series should compress to N_eff = 1."""
    dates = [f"2025-01-{d:02d}" for d in range(1, 31)]
    rng = np.random.default_rng(seed=7)
    base = rng.normal(size=len(dates))
    returns = {
        "A": pd.Series(base, index=dates),
        "B": pd.Series(base, index=dates),
        "C": pd.Series(base, index=dates),
    }

    n_eff = _compute_n_eff_assets(returns)

    assert n_eff == pytest.approx(1.0, abs=0.05)


def test_n_eff_assets_with_single_ticker_returns_one():
    dates = [f"2025-01-{d:02d}" for d in range(1, 11)]
    returns = {"A": pd.Series(np.linspace(-0.01, 0.01, 10), index=dates)}

    assert _compute_n_eff_assets(returns) == 1.0


def test_n_eff_assets_handles_misaligned_dates():
    """Inner-join alignment should not crash when tickers have disjoint dates."""
    rng = np.random.default_rng(seed=11)
    returns = {
        "A": pd.Series(rng.normal(size=20), index=[f"2025-01-{d:02d}" for d in range(1, 21)]),
        "B": pd.Series(rng.normal(size=20), index=[f"2025-02-{d:02d}" for d in range(1, 21)]),
    }

    n_eff = _compute_n_eff_assets(returns)

    # Inner-join leaves 0 overlapping dates, so we fall back to raw count.
    assert n_eff == 2.0


# ─── Binomial null test ─────────────────────────────────────────


def test_binomial_null_test_p_value_decreases_with_more_passes():
    """At fixed N, more passes → lower p-value (more evidence vs the null)."""
    rows_few = [_ticker_row(f"T{i}", "valid", passed_validation=(i < 1)) for i in range(10)]
    rows_many = [_ticker_row(f"T{i}", "valid", passed_validation=(i < 5)) for i in range(10)]

    p_few = _compute_binomial_null_test(rows_few, n_eff_assets=10.0).p_value
    p_many = _compute_binomial_null_test(rows_many, n_eff_assets=10.0).p_value

    assert p_many < p_few


def test_binomial_null_test_significant_when_pass_rate_far_above_alpha():
    """6 passes out of 10 valid tickers, with α=0.05 → p ≪ 0.05."""
    rows = [_ticker_row(f"T{i}", "valid", passed_validation=(i < 6)) for i in range(10)]

    result = _compute_binomial_null_test(rows, n_eff_assets=10.0)

    assert result.n_passed == 6
    assert result.n_valid == 10
    assert result.p_value < 0.001
    assert result.significant is True


def test_binomial_null_test_uses_n_eff_assets_not_raw_count():
    """Treating 3 highly correlated tickers as 3 independent observations
    inflates significance. Use N_eff_assets ≈ 1 → p must be much higher."""
    rows = [_ticker_row(f"T{i}", "valid", passed_validation=True) for i in range(3)]

    naive = _compute_binomial_null_test(rows, n_eff_assets=3.0)
    corrected = _compute_binomial_null_test(rows, n_eff_assets=1.0)

    # All passes — but corrected (n_eff_assets=1) treats it as 1 trial.
    assert corrected.p_value > naive.p_value


def test_binomial_null_test_empty_returns_default():
    result = _compute_binomial_null_test([], n_eff_assets=0.0)

    assert result.p_value == 1.0
    assert result.significant is False


# ─── Graduation stage ────────────────────────────────────────────


def _stage_inputs(
    n_valid: int,
    mean_neff: float,
    aggregate_ic: float,
    ci_excludes_zero: bool,
    binomial_p: float,
    n_eff_assets: float,
):
    rows = [
        _ticker_row(f"T{i}", "valid", mean_ic=aggregate_ic, effective_n=mean_neff, passed_validation=True)
        for i in range(n_valid)
    ]
    half_width = max(abs(aggregate_ic) - 0.001, 0.001) if ci_excludes_zero else abs(aggregate_ic) + 0.5
    aggregate = AggregateIC(
        point=aggregate_ic,
        se=half_width / 1.96,
        ci_lower=aggregate_ic - half_width,
        ci_upper=aggregate_ic + half_width,
        confidence_level=0.95,
        weighting_method="precision",
        n_tickers_used=n_valid,
        sum_weights=mean_neff * n_valid,
        valid=True,
    )
    binomial = BinomialNullTest(
        n_valid=n_valid,
        n_eff_assets=n_eff_assets,
        n_passed=n_valid,
        alpha_per_ticker=PER_TICKER_ALPHA,
        p_value=binomial_p,
        significant=binomial_p < 0.05,
    )
    return rows, aggregate, binomial, n_eff_assets


def test_stage0_when_too_few_valid_tickers():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=1,
        mean_neff=100.0,
        aggregate_ic=0.05,
        ci_excludes_zero=True,
        binomial_p=0.01,
        n_eff_assets=1.0,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 0
    assert any(c.name == "Valid tickers" for c in info.failed_criteria)


def test_stage0_when_mean_neff_too_low():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=5,
        mean_neff=STAGE0_MEAN_NEFF_MIN - 1,
        aggregate_ic=0.05,
        ci_excludes_zero=True,
        binomial_p=0.01,
        n_eff_assets=4.0,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 0
    assert any("N_eff" in c.name for c in info.failed_criteria)


def test_stage0_when_binomial_p_too_high():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=5,
        mean_neff=100.0,
        aggregate_ic=0.05,
        ci_excludes_zero=True,
        binomial_p=STAGE0_BINOMIAL_P_MAX + 0.01,
        n_eff_assets=4.0,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 0
    assert any("Binomial" in c.name for c in info.failed_criteria)


def test_stage0_when_ci_includes_zero_and_ic_below_floor():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=5,
        mean_neff=100.0,
        aggregate_ic=STAGE0_AGGREGATE_IC_FLOOR - 0.005,
        ci_excludes_zero=False,
        binomial_p=0.10,
        n_eff_assets=4.0,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 0
    assert any("Aggregate IC magnitude" in c.name for c in info.failed_criteria)


def test_stage1_when_survives_stage0_but_below_stage2():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=3,
        mean_neff=100.0,
        aggregate_ic=STAGE2_AGGREGATE_IC_MIN - 0.005,
        ci_excludes_zero=True,
        binomial_p=0.10,
        n_eff_assets=2.5,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 1
    assert info.label == "Weak Candidate"
    assert info.next_stage_label == "Research Candidate"
    assert len(info.advance_criteria) > 0


def test_stage2_when_meets_research_candidate_thresholds():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=6,
        mean_neff=200.0,
        aggregate_ic=0.04,
        ci_excludes_zero=True,
        binomial_p=0.02,
        n_eff_assets=3.5,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 2
    assert info.label == "Research Candidate"
    assert info.next_stage_label == "Promotion Candidate"


def test_stage3_when_meets_promotion_thresholds():
    rows, agg, bn, neff_assets = _stage_inputs(
        n_valid=12,
        mean_neff=400.0,
        aggregate_ic=0.07,
        ci_excludes_zero=True,
        binomial_p=0.001,
        n_eff_assets=6.0,
    )

    info = _compute_stage_info(rows, agg, bn, neff_assets)

    assert info.stage == 3
    assert info.label == "Promotion Candidate"


# ─── HAC lag selection (after ChatGPT v2 review) ─────────────


def test_select_hac_lag_drops_min_lag_floor_at_small_n():
    """A ``min_lag = 5`` floor doesn't survive a 6-IC-observation window.

    Before the v2 fix, the floor dominated and N_eff collapsed to ~2.
    The new selection caps the lag at ``min(round(1.5·√n), n // 3)`` and
    forces ``min_lag = 1`` when ``n < 50``.
    """
    # n = 6 → sqrt-cap ≈ round(1.5·2.449) = 4 ; n//3 = 2 ; upper = 2
    # min_lag floor is dropped (n < 50), so the chosen lag must be ≤ 2.
    chosen = _select_hac_lag(n=6, min_lag=5)
    assert chosen <= 2
    assert chosen >= 1


def test_select_hac_lag_respects_min_lag_when_n_large_enough():
    """At n ≥ 50 we honour the caller's structural min_lag."""
    # n = 200 → sqrt-cap ≈ round(1.5·14.14) = 21 ; n//3 = 66 ; upper = 21
    chosen = _select_hac_lag(n=200, min_lag=5)
    assert chosen >= 5
    assert chosen <= 21


def test_select_hac_lag_clamps_to_n_minus_2():
    """Even when the cap is permissive, lag can't exceed n - 2."""
    chosen = _select_hac_lag(n=4, min_lag=10)
    assert chosen <= 4 - 2


# ─── N_eff_assets method selection ──────────────────────────


def test_n_eff_assets_with_method_prefers_ic_when_all_tickers_have_enough_obs():
    """IC correlation is the Stage-2-correct dependency structure; we
    use it whenever every ticker has ≥ N_EFF_ASSETS_IC_OBS_MIN obs."""
    rng = np.random.default_rng(seed=17)
    dates_long = [f"2025-01-{d:02d}" for d in range(1, 21)]
    ic_series = {
        t: pd.Series(
            rng.normal(size=N_EFF_ASSETS_IC_OBS_MIN + 5),
            index=dates_long[: N_EFF_ASSETS_IC_OBS_MIN + 5],
        )
        for t in ("A", "B", "C")
    }
    returns = {
        t: pd.Series(rng.normal(size=20), index=dates_long) for t in ("A", "B", "C")
    }

    n_eff, method = _compute_n_eff_assets_with_method(ic_series, returns)

    assert method == "ic"
    assert 1.0 <= n_eff <= 3.0


def test_n_eff_assets_with_method_falls_back_to_returns_when_ic_too_short():
    """Below the IC-obs threshold per ticker, fall back to the
    daily-returns correlation (Stage 1 acceptable)."""
    rng = np.random.default_rng(seed=18)
    dates = [f"2025-01-{d:02d}" for d in range(1, 21)]
    ic_series = {
        t: pd.Series(rng.normal(size=2), index=dates[:2]) for t in ("A", "B", "C")
    }
    returns = {
        t: pd.Series(rng.normal(size=20), index=dates) for t in ("A", "B", "C")
    }

    n_eff, method = _compute_n_eff_assets_with_method(ic_series, returns)

    assert method == "returns"
    assert 1.0 <= n_eff <= 3.0


def test_n_eff_assets_with_method_falls_back_when_ic_series_empty():
    """No IC series at all → returns fallback."""
    returns = {
        t: pd.Series(np.linspace(-0.01, 0.01, 20)) for t in ("A", "B")
    }

    _, method = _compute_n_eff_assets_with_method({}, returns)

    assert method == "returns"


# ─── AggregateIC SE-approximation note ──────────────────────


def test_weighted_aggregate_ic_carries_se_approximation_disclaimer():
    """The CI tooltip must cite the SE approximation so a reader knows
    what's pinned vs approximated."""
    rows = [
        {"validity": "valid", "mean_ic": 0.04, "effective_n": 100.0, "passed_validation": True},
        {"validity": "valid", "mean_ic": 0.06, "effective_n": 100.0, "passed_validation": True},
    ]

    result = _compute_weighted_aggregate_ic(rows)

    assert result.valid is True
    assert "approximation" in result.se_approximation_note.lower()
    assert "Lo (2002)" in result.se_approximation_note
