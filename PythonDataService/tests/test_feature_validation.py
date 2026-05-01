"""Tests for the Feature Runner validation screens & 0/1/2/3 ladder.

Pin the structural invariants (which screen is required-for-stage1,
how Holm scales with the family, how cost erasure is computed, how the
stage assignment combines the four screens) without leaning on a
golden fixture — the math here is closed-form and the tolerances are
trivial.
"""

from __future__ import annotations

import math

import pytest

from app.research.feature_spec import FeatureValidationSpec, get_spec
from app.research.feature_validation import (
    DEFAULT_NUM_FEATURE_FAMILY,
    STAGE1_MAX_NW_P,
    STAGE1_MIN_ABS_IC,
    STAGE1_MIN_EFFECTIVE_N,
    STAGE2_MIN_ABS_TEST_IC,
    STAGE2_MIN_OOS_RETENTION,
    STAGE2_MIN_REGIMES,
    STAGE2_MIN_TEST_DAYS,
    STAGE3_MIN_ABS_IC,
    STAGE3_MIN_ABS_TEST_IC,
    STAGE3_MIN_EFFECTIVE_N,
    STAGE3_MIN_OOS_RETENTION,
    STAGE3_MIN_TEST_DAYS,
    compute_cost_viability,
    compute_holm_p,
    compute_ic_ci,
    evaluate_feature_validation,
)

# ─── feature_spec ─────────────────────────────────────────────────────────


def test_get_spec_returns_builtin_when_known():
    spec = get_spec("rsi_14")

    assert spec.feature_name == "rsi_14"
    assert spec.expected_direction == "negative"
    assert spec.stationarity_required is True
    assert spec.monotonicity_required is True


def test_get_spec_returns_generic_fallback_for_unknown_feature():
    spec = get_spec("not_a_real_feature_xyz")

    assert spec.feature_name == "not_a_real_feature_xyz"
    assert spec.expected_direction == "unknown"
    assert spec.stationarity_required is False
    assert spec.monotonicity_required is False
    assert any("No validation contract" in n for n in spec.notes)


# ─── compute_holm_p ───────────────────────────────────────────────────────


def test_holm_correction_scales_with_family_size():
    raw = 0.04
    holm_5 = compute_holm_p(raw, n_family=5)
    holm_10 = compute_holm_p(raw, n_family=10)

    assert holm_5 == pytest.approx(0.20, abs=1e-12)
    assert holm_10 == pytest.approx(0.40, abs=1e-12)


def test_holm_correction_caps_at_one():
    holm = compute_holm_p(0.5, n_family=10)

    assert holm == 1.0


def test_holm_correction_handles_invalid_inputs():
    assert compute_holm_p(-0.01, n_family=5) == 1.0
    assert compute_holm_p(0.04, n_family=0) == 1.0


# ─── compute_ic_ci ────────────────────────────────────────────────────────


def test_ic_ci_is_symmetric_and_brackets_point():
    ci = compute_ic_ci(mean_ic=0.05, n_eff=200)

    assert ci.valid is True
    assert ci.ci_lower < 0.05 < ci.ci_upper
    upper_half = ci.ci_upper - ci.point
    lower_half = ci.point - ci.ci_lower
    assert upper_half == pytest.approx(lower_half, rel=1e-9)


def test_ic_ci_widens_when_n_eff_shrinks():
    ci_large = compute_ic_ci(mean_ic=0.05, n_eff=400)
    ci_small = compute_ic_ci(mean_ic=0.05, n_eff=100)

    assert (ci_small.ci_upper - ci_small.ci_lower) > (
        ci_large.ci_upper - ci_large.ci_lower
    )


def test_ic_ci_invalid_when_n_eff_too_small():
    ci = compute_ic_ci(mean_ic=0.05, n_eff=1)

    assert ci.valid is False


def test_ic_ci_invalid_when_mean_ic_is_nan():
    ci = compute_ic_ci(mean_ic=float("nan"), n_eff=200)

    assert ci.valid is False


# ─── compute_cost_viability ───────────────────────────────────────────────


def test_cost_viability_erasure_threshold_is_half_gross_spread():
    """Round-trip cost = 2× one-way, so erasure happens at gross/2 one-way."""
    bins = [
        {"bin_number": 1, "mean_return": 0.0},
        {"bin_number": 2, "mean_return": 0.0},
        {"bin_number": 3, "mean_return": 0.0},
        {"bin_number": 4, "mean_return": 0.0},
        # Q5 mean − Q1 mean = 0.0010 → 10 bps gross, erasure at 5 bps one-way.
        {"bin_number": 5, "mean_return": 0.0010},
    ]

    cost = compute_cost_viability(bins, cost_assumption_one_way_bps=1.0)

    assert cost.gross_spread_bps == pytest.approx(10.0, abs=1e-9)
    assert cost.cost_erasure_one_way_bps == pytest.approx(5.0, abs=1e-9)
    # net = gross - 2 × 1 = 8 bps; viable.
    assert cost.net_spread_bps_at_assumption == pytest.approx(8.0, abs=1e-9)
    assert cost.viable_at_assumption is True


def test_cost_viability_flips_to_not_viable_when_cost_high():
    bins = [
        {"bin_number": 1, "mean_return": 0.0},
        {"bin_number": 5, "mean_return": 0.0001},  # 1 bp gross spread
    ]

    cost = compute_cost_viability(bins, cost_assumption_one_way_bps=1.0)

    # gross = 1 bp, round-trip = 2 bps → net = -1 bp.
    assert cost.viable_at_assumption is False
    assert cost.net_spread_bps_at_assumption < 0


def test_cost_viability_handles_empty_bins():
    cost = compute_cost_viability([], cost_assumption_one_way_bps=1.0)

    assert cost.gross_spread_bps == 0.0
    assert cost.viable_at_assumption is False
    assert "No quantile" in cost.note


# ─── evaluate_feature_validation — stage assignment ──────────────────────


def _strong_quantile_bins() -> list[dict]:
    """Q5-Q1 = 20 bps → cost-viable at 1 bp one-way."""
    return [
        {"bin_number": 1, "mean_return": -0.0010},
        {"bin_number": 2, "mean_return": -0.0005},
        {"bin_number": 3, "mean_return": 0.0},
        {"bin_number": 4, "mean_return": 0.0005},
        {"bin_number": 5, "mean_return": 0.0010},
    ]


def _strong_args(spec: FeatureValidationSpec) -> dict:
    """Inputs that trip Stage 3 — used as the baseline for "make me fail" tests."""
    return dict(
        spec=spec,
        mean_ic=0.06,
        nw_p_value=0.001,  # Holm-corrected ≈ 0.005 ≪ 0.05
        effective_n=400,
        is_stationary=True,
        is_monotonic=True,
        quantile_bins=_strong_quantile_bins(),
        train_test_present=True,
        test_days=120,
        test_mean_ic=0.045,
        oos_retention=0.75,
        regimes_observed=6,
    )


def test_evaluate_returns_stage_3_when_all_screens_pass_strongly():
    spec = get_spec("momentum_5m")

    verdict = evaluate_feature_validation(**_strong_args(spec))

    assert verdict.stage_info.stage == 3
    assert verdict.stage_info.label == "Paper-trading candidate"
    assert verdict.statistical_screen.passed is True
    assert verdict.economic_screen.passed is True
    assert verdict.oos_screen.passed is True
    assert verdict.multiple_testing_screen.passed is True


def test_evaluate_returns_stage_0_when_statistical_screen_fails_required_check():
    """|IC| below Stage 1 floor must trip Stage 0 regardless of OOS / cost."""
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["mean_ic"] = 0.005  # below STAGE1_MIN_ABS_IC = 0.03

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 0
    assert verdict.statistical_screen.passed is False
    assert "Statistical association" in verdict.stage_info.failed_screens


def test_evaluate_returns_stage_1_when_oos_screen_fails():
    """OOS retention is diagnostic at Stage 1, gating at Stage 2+."""
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["oos_retention"] = 0.20  # below STAGE2_MIN_OOS_RETENTION
    args["test_mean_ic"] = 0.005  # also below STAGE2_MIN_ABS_TEST_IC

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 1
    assert verdict.statistical_screen.passed is True
    assert verdict.oos_screen.passed is False
    # OOS is not required-for-stage1, so it doesn't pull us down to Stage 0.


def test_evaluate_returns_stage_1_when_cost_erases_alpha():
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["quantile_bins"] = [
        {"bin_number": 1, "mean_return": 0.0},
        {"bin_number": 5, "mean_return": 0.00005},  # 0.5 bps gross — cost erodes
    ]

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 1
    assert verdict.economic_screen.passed is False
    assert verdict.statistical_screen.passed is True


def test_evaluate_stage_1_finalises_decision_with_cost_erasure_message():
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["quantile_bins"] = [
        {"bin_number": 1, "mean_return": 0.0},
        {"bin_number": 5, "mean_return": 0.00005},
    ]

    verdict = evaluate_feature_validation(**args)

    assert "Stage 1" in verdict.final_decision
    assert "Cost erases" in verdict.final_decision


def test_evaluate_stage_2_when_multiple_testing_relaxed_but_stage3_thresholds_unmet():
    """Stage 2 fires when Holm-p ≤ 0.10 (Stage 2 threshold) but a Stage 3
    threshold (effective_n / test_days / abs_ic) is unmet."""
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["effective_n"] = 120  # below STAGE3_MIN_EFFECTIVE_N = 180

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 2
    assert verdict.stage_info.next_stage_label == "Paper-trading candidate"


def test_evaluate_stationarity_required_kills_when_not_stationary():
    """RSI's spec requires stationarity → non-stationary trips Stage 0."""
    spec = get_spec("rsi_14")
    args = _strong_args(spec)
    args["is_stationary"] = False

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 0
    assert any(
        "Stationarity required" in r for r in verdict.statistical_screen.failure_reasons
    )


def test_evaluate_stationarity_not_required_for_macd_passes_when_non_stationary():
    """MACD spec sets ``stationarity_required=False`` (it's a price diff)."""
    spec = get_spec("macd_signal")
    args = _strong_args(spec)
    args["is_stationary"] = False

    verdict = evaluate_feature_validation(**args)

    # Should still be Stage 1+ because stationarity isn't required for MACD.
    assert verdict.stage_info.stage >= 1


def test_evaluate_holm_correction_visible_in_warning():
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["nw_p_value"] = 0.04

    verdict = evaluate_feature_validation(**args)

    assert verdict.multiple_testing.raw_nw_p_value == pytest.approx(0.04, abs=1e-12)
    expected_holm = min(1.0, 0.04 * DEFAULT_NUM_FEATURE_FAMILY)
    assert verdict.multiple_testing.holm_p_value == pytest.approx(
        expected_holm, abs=1e-12
    )


def test_evaluate_returns_ic_ci_consistent_with_helper():
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    expected_ci = compute_ic_ci(mean_ic=args["mean_ic"], n_eff=args["effective_n"])

    verdict = evaluate_feature_validation(**args)

    assert verdict.ic_ci.valid == expected_ci.valid
    assert verdict.ic_ci.ci_lower == pytest.approx(expected_ci.ci_lower, rel=1e-9)
    assert verdict.ic_ci.ci_upper == pytest.approx(expected_ci.ci_upper, rel=1e-9)


def test_evaluate_includes_advance_criteria_for_non_terminal_stages():
    spec = get_spec("momentum_5m")
    # Stage 1 result — advance criteria should list the Stage 2 requirements.
    args = _strong_args(spec)
    args["oos_retention"] = 0.10
    args["test_mean_ic"] = 0.005

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 1
    assert len(verdict.stage_info.advance_criteria) > 0
    advance_names = {c.name for c in verdict.stage_info.advance_criteria}
    assert "OOS retention" in advance_names


def test_evaluate_stage_2_advance_criteria_reference_stage_3_thresholds():
    spec = get_spec("momentum_5m")
    args = _strong_args(spec)
    args["effective_n"] = 120  # forces stage 2

    verdict = evaluate_feature_validation(**args)

    assert verdict.stage_info.stage == 2
    n_eff_criterion = next(
        c for c in verdict.stage_info.advance_criteria if c.name == "Effective N"
    )
    assert math.isclose(n_eff_criterion.current_value, 120.0, abs_tol=1e-12)
    assert n_eff_criterion.met is False


# ─── Sanity: thresholds are strictly nested ──────────────────────────────


def test_stage_thresholds_are_strictly_nested():
    """Stage 3 thresholds must dominate Stage 2 thresholds."""
    assert STAGE3_MIN_ABS_IC >= STAGE1_MIN_ABS_IC
    assert STAGE3_MIN_EFFECTIVE_N >= STAGE1_MIN_EFFECTIVE_N
    assert STAGE3_MIN_TEST_DAYS >= STAGE2_MIN_TEST_DAYS
    assert STAGE3_MIN_ABS_TEST_IC >= STAGE2_MIN_ABS_TEST_IC
    assert STAGE3_MIN_OOS_RETENTION >= STAGE2_MIN_OOS_RETENTION
    assert STAGE2_MIN_REGIMES > 0
    assert STAGE1_MAX_NW_P > 0
