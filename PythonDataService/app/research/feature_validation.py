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
# v2 review: Stage 3 should NOT be gated mainly by a bigger headline IC.
# A flashy |IC| 0.08 with negative net-cost spread should not graduate
# over a stable |IC| 0.035 with positive net spread and strong OOS.
# We hold |IC| at the same 0.03 floor as Stage 2 and discriminate on
# OOS retention, cost viability, and direction-match instead.
STAGE3_MIN_ABS_IC = 0.03
STAGE3_MIN_EFFECTIVE_N = 180
STAGE3_MIN_TEST_DAYS = 60
STAGE3_MIN_ABS_TEST_IC = 0.02
STAGE3_MIN_OOS_RETENTION = 0.60
STAGE3_MAX_HOLM_P = 0.05

# Regime stability (Stage 2+ gating). Promoted from a hidden Stage 2
# sub-criterion to its own screen so an allocator can see at a glance
# that the signal works only in one market state.
REGIME_STABILITY_MIN_REGIMES_OBSERVED = 4
REGIME_STABILITY_MAX_SIGN_FLIP_FRACTION = 0.34
"""Maximum fraction of regime buckets whose IC sign disagrees with the
overall sign before the regime-stability screen fails. 0.34 ≈ "at most
one in three buckets flip" — looser than 0.5 (would let half flip) but
tighter than the prior implicit "any 4 regimes observed" gate."""

# Multiple-testing correction.
DEFAULT_NUM_FEATURE_FAMILY = 5
"""Holm-Bonferroni correction divisor used when the user hasn't
explicitly declared how many features they're searching across.
The five built-in features in the picker are the canonical default;
the runner exposes ``n_family`` so a user running a wider search can
override."""


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
    """Cost-adjusted long-short spread, anchored on spec direction.

    v1 review's central point: a statistically significant IC can
    correspond to an economically untradeable spread. v2 review found
    a related bug — the page reported a *signed* Q5-Q1 spread on the
    headline (negative for momentum-on-RSI-style features) while the
    cost table reported an *absolute* spread, so the same number
    appeared with two different signs.

    Resolution: report both. ``gross_spread_bps_signed`` is the raw
    Q5-Q1 (top quintile minus bottom quintile, with sign). The
    ``directional_spread_bps`` is what the spec-direction long-short
    actually captures: positive direction = Q5-Q1, negative direction
    = Q1-Q5, two-sided/unknown = ``|Q5-Q1|``. Viability is gated on
    ``directional_spread_bps`` so a spec-mismatched IC sign cannot
    sneak through the economic screen.
    """

    gross_spread_bps_signed: float = 0.0
    """Raw Q5-Q1 spread in bps (×10⁴), preserving sign. Negative when
    the top quintile underperforms the bottom quintile."""
    directional_spread_bps: float = 0.0
    """Spec-direction-aligned spread:

    * ``positive`` direction → Q5 − Q1
    * ``negative`` direction → Q1 − Q5
    * ``two_sided`` / ``unknown`` → ``|Q5 − Q1|``

    This is the spread the trade actually captures, and the basis for
    the economic-viability gate."""
    cost_assumption_one_way_bps: float = 1.0
    cost_erasure_one_way_bps: float = 0.0
    """One-way cost at which the net directional spread crosses zero."""
    net_spread_bps_at_assumption: float = 0.0
    viable_at_assumption: bool = False
    spec_direction: str = "unknown"
    """Direction the cost calc was anchored on; surfaced so the UI can
    distinguish 'inverse-direction discovery' from 'spec-direction
    works'."""
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
    regime_stability_screen: ValidationScreen = field(default_factory=ValidationScreen)
    """Promoted from a Stage 2 sub-criterion to a first-class screen.
    An allocator's chief concern is "does this signal work in more
    than one market state"; hiding this inside ``regimes_observed >=
    4`` was deferring the question."""

    multiple_testing: MultipleTestingWarning = field(default_factory=MultipleTestingWarning)
    cost_viability: CostViability = field(default_factory=CostViability)
    ic_ci: IcCi = field(default_factory=IcCi)

    direction_matches_spec: bool = True
    """False when the headline IC sign disagrees with
    ``feature_spec.expected_direction``. A negative IC on a
    ``positive``-direction feature is "inverse relationship discovered",
    which is a different (and weaker) hypothesis than the one the spec
    proposes; it is not an automatic failure but the statistical
    screen reports it explicitly so the verdict isn't read as 'feature
    passed'."""

    target_signed_appropriate: bool = True
    """False when the spec marks signed forward return as the wrong
    target for this feature (e.g. realized_vol_30). Stage 0 fires
    immediately so the headline IC is preserved as a diagnostic but
    cannot be misread as a verdict."""

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
    expected_direction: str = "unknown",
) -> CostViability:
    """Compute cost-adjusted long-short spread, anchored on spec direction.

    Round-trip cost = 2 × one-way (enter + exit, both legs). Slippage
    and market impact are not modelled. The 1-bp default is
    intentionally tight — cost erasure should be obvious when the
    headline IC's tradeable form is a fraction of a basis point.

    Direction handling (v2 review fix): a negative-direction feature
    (e.g. RSI) trades long-Q1, short-Q5. The signed Q5−Q1 is negative
    when the spec works as intended, so gating on signed > 0 would
    miss the entire point. We compute both:

    * ``gross_spread_bps_signed`` = Q5 − Q1 with sign preserved
      (audit / display only).
    * ``directional_spread_bps`` = the spec-direction-aligned spread.
      The economic gate checks this against the round-trip cost.

    ``two_sided`` / ``unknown`` collapses to ``|Q5 − Q1|`` because the
    sign cannot be claimed in advance.
    """
    if not quantile_bins:
        return CostViability(
            note="No quantile bins available.",
            spec_direction=expected_direction,
        )

    sorted_bins = sorted(quantile_bins, key=lambda b: b.get("bin_number", 0))
    if len(sorted_bins) < 2:
        return CostViability(
            note="Need at least 2 quantile bins.",
            spec_direction=expected_direction,
        )

    bottom = float(sorted_bins[0].get("mean_return", 0.0))
    top = float(sorted_bins[-1].get("mean_return", 0.0))
    gross_spread_signed_bps = (top - bottom) * 10_000.0

    if expected_direction == "negative":
        directional_spread_bps = -gross_spread_signed_bps
    elif expected_direction == "positive":
        directional_spread_bps = gross_spread_signed_bps
    else:  # two_sided / unknown / anything else
        directional_spread_bps = abs(gross_spread_signed_bps)

    round_trip_cost_bps = 2.0 * cost_assumption_one_way_bps
    net_spread_bps = directional_spread_bps - round_trip_cost_bps

    # Cost-erasure threshold = directional_spread_bps / 2 (round-trip
    # cost equals 2× one-way). Reported on the directional spread, not
    # the signed one — the signed value is illustrative.
    erasure_one_way = (
        directional_spread_bps / 2.0 if directional_spread_bps > 0 else 0.0
    )

    return CostViability(
        gross_spread_bps_signed=gross_spread_signed_bps,
        directional_spread_bps=directional_spread_bps,
        cost_assumption_one_way_bps=cost_assumption_one_way_bps,
        cost_erasure_one_way_bps=erasure_one_way,
        net_spread_bps_at_assumption=net_spread_bps,
        viable_at_assumption=net_spread_bps > 0,
        spec_direction=expected_direction,
    )


# ─── Screen evaluators ────────────────────────────────────────────────────


def _direction_matches(mean_ic: float, expected_direction: str) -> bool:
    """Check whether the IC sign matches the spec's prior.

    ``unknown`` and ``two_sided`` accept either sign. ``positive``
    requires IC > 0; ``negative`` requires IC < 0. A near-zero IC is
    sign-ambiguous — the |IC| floor below catches that separately.
    """
    if expected_direction in {"unknown", "two_sided"}:
        return True
    if expected_direction == "positive":
        return mean_ic > 0
    if expected_direction == "negative":
        return mean_ic < 0
    return True  # unrecognised value — treat as no claim.


def _evaluate_statistical_screen(
    *,
    spec: FeatureValidationSpec,
    mean_ic: float,
    nw_p_value: float,
    effective_n: float,
    is_stationary: bool,
    is_monotonic: bool,
    direction_matches_spec: bool,
) -> ValidationScreen:
    """Statistical-shape screen: |IC|, p-value, sample size, spec gates,
    direction match.

    Direction handling (v2 review fix): a feature spec with
    ``expected_direction="positive"`` and a strongly-negative observed
    IC is **not** a Stage 1 pass on the intended hypothesis. It may
    indicate an inverse-relationship discovery, but that is a
    different (and weaker) claim than the spec's. We surface the
    mismatch as a screen failure with a "discovered inverse signal"
    message; the user can then explicitly run with
    ``expected_direction="unknown"`` or flip the spec.
    """
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
    if not direction_matches_spec:
        reasons.append(
            f"IC sign disagrees with spec.expected_direction = "
            f"'{spec.expected_direction}'. Possible inverse-relationship "
            "discovery — a different hypothesis than this spec. Re-run "
            "with expected_direction='unknown' to validate as exploratory."
        )

    return ValidationScreen(
        name="Statistical association",
        description=(
            "Daily IC is large enough, NW-significant, sample is deep "
            "enough, IC sign matches the spec's expected direction, and "
            "spec-required stationarity/monotonicity hold."
        ),
        passed=not reasons,
        required_for_stage1=True,
        failure_reasons=reasons,
    )


def _evaluate_economic_screen(cost: CostViability) -> ValidationScreen:
    """Economic-viability screen: net directional spread > 0 at the assumed cost.

    "Directional" = aligned with the spec's expected direction. For a
    negative-direction feature we trade Q1 long / Q5 short, so the
    relevant gross spread is Q1 − Q5, not the raw signed Q5 − Q1. See
    ``compute_cost_viability`` for the direction-handling rules.
    """
    description = (
        "Net spec-direction long-short spread must exceed the "
        "assumed round-trip cost (2 × one-way)."
    )

    if cost.directional_spread_bps == 0 and cost.gross_spread_bps_signed == 0:
        return ValidationScreen(
            name="Economic viability",
            description=description,
            passed=False,
            required_for_stage1=False,
            failure_reasons=["No quantile spread to evaluate."],
        )

    if cost.viable_at_assumption:
        return ValidationScreen(
            name="Economic viability",
            description=description,
            passed=True,
            required_for_stage1=False,
        )

    sign_note = ""
    if cost.spec_direction in {"positive", "negative"} and (
        cost.gross_spread_bps_signed * cost.directional_spread_bps < 0
    ):
        # Signed and directional spreads disagree in sign → spec mismatch.
        sign_note = (
            f" Signed Q5−Q1 = {cost.gross_spread_bps_signed:.2f} bps; "
            f"spec direction is '{cost.spec_direction}', so the "
            f"trade is Q{('1' if cost.spec_direction == 'negative' else '5')}-long / "
            f"Q{('5' if cost.spec_direction == 'negative' else '1')}-short — "
            "the headline IC sign suggests the *opposite* trade would be "
            "the moneymaker."
        )

    return ValidationScreen(
        name="Economic viability",
        description=description,
        passed=False,
        required_for_stage1=False,
        failure_reasons=[
            f"Directional spread = {cost.directional_spread_bps:.2f} bps; "
            f"round-trip cost (2 × one-way) at {cost.cost_assumption_one_way_bps:.1f} "
            f"bps assumption = {2 * cost.cost_assumption_one_way_bps:.1f} bps. "
            f"Net = {cost.net_spread_bps_at_assumption:.2f} bps. Cost erases the "
            f"alpha at approximately {cost.cost_erasure_one_way_bps:.2f} bps one-way."
            + sign_note
        ],
    )


def _evaluate_regime_stability_screen(
    *,
    regimes_observed: int,
    regime_sign_flip_fraction: float,
) -> ValidationScreen:
    """Regime stability screen (v2 review addition).

    Promoted from a hidden Stage 2 sub-criterion to a first-class
    screen. Allocators care whether a signal works in more than one
    market state; we gate on:

    * at least ``REGIME_STABILITY_MIN_REGIMES_OBSERVED`` (= 4) regime
      buckets observed (vol low/normal/high × trend up/sideways/down),
    * the fraction of buckets where the IC sign disagrees with the
      overall sign is at most
      ``REGIME_STABILITY_MAX_SIGN_FLIP_FRACTION`` (≈ 1 in 3).

    Diagnostic at Stage 1, gating Stage 2+.
    """
    description = (
        "Signal must be observed in enough regime buckets and not flip "
        "sign across them — otherwise the signal works only in one "
        "market state."
    )
    reasons: list[str] = []
    if regimes_observed < REGIME_STABILITY_MIN_REGIMES_OBSERVED:
        reasons.append(
            f"Only {regimes_observed} regime buckets observed; need ≥ "
            f"{REGIME_STABILITY_MIN_REGIMES_OBSERVED}."
        )
    if regime_sign_flip_fraction > REGIME_STABILITY_MAX_SIGN_FLIP_FRACTION:
        reasons.append(
            f"{regime_sign_flip_fraction:.0%} of regime buckets flip IC "
            f"sign vs the overall sign; threshold is "
            f"≤ {REGIME_STABILITY_MAX_SIGN_FLIP_FRACTION:.0%}."
        )
    return ValidationScreen(
        name="Regime stability",
        description=description,
        passed=not reasons,
        required_for_stage1=False,
        failure_reasons=reasons,
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
    regime_stability_screen: ValidationScreen,
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
            regime_stability_screen,
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
    # v2: |IC| floor matches Stage 2 (no bigger-IC bonus). The
    # discriminating factors are OOS retention, cost viability, and
    # regime stability — i.e. is the result *implementable*, not just
    # *flashy*.
    stage3_ok = (
        abs_ic >= STAGE3_MIN_ABS_IC
        and effective_n >= STAGE3_MIN_EFFECTIVE_N
        and test_days >= STAGE3_MIN_TEST_DAYS
        and abs_test_ic >= STAGE3_MIN_ABS_TEST_IC
        and oos_retention >= STAGE3_MIN_OOS_RETENTION
        and holm_p_value <= STAGE3_MAX_HOLM_P
        and cost.viable_at_assumption
        and regime_stability_screen.passed
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
        and regime_stability_screen.passed
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
    regime_sign_flip_fraction: float = 0.0,
    cost_assumption_one_way_bps: float = 1.0,
    n_family: int = DEFAULT_NUM_FEATURE_FAMILY,
) -> FeatureValidationVerdict:
    """Combine screens and stage assignment into a single verdict.

    Order of operations:

    1. **Wrong-target fast-path.** If the spec marks the signed
       forward return as inappropriate (e.g. realized_vol_30), Stage 0
       fires immediately. The IC against signed return is preserved
       as a diagnostic but cannot graduate.
    2. **Direction match.** Compute whether the IC sign agrees with
       ``spec.expected_direction``. A mismatch is folded into the
       statistical screen as a failure reason ("inverse signal
       discovered").
    3. **Five screens** (statistical / economic / OOS / multiple-testing
       / regime stability) plus the IC CI.
    4. **Stage assignment** combining screen passes with numeric
       thresholds.
    5. **Final-decision rendering** for the UI headline.

    Parameters
    ----------
    n_family : int
        Holm-Bonferroni divisor. Default = the 5 built-in features;
        callers running a wider research family should pass the
        actual count.
    regime_sign_flip_fraction : float
        Fraction of regime buckets whose IC sign disagrees with the
        overall sign. Computed by the runner from the robustness
        breakdown; passed in here to keep this module pure.
    """
    direction_matches_spec = _direction_matches(mean_ic, spec.expected_direction)

    cost = compute_cost_viability(
        quantile_bins=quantile_bins,
        cost_assumption_one_way_bps=cost_assumption_one_way_bps,
        expected_direction=spec.expected_direction,
    )

    statistical = _evaluate_statistical_screen(
        spec=spec,
        mean_ic=mean_ic,
        nw_p_value=nw_p_value,
        effective_n=effective_n,
        is_stationary=is_stationary,
        is_monotonic=is_monotonic,
        direction_matches_spec=direction_matches_spec,
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
    regime_stability = _evaluate_regime_stability_screen(
        regimes_observed=regimes_observed,
        regime_sign_flip_fraction=regime_sign_flip_fraction,
    )
    ic_ci = compute_ic_ci(mean_ic=mean_ic, n_eff=effective_n)

    # Wrong-target fast-path: Stage 0 with explicit reason, headline
    # IC kept for diagnostic display only.
    if not spec.is_signed_target_appropriate:
        stage_info = FeatureStageInfo(
            stage=0,
            label="Rejected",
            description=(
                f"Feature spec marks signed forward return as the wrong "
                f"target for '{spec.feature_name}'. The headline IC is a "
                "diagnostic only — graduation is blocked until the "
                "runner supports feature-aware target dispatch."
            ),
            failed_screens=["Wrong target"],
        )
        return FeatureValidationVerdict(
            statistical_screen=statistical,
            economic_screen=economic,
            oos_screen=oos,
            multiple_testing_screen=multiple_testing_screen,
            regime_stability_screen=regime_stability,
            multiple_testing=multiple_testing,
            cost_viability=cost,
            ic_ci=ic_ci,
            direction_matches_spec=direction_matches_spec,
            target_signed_appropriate=False,
            stage_info=stage_info,
            final_decision=(
                f"Do not trade. '{spec.feature_name}' predicts the size "
                "of the next move, not its sign — signed-return IC is "
                "the wrong test. Re-run with an absolute-return target "
                "when feature-aware dispatch lands."
            ),
        )

    stage_info = _compute_stage_info(
        statistical_screen=statistical,
        economic_screen=economic,
        oos_screen=oos,
        multiple_testing_screen=multiple_testing_screen,
        regime_stability_screen=regime_stability,
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
        regime_stability_screen=regime_stability,
        multiple_testing=multiple_testing,
        cost_viability=cost,
        ic_ci=ic_ci,
        direction_matches_spec=direction_matches_spec,
        target_signed_appropriate=True,
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
        if not cost.viable_at_assumption and cost.directional_spread_bps != 0:
            parts.append(
                f"Cost erases the spread at ≈ {cost.cost_erasure_one_way_bps:.2f} bps one-way."
            )
        if not oos.passed and oos.failure_reasons:
            parts.append("OOS evidence weak.")
        return " ".join(parts)
    if stage_info.stage == 2:
        return "Stage 2 — research candidate. Defensible but not paper-trading-ready."
    return "Stage 3 — paper-trading candidate. Not live-trading validated."
