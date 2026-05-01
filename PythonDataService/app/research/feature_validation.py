"""Feature Runner validation screens & 0/1/2/3 graduation ladder.

The original ``passed_validation`` boolean was a four-condition
conjunction (``|IC| ≥ 0.03 AND p < 0.05 AND stationary AND
monotonic``). That is a *statistical-shape* gate, not a validation
gate — it ignores OOS retention, cost viability, and the family-wise
error rate when the user runs multiple features.

This module decomposes the verdict into four screens and rolls them
into a 0/1/2/3 graduation stage. Each screen is reported separately
so the UI can show "passed statistical screen, failed economic
screen", which is the right level of detail for someone deciding
whether to act on the feature.

Authority for the thresholds: the v1 external methodology review
(see chat log dated 2026-05-01) and the project's existing
graduation-ladder pattern in ``app/research/signal/graduation.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from scipy import stats as scipy_stats

from app.research.feature_spec import FeatureValidationSpec

# ─── Stage thresholds (programmable) ──────────────────────────────────────

# Stage 1 — preliminary statistical association.
STAGE1_MIN_ABS_IC = 0.03
STAGE1_MIN_EFFECTIVE_N = 60
STAGE1_MAX_NW_P = 0.05

# Stage 2 — robust research candidate.
STAGE2_MIN_ABS_IC = 0.03
STAGE2_MIN_EFFECTIVE_N = 100
STAGE2_MIN_TEST_DAYS = 40
STAGE2_MIN_ABS_TEST_IC = 0.015
STAGE2_MIN_OOS_RETENTION = 0.50
STAGE2_MAX_HOLM_P = 0.10
STAGE2_MIN_REGIMES = 4

# Stage 3 — paper-trading candidate.
STAGE3_MIN_ABS_IC = 0.05
STAGE3_MIN_EFFECTIVE_N = 180
STAGE3_MIN_TEST_DAYS = 60
STAGE3_MIN_ABS_TEST_IC = 0.02
STAGE3_MIN_OOS_RETENTION = 0.60
STAGE3_MAX_HOLM_P = 0.05

# Multiple-testing correction.
DEFAULT_NUM_FEATURE_FAMILY = 5
"""Holm-Bonferroni correction divisor used when the user hasn't
explicitly declared how many features they're searching across.
The five built-in features in the picker are the canonical default."""


# ─── Result dataclasses ───────────────────────────────────────────────────


@dataclass
class IcCi:
    """Lo (2002)-style confidence interval on the headline mean IC.

    The IC over an effective sample of size ``n_eff`` has approximate
    variance ``(1 - IC²)² / n_eff`` (Fisher z-transform style); for
    small ICs this collapses to ``1 / n_eff``. We use the simpler
    ``1 / n_eff`` form and document it as an approximation, in line
    with the cross-sectional aggregate IC SE.
    """

    point: float = 0.0
    se: float = 0.0
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_level: float = 0.95
    n_eff_used: float = 0.0
    valid: bool = False
    """False when N_eff is too small for the interval to be meaningful."""
    se_approximation_note: str = (
        "SE assumes IC variance ≈ 1/N_eff (Stage 1 approximation). "
        "Lo (2002)'s full (1 - IC²)² / N_eff form is a Stage 3 upgrade."
    )


@dataclass
class MultipleTestingWarning:
    """Holm-Bonferroni correction over the user's feature family.

    The page can't observe how many features the user has tried
    before this one, so we conservatively assume the full built-in
    family (default 5). The Holm-corrected p-value is the
    minimum-rank-adjusted equivalent of Bonferroni; for a
    single-feature page it reduces to ``min(1, raw_p · n_family)``.
    """

    raw_nw_p_value: float = 1.0
    holm_p_value: float = 1.0
    n_family: int = DEFAULT_NUM_FEATURE_FAMILY
    note: str = ""


@dataclass
class CostViability:
    """Cost-adjusted Q5-Q1 spread.

    A statistically significant IC can correspond to an economically
    untradeable spread — the v1 review's central point. This
    structure isolates the economic-viability question from the
    statistical question.
    """

    gross_spread_bps: float = 0.0
    """Q5-Q1 mean-return spread, expressed in bps (×10⁴)."""
    cost_assumption_one_way_bps: float = 1.0
    cost_erasure_one_way_bps: float = 0.0
    """One-way cost at which net spread crosses zero."""
    net_spread_bps_at_assumption: float = 0.0
    viable_at_assumption: bool = False
    note: str = ""


@dataclass
class ValidationScreen:
    """One of four binary screens that combine into the stage label.

    Stage 0 trips when *any* required screen fails; Stage 1+ requires
    all required screens to pass. Optional screens are reported but
    do not gate the stage.
    """

    name: str = ""
    description: str = ""
    passed: bool = False
    required_for_stage1: bool = True
    failure_reasons: list[str] = field(default_factory=list)


@dataclass
class FeatureStageCriterion:
    """One item in the next-stage advance list."""

    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


@dataclass
class FeatureStageInfo:
    """Where the feature sits on the 0/1/2/3 ladder."""

    stage: int = 0
    """0 = Rejected, 1 = Statistical association, 2 = Research
    candidate, 3 = Paper-trading candidate."""
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    advance_criteria: list[FeatureStageCriterion] = field(default_factory=list)
    failed_screens: list[str] = field(default_factory=list)
    """Names of the required screens that failed (empty when stage ≥ 1)."""


@dataclass
class FeatureValidationVerdict:
    """Top-level result that replaces the legacy ``passed_validation``."""

    statistical_screen: ValidationScreen = field(default_factory=ValidationScreen)
    economic_screen: ValidationScreen = field(default_factory=ValidationScreen)
    oos_screen: ValidationScreen = field(default_factory=ValidationScreen)
    multiple_testing_screen: ValidationScreen = field(default_factory=ValidationScreen)

    multiple_testing: MultipleTestingWarning = field(default_factory=MultipleTestingWarning)
    cost_viability: CostViability = field(default_factory=CostViability)
    ic_ci: IcCi = field(default_factory=IcCi)

    stage_info: FeatureStageInfo = field(default_factory=FeatureStageInfo)

    final_decision: str = "Do not trade."
    """Reader-facing one-liner the UI puts at the top: e.g.
    'Stage 1 — statistical association only. Not tradeable: cost
    erodes the estimated spread below 1 bp.'"""


# ─── Helpers ──────────────────────────────────────────────────────────────


def compute_ic_ci(
    mean_ic: float,
    n_eff: float,
    confidence_level: float = 0.95,
) -> IcCi:
    """Lo-style CI on the headline mean IC with N_eff substitution."""
    if n_eff <= 1 or not math.isfinite(mean_ic):
        return IcCi(point=mean_ic, valid=False, n_eff_used=float(n_eff))

    se = float(1.0 / math.sqrt(n_eff))
    tail = (1.0 - confidence_level) / 2.0
    z_critical = float(scipy_stats.norm.ppf(1.0 - tail))

    return IcCi(
        point=mean_ic,
        se=se,
        ci_lower=mean_ic - z_critical * se,
        ci_upper=mean_ic + z_critical * se,
        confidence_level=confidence_level,
        n_eff_used=float(n_eff),
        valid=True,
    )


def compute_holm_p(
    raw_p_value: float,
    n_family: int = DEFAULT_NUM_FEATURE_FAMILY,
) -> float:
    """Holm-Bonferroni-adjusted p-value for a single test in a family.

    For one observed p-value out of an n-feature family, the Holm
    rank-1 correction reduces to ``min(1, raw_p · n)``. We expose
    this as the conservative correction the UI surfaces alongside
    the raw NW p-value.
    """
    if raw_p_value < 0 or n_family <= 0:
        return 1.0
    return float(min(1.0, raw_p_value * n_family))


def compute_cost_viability(
    quantile_bins: list[dict],
    cost_assumption_one_way_bps: float = 1.0,
) -> CostViability:
    """Compute cost-adjusted Q5-Q1 spread.

    Assumes round-trip cost = 2 × one-way (i.e. enter long Q5, short Q1
    + exit). Slippage / market impact not modelled. The 1-bp default
    assumption is intentionally tight — cost erasure should be
    obvious in the UI when the headline IC's tradeable form is
    a fraction of a basis point.
    """
    if not quantile_bins:
        return CostViability(note="No quantile bins available.")

    sorted_bins = sorted(quantile_bins, key=lambda b: b.get("bin_number", 0))
    if len(sorted_bins) < 2:
        return CostViability(note="Need at least 2 quantile bins.")

    bottom = float(sorted_bins[0].get("mean_return", 0.0))
    top = float(sorted_bins[-1].get("mean_return", 0.0))
    gross_spread_bps = (top - bottom) * 10_000.0  # log-return → bps

    round_trip_cost_bps = 2.0 * cost_assumption_one_way_bps
    net_spread_bps = gross_spread_bps - round_trip_cost_bps

    # Cost-erasure threshold = gross_spread_bps / 2 (because the
    # round-trip cost is 2× the one-way cost).
    erasure_one_way = abs(gross_spread_bps) / 2.0 if gross_spread_bps != 0 else 0.0

    return CostViability(
        gross_spread_bps=gross_spread_bps,
        cost_assumption_one_way_bps=cost_assumption_one_way_bps,
        cost_erasure_one_way_bps=erasure_one_way,
        net_spread_bps_at_assumption=net_spread_bps,
        viable_at_assumption=net_spread_bps > 0,
    )


# ─── Screen evaluators ────────────────────────────────────────────────────


def _evaluate_statistical_screen(
    *,
    spec: FeatureValidationSpec,
    mean_ic: float,
    nw_p_value: float,
    effective_n: float,
    is_stationary: bool,
    is_monotonic: bool,
) -> ValidationScreen:
    """Statistical-shape screen: |IC|, p-value, optional stationarity/monotonicity."""
    reasons: list[str] = []
    if abs(mean_ic) < STAGE1_MIN_ABS_IC:
        reasons.append(
            f"|IC| = {abs(mean_ic):.4f} below the {STAGE1_MIN_ABS_IC:.2f} "
            "materiality threshold."
        )
    if nw_p_value >= STAGE1_MAX_NW_P:
        reasons.append(
            f"Newey-West p-value = {nw_p_value:.4f} above the "
            f"{STAGE1_MAX_NW_P:.2f} significance threshold."
        )
    if effective_n < STAGE1_MIN_EFFECTIVE_N:
        reasons.append(
            f"Effective N = {effective_n:.0f} below the "
            f"{STAGE1_MIN_EFFECTIVE_N} sample-size floor."
        )
    if spec.stationarity_required and not is_stationary:
        reasons.append(
            "Stationarity required by feature spec but ADF/KPSS rejected stationarity."
        )
    if spec.monotonicity_required and not is_monotonic:
        reasons.append(
            "Monotonicity required by feature spec but the quantile chart "
            "is not monotonic at the configured threshold."
        )

    return ValidationScreen(
        name="Statistical association",
        description=(
            "Daily IC is large enough, NW-significant, and the sample "
            "is deep enough. Stationarity/monotonicity gate only when "
            "the feature spec requires them."
        ),
        passed=not reasons,
        required_for_stage1=True,
        failure_reasons=reasons,
    )


def _evaluate_economic_screen(cost: CostViability) -> ValidationScreen:
    """Economic-viability screen: net Q5-Q1 spread > 0 at the assumed cost."""
    if cost.gross_spread_bps == 0:
        return ValidationScreen(
            name="Economic viability",
            description=(
                "Net Q5-Q1 spread (long extreme − short extreme) must "
                "exceed assumed round-trip cost."
            ),
            passed=False,
            required_for_stage1=False,
            failure_reasons=["No quantile spread to evaluate."],
        )

    if cost.viable_at_assumption:
        return ValidationScreen(
            name="Economic viability",
            description=(
                "Net Q5-Q1 spread (long extreme − short extreme) must "
                "exceed assumed round-trip cost."
            ),
            passed=True,
            required_for_stage1=False,
        )

    return ValidationScreen(
        name="Economic viability",
        description=(
            "Net Q5-Q1 spread (long extreme − short extreme) must "
            "exceed assumed round-trip cost."
        ),
        passed=False,
        required_for_stage1=False,
        failure_reasons=[
            f"Gross spread = {cost.gross_spread_bps:.2f} bps; "
            f"round-trip cost (2 × one-way) at {cost.cost_assumption_one_way_bps:.1f} "
            f"bps assumption = {2 * cost.cost_assumption_one_way_bps:.1f} bps. "
            f"Net = {cost.net_spread_bps_at_assumption:.2f} bps. Cost erases the "
            f"alpha at approximately {cost.cost_erasure_one_way_bps:.2f} bps one-way."
        ],
    )


def _evaluate_oos_screen(
    train_test_present: bool,
    test_days: int,
    test_mean_ic: float,
    oos_retention: float,
) -> ValidationScreen:
    """OOS screen: test-period evidence is consistent with train."""
    if not train_test_present:
        return ValidationScreen(
            name="Out-of-sample",
            description=(
                "Test-period IC must retain a meaningful fraction of "
                "train-period IC, with non-trivial test sample size."
            ),
            passed=False,
            required_for_stage1=False,
            failure_reasons=["Train/test split was not computed."],
        )

    reasons: list[str] = []
    if test_days < STAGE2_MIN_TEST_DAYS:
        reasons.append(
            f"Test window = {test_days} days; below the "
            f"{STAGE2_MIN_TEST_DAYS}-day floor for a defensible OOS read."
        )
    if abs(test_mean_ic) < STAGE2_MIN_ABS_TEST_IC:
        reasons.append(
            f"|Test IC| = {abs(test_mean_ic):.4f}; below the "
            f"{STAGE2_MIN_ABS_TEST_IC:.3f} OOS-IC floor."
        )
    if oos_retention < STAGE2_MIN_OOS_RETENTION:
        reasons.append(
            f"OOS retention = {oos_retention:.0%}; below the "
            f"{STAGE2_MIN_OOS_RETENTION:.0%} threshold (likely overfit)."
        )

    return ValidationScreen(
        name="Out-of-sample",
        description=(
            "Test-period IC must retain a meaningful fraction of "
            "train-period IC, with non-trivial test sample size."
        ),
        passed=not reasons,
        required_for_stage1=False,  # diagnostic at Stage 1; gating at Stage 2.
        failure_reasons=reasons,
    )


def _evaluate_multiple_testing_screen(
    nw_p_value: float,
    n_family: int = DEFAULT_NUM_FEATURE_FAMILY,
) -> tuple[ValidationScreen, MultipleTestingWarning]:
    """Holm-corrected p-value screen.

    The user can run any of N built-in features on the page; the
    Holm correction approximates the family-wise false-positive rate
    inflation. We report the corrected p alongside the raw p so the
    reader sees what changed.
    """
    holm_p = compute_holm_p(nw_p_value, n_family=n_family)
    warning = MultipleTestingWarning(
        raw_nw_p_value=nw_p_value,
        holm_p_value=holm_p,
        n_family=n_family,
        note=(
            f"Per-feature p = {nw_p_value:.4f}. Across the {n_family}-feature "
            f"family, the Holm-Bonferroni-corrected p ≈ {holm_p:.4f}. "
            "Significance is overstated if the user tested multiple "
            "features and selected the best result."
        ),
    )

    passed = holm_p < STAGE1_MAX_NW_P
    reasons: list[str] = (
        [] if passed else [
            f"Holm-corrected p = {holm_p:.4f} above {STAGE1_MAX_NW_P:.2f}."
        ]
    )

    screen = ValidationScreen(
        name="Multiple-testing correction",
        description=(
            "Holm-Bonferroni-corrected p-value across the "
            f"{n_family}-feature family must remain significant."
        ),
        passed=passed,
        required_for_stage1=False,  # diagnostic at Stage 1; gating at Stage 2+.
        failure_reasons=reasons,
    )
    return screen, warning


# ─── Stage assignment ─────────────────────────────────────────────────────


def _compute_stage_info(
    *,
    statistical_screen: ValidationScreen,
    economic_screen: ValidationScreen,
    oos_screen: ValidationScreen,
    multiple_testing_screen: ValidationScreen,
    mean_ic: float,
    effective_n: float,
    test_days: int,
    test_mean_ic: float,
    oos_retention: float,
    holm_p_value: float,
    cost: CostViability,
    regimes_observed: int,
) -> FeatureStageInfo:
    """Pick the stage from screen results + numerical thresholds."""
    # Stage 0 — any *required-for-stage1* screen fails.
    failed_required = [
        s for s in (
            statistical_screen,
            economic_screen,
            oos_screen,
            multiple_testing_screen,
        )
        if s.required_for_stage1 and not s.passed
    ]
    if failed_required:
        return FeatureStageInfo(
            stage=0,
            label="Rejected",
            description=(
                "Failed at least one required Stage 1 screen. The headline "
                "result does not support a predictive claim — try a "
                "different feature, longer date range, or different target."
            ),
            failed_screens=[s.name for s in failed_required],
        )

    abs_ic = abs(mean_ic)
    abs_test_ic = abs(test_mean_ic)

    # Stage 3 — paper-trading candidate.
    stage3_ok = (
        abs_ic >= STAGE3_MIN_ABS_IC
        and effective_n >= STAGE3_MIN_EFFECTIVE_N
        and test_days >= STAGE3_MIN_TEST_DAYS
        and abs_test_ic >= STAGE3_MIN_ABS_TEST_IC
        and oos_retention >= STAGE3_MIN_OOS_RETENTION
        and holm_p_value <= STAGE3_MAX_HOLM_P
        and cost.viable_at_assumption
    )
    if stage3_ok:
        return FeatureStageInfo(
            stage=3,
            label="Paper-trading candidate",
            description=(
                "Strong in-sample evidence, OOS retention holds, "
                "multiple-testing-corrected p stays significant, and "
                "the spread survives the assumed cost. Suitable for "
                "paper-trading — not live-trading validated."
            ),
        )

    # Stage 2 — research candidate.
    stage2_ok = (
        abs_ic >= STAGE2_MIN_ABS_IC
        and effective_n >= STAGE2_MIN_EFFECTIVE_N
        and test_days >= STAGE2_MIN_TEST_DAYS
        and abs_test_ic >= STAGE2_MIN_ABS_TEST_IC
        and oos_retention >= STAGE2_MIN_OOS_RETENTION
        and holm_p_value <= STAGE2_MAX_HOLM_P
        and cost.viable_at_assumption
        and regimes_observed >= STAGE2_MIN_REGIMES
    )
    if stage2_ok:
        return FeatureStageInfo(
            stage=2,
            label="Research candidate",
            description=(
                "Survives OOS, multiple-testing correction, and the "
                "economic-viability screen. Defensible mid-stage research."
            ),
            next_stage_label="Paper-trading candidate",
            advance_criteria=[
                FeatureStageCriterion(
                    name="|Mean IC|",
                    description="Headline IC magnitude",
                    current_value=abs_ic,
                    required_repr=f"≥ {STAGE3_MIN_ABS_IC:.2f}",
                    met=abs_ic >= STAGE3_MIN_ABS_IC,
                ),
                FeatureStageCriterion(
                    name="Effective N",
                    description="Autocorrelation-adjusted IC days",
                    current_value=effective_n,
                    required_repr=f"≥ {STAGE3_MIN_EFFECTIVE_N}",
                    met=effective_n >= STAGE3_MIN_EFFECTIVE_N,
                ),
                FeatureStageCriterion(
                    name="OOS retention",
                    description="|test IC| / |train IC|",
                    current_value=oos_retention,
                    required_repr=f"≥ {STAGE3_MIN_OOS_RETENTION:.0%}",
                    met=oos_retention >= STAGE3_MIN_OOS_RETENTION,
                ),
                FeatureStageCriterion(
                    name="Holm p-value",
                    description="Family-wise corrected p",
                    current_value=holm_p_value,
                    required_repr=f"≤ {STAGE3_MAX_HOLM_P:.2f}",
                    met=holm_p_value <= STAGE3_MAX_HOLM_P,
                ),
            ],
        )

    # Stage 1 — preliminary statistical association.
    return FeatureStageInfo(
        stage=1,
        label="Statistical association",
        description=(
            "In-sample statistical association detected. Not yet a "
            "research candidate — OOS evidence, multiple-testing "
            "correction, or economic viability is missing or weak."
        ),
        next_stage_label="Research candidate",
        advance_criteria=[
            FeatureStageCriterion(
                name="Test-period IC",
                description="|Mean IC| on the OOS window",
                current_value=abs_test_ic,
                required_repr=f"≥ {STAGE2_MIN_ABS_TEST_IC:.3f}",
                met=abs_test_ic >= STAGE2_MIN_ABS_TEST_IC,
            ),
            FeatureStageCriterion(
                name="OOS retention",
                description="|test IC| / |train IC|",
                current_value=oos_retention,
                required_repr=f"≥ {STAGE2_MIN_OOS_RETENTION:.0%}",
                met=oos_retention >= STAGE2_MIN_OOS_RETENTION,
            ),
            FeatureStageCriterion(
                name="Test-window days",
                description="Length of the OOS window",
                current_value=float(test_days),
                required_repr=f"≥ {STAGE2_MIN_TEST_DAYS}",
                met=test_days >= STAGE2_MIN_TEST_DAYS,
            ),
            FeatureStageCriterion(
                name="Holm-corrected p",
                description=f"Family-wise correction over {DEFAULT_NUM_FEATURE_FAMILY} features",
                current_value=holm_p_value,
                required_repr=f"≤ {STAGE2_MAX_HOLM_P:.2f}",
                met=holm_p_value <= STAGE2_MAX_HOLM_P,
            ),
            FeatureStageCriterion(
                name="Net Q5-Q1 spread (after cost)",
                description="Cost-adjusted spread > 0 at assumed one-way cost",
                current_value=cost.net_spread_bps_at_assumption,
                required_repr="> 0 bps",
                met=cost.viable_at_assumption,
            ),
            FeatureStageCriterion(
                name="Regimes observed",
                description="Distinct vol × trend regimes with data",
                current_value=float(regimes_observed),
                required_repr=f"≥ {STAGE2_MIN_REGIMES}",
                met=regimes_observed >= STAGE2_MIN_REGIMES,
            ),
        ],
    )


# ─── Top-level entry ──────────────────────────────────────────────────────


def evaluate_feature_validation(
    *,
    spec: FeatureValidationSpec,
    mean_ic: float,
    nw_p_value: float,
    effective_n: float,
    is_stationary: bool,
    is_monotonic: bool,
    quantile_bins: list[dict],
    train_test_present: bool,
    test_days: int,
    test_mean_ic: float,
    oos_retention: float,
    regimes_observed: int,
    cost_assumption_one_way_bps: float = 1.0,
    n_family: int = DEFAULT_NUM_FEATURE_FAMILY,
) -> FeatureValidationVerdict:
    """Combine screens and stage assignment into a single verdict."""
    statistical = _evaluate_statistical_screen(
        spec=spec,
        mean_ic=mean_ic,
        nw_p_value=nw_p_value,
        effective_n=effective_n,
        is_stationary=is_stationary,
        is_monotonic=is_monotonic,
    )
    cost = compute_cost_viability(
        quantile_bins=quantile_bins,
        cost_assumption_one_way_bps=cost_assumption_one_way_bps,
    )
    economic = _evaluate_economic_screen(cost)
    oos = _evaluate_oos_screen(
        train_test_present=train_test_present,
        test_days=test_days,
        test_mean_ic=test_mean_ic,
        oos_retention=oos_retention,
    )
    multiple_testing_screen, multiple_testing = _evaluate_multiple_testing_screen(
        nw_p_value=nw_p_value,
        n_family=n_family,
    )
    ic_ci = compute_ic_ci(mean_ic=mean_ic, n_eff=effective_n)

    stage_info = _compute_stage_info(
        statistical_screen=statistical,
        economic_screen=economic,
        oos_screen=oos,
        multiple_testing_screen=multiple_testing_screen,
        mean_ic=mean_ic,
        effective_n=effective_n,
        test_days=test_days,
        test_mean_ic=test_mean_ic,
        oos_retention=oos_retention,
        holm_p_value=multiple_testing.holm_p_value,
        cost=cost,
        regimes_observed=regimes_observed,
    )

    return FeatureValidationVerdict(
        statistical_screen=statistical,
        economic_screen=economic,
        oos_screen=oos,
        multiple_testing_screen=multiple_testing_screen,
        multiple_testing=multiple_testing,
        cost_viability=cost,
        ic_ci=ic_ci,
        stage_info=stage_info,
        final_decision=_render_final_decision(stage_info, cost, oos),
    )


def _render_final_decision(
    stage_info: FeatureStageInfo,
    cost: CostViability,
    oos: ValidationScreen,
) -> str:
    """One-sentence reader-facing verdict the UI puts at the top."""
    if stage_info.stage == 0:
        screens = ", ".join(stage_info.failed_screens) or "Stage 1 screens"
        return f"Do not trade. Rejected at Stage 0 ({screens})."
    if stage_info.stage == 1:
        parts = ["Stage 1 — statistical association only. Not tradeable as shown."]
        if not cost.viable_at_assumption and cost.gross_spread_bps != 0:
            parts.append(
                f"Cost erases the spread at ≈ {cost.cost_erasure_one_way_bps:.2f} bps one-way."
            )
        if not oos.passed and oos.failure_reasons:
            parts.append("OOS evidence weak.")
        return " ".join(parts)
    if stage_info.stage == 2:
        return "Stage 2 — research candidate. Defensible but not paper-trading-ready."
    return "Stage 3 — paper-trading candidate. Not live-trading validated."
