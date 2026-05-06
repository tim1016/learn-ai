"""Graduation evaluation for the Signal Engine.

Formula: Stage ladder (0/1/2/3) evaluated against IC t-stat, OOS Sharpe, regime coverage, and deflated Sharpe thresholds per docs/signal-engine-authority.md §4–5. Stage 0 rejection short-circuits all downstream.
Reference: Internal — docs/signal-engine-authority.md §4 (authority for every formula); Bailey & López de Prado (2014) Deflated Sharpe for Stage 2+ gate.
Canonical implementation: app/research/signal/graduation.py
Validated against: NONE — pending (pending-fixture per registry)

The graduation step turns a bag of metrics into a single decision: does
this signal warrant further research, and if so, how far has it gotten?

The decision lives on two surfaces:

1. **Graduation stage** (0/1/2/3) — the ladder rendered on the verdict
   block. A signal is at the highest stage whose criteria it satisfies.
2. **Stage 0 rejection** — a hard kill switch that fires *first* and
   short-circuits interpretation of downstream metrics. When triggered,
   the UI collapses the deeper panels behind a "show diagnostic details
   anyway" disclosure (see `signal-engine-authority.md` § 5).

The legacy A–F grade and "Robust Alpha / Conditional Alpha / Degrading"
status labels are kept for now to avoid breaking the existing UI; they
will be removed once the stage ladder is fully shipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.research.signal.backtest import BacktestResult
from app.research.signal.diagnostics import DataSufficiency, SignalDiagnostics
from app.research.signal.walk_forward import WalkForwardResult

# ─── Stage 0 thresholds (authority: docs/signal-engine-authority.md § 5.1) ──

STAGE0_STABILITY_MIN = 0.25
STAGE0_MEDIAN_OOS_SHARPE_MIN = 0.0  # strict-greater, so ≤ 0 fails
STAGE0_PCT_POSITIVE_FOLDS_MIN = 0.40
STAGE0_TURNOVER_MAX = 200.0
STAGE0_TURNOVER_SHARPE_MIN = 0.5

# ─── Stage advancement thresholds (§ 5.2 – § 5.4) ──────────────────────────

STAGE2_OOS_SHARPE_MIN = 0.30
STAGE2_STABILITY_MIN = 0.30
STAGE2_FOLDS_MIN = 4

STAGE3_OOS_SHARPE_MIN = 0.50
STAGE3_STABILITY_MIN = 0.50
STAGE3_PCT_POSITIVE_FOLDS_MIN = 0.60


@dataclass
class GraduationCriterion:
    """Single graduation criterion with pass/fail status."""

    name: str = ""
    description: str = ""
    passed: bool = False
    value: float = 0.0
    threshold: float = 0.0
    label: str = "Fail"
    failure_reason: str = ""


@dataclass
class ParameterStability:
    """Sensitivity of Sharpe to threshold selection."""

    sharpe_values_by_threshold: dict[float, float] = field(default_factory=dict)
    stability_score: float = 0.0
    stability_label: str = "Fragile"


@dataclass
class Stage0Failure:
    """A single Stage 0 kill criterion that this signal failed."""

    criterion_name: str = ""
    value: float = 0.0
    threshold_repr: str = ""
    """Human-readable threshold (e.g. ``< 0.25`` or ``> 200x AND Sharpe < 0.5``)."""
    message: str = ""


@dataclass
class Stage0Rejection:
    """Kill-switch evaluation. ``rejected=True`` short-circuits the page."""

    rejected: bool = False
    failed_criteria: list[Stage0Failure] = field(default_factory=list)


@dataclass
class StageAdvanceCriterion:
    """One requirement to advance from the current stage to the next."""

    name: str = ""
    description: str = ""
    current_value: float = 0.0
    required_repr: str = ""
    met: bool = False


@dataclass
class GraduationStageInfo:
    """Where the signal sits on the 0 → 1 → 2 → 3 ladder, plus what advancement requires."""

    stage: int = 0
    """0 = Rejected, 1 = Weak, 2 = Research, 3 = Promotion."""
    label: str = "Rejected"
    description: str = ""
    next_stage_label: str = ""
    """Empty when at the top of the ladder."""
    advance_criteria: list[StageAdvanceCriterion] = field(default_factory=list)


@dataclass
class GraduationResult:
    """Complete graduation assessment."""

    criteria: list[GraduationCriterion] = field(default_factory=list)
    overall_passed: bool = False
    overall_grade: str = "F"
    summary: str = ""
    status_label: str = "Exploratory"
    parameter_stability: ParameterStability = field(default_factory=ParameterStability)

    # New: Stage 0 / ladder additions (authority doc § 5).
    stage0_rejection: Stage0Rejection = field(default_factory=Stage0Rejection)
    stage_info: GraduationStageInfo = field(default_factory=GraduationStageInfo)


def evaluate_graduation(
    walk_forward: WalkForwardResult | None,
    backtest_grid: list[BacktestResult],
    regime_coverage: dict[str, int],
    signal_diagnostics: SignalDiagnostics | None,
    data_sufficiency: DataSufficiency | None,
) -> GraduationResult:
    """Evaluate all graduation criteria and determine overall grade and stage."""
    # Find best in-sample result at default cost
    best_is = _find_best_insample(backtest_grid, default_cost=2.0)

    # Parameter stability
    param_stability = _compute_parameter_stability(backtest_grid, default_cost=2.0)

    # Build the legacy 5-criterion checklist (kept for the existing UI).
    criteria: list[GraduationCriterion] = []

    # 1. Net Sharpe > 0.75
    net_sharpe = best_is.net_sharpe if best_is else 0.0
    c1 = GraduationCriterion(
        name="Net Sharpe Ratio",
        description="Best in-sample net Sharpe must exceed 0.75",
        value=net_sharpe,
        threshold=0.75,
        passed=net_sharpe > 0.75,
    )
    c1.label = _pass_label(c1.passed, net_sharpe, 0.75, 0.5)
    if not c1.passed:
        c1.failure_reason = (
            f"Net Sharpe = {net_sharpe:.2f} is below 0.75 threshold. "
            "Consider stronger feature transformation or longer lookback."
        )
    criteria.append(c1)

    # 2. Max drawdown < 15%
    max_dd = best_is.max_drawdown if best_is else 1.0
    c2 = GraduationCriterion(
        name="Maximum Drawdown",
        description="Max drawdown must be below 15%",
        value=max_dd,
        threshold=0.15,
        passed=max_dd < 0.15,
    )
    c2.label = _pass_label(c2.passed, max_dd, 0.15, 0.20, invert=True)
    if not c2.passed:
        c2.failure_reason = (
            f"Max Drawdown = {max_dd * 100:.1f}% exceeds 15% threshold. Consider tighter threshold or shorter holding."
        )
    criteria.append(c2)

    # 3. OOS windows positive Sharpe > 60%
    pct_pos_sharpe = walk_forward.pct_windows_positive_sharpe if walk_forward else 0.0
    c3 = GraduationCriterion(
        name="OOS Windows Positive Sharpe",
        description="At least 60% of walk-forward windows must have positive Sharpe",
        value=pct_pos_sharpe,
        threshold=0.60,
        passed=pct_pos_sharpe > 0.60,
    )
    c3.label = _pass_label(c3.passed, pct_pos_sharpe, 0.60, 0.40)
    if not c3.passed:
        c3.failure_reason = (
            f"Only {pct_pos_sharpe * 100:.0f}% of OOS windows have positive Sharpe "
            f"(threshold: 60%). Signal may be unstable."
        )
    criteria.append(c3)

    # 4. Regime coverage >= 4 of 6
    regimes_covered = sum(1 for v in regime_coverage.values() if v > 0)
    c4 = GraduationCriterion(
        name="Regime Coverage",
        description="At least 4 of 6 regimes must have observations",
        value=float(regimes_covered),
        threshold=4.0,
        passed=regimes_covered >= 4,
    )
    c4.label = "Pass" if c4.passed else ("Marginal" if regimes_covered >= 3 else "Fail")
    if not c4.passed:
        c4.failure_reason = (
            f"Only {regimes_covered}/6 regimes covered. Need more data spanning different market conditions."
        )
    criteria.append(c4)

    # 5. Parameter stability score > 0.5
    c5 = GraduationCriterion(
        name="Parameter Stability",
        description="Stability score must exceed 0.5 (insensitive to threshold choice)",
        value=param_stability.stability_score,
        threshold=0.5,
        passed=param_stability.stability_score > 0.5,
    )
    c5.label = _pass_label(c5.passed, param_stability.stability_score, 0.5, 0.3)
    if not c5.passed:
        c5.failure_reason = (
            f"Stability score = {param_stability.stability_score:.2f} (threshold: 0.50). "
            "Performance is sensitive to threshold selection — possible overfit."
        )
    criteria.append(c5)

    # Overall (legacy)
    passed_count = sum(1 for c in criteria if c.passed)
    overall_passed = passed_count == len(criteria)
    overall_grade = _compute_grade(passed_count, len(criteria))

    # Stage 0 kill switch (authority § 5.1).
    stage0 = _evaluate_stage0(
        walk_forward=walk_forward,
        param_stability=param_stability,
        best_is=best_is,
    )

    # Graduation stage (authority § 5.2 – § 5.4).
    stage_info = _compute_stage_info(
        walk_forward=walk_forward,
        param_stability=param_stability,
        stage0=stage0,
    )

    # Status label (legacy) — only mark "Degrading" when the alpha-decay
    # test is statistically valid and significant. Without the power
    # guard the badge fired for any whisper of a negative slope, even
    # with three folds.
    status_label = _compute_status_label(
        data_sufficiency=data_sufficiency,
        walk_forward=walk_forward,
        overall_passed=overall_passed,
        param_stability=param_stability,
    )

    summary = _generate_summary(criteria, overall_grade, status_label, best_is)

    return GraduationResult(
        criteria=criteria,
        overall_passed=overall_passed,
        overall_grade=overall_grade,
        summary=summary,
        status_label=status_label,
        parameter_stability=param_stability,
        stage0_rejection=stage0,
        stage_info=stage_info,
    )


# ─── Stage 0 evaluation ────────────────────────────────────────────────────


def _evaluate_stage0(
    walk_forward: WalkForwardResult | None,
    param_stability: ParameterStability,
    best_is: BacktestResult | None,
) -> Stage0Rejection:
    """Run the four kill-switch checks. Any one failure rejects the signal.

    The thresholds here are the project defaults adopted from external
    methodology review (2026-04-30) and codified in
    ``docs/signal-engine-authority.md`` § 5.1. Tuning them requires
    updating the constants at the top of this module, the authority
    document, and the matching test.
    """
    failures: list[Stage0Failure] = []

    # Criterion A: Parameter stability < 0.25
    stability_score = param_stability.stability_score
    if stability_score < STAGE0_STABILITY_MIN:
        failures.append(
            Stage0Failure(
                criterion_name="Parameter Stability",
                value=stability_score,
                threshold_repr=f"≥ {STAGE0_STABILITY_MIN:.2f}",
                message=(
                    f"Stability is {stability_score:.2f}. Below {STAGE0_STABILITY_MIN:.2f} "
                    "the Sharpe ratio is highly sensitive to threshold selection — "
                    "the apparent edge is most likely a noise fit."
                ),
            )
        )

    # Criterion B: Median OOS Sharpe ≤ 0
    if walk_forward is not None and walk_forward.windows:
        median_oos = walk_forward.median_oos_sharpe
        if median_oos <= STAGE0_MEDIAN_OOS_SHARPE_MIN:
            failures.append(
                Stage0Failure(
                    criterion_name="Median OOS Sharpe",
                    value=median_oos,
                    threshold_repr=f"> {STAGE0_MEDIAN_OOS_SHARPE_MIN:.1f}",
                    message=(
                        f"Median walk-forward OOS Sharpe is {median_oos:.2f}. "
                        "When the median fold has zero or negative Sharpe, the "
                        "headline mean is being carried by one or two outlier folds."
                    ),
                )
            )

        # Criterion C: % positive folds < 40%
        pct_positive = walk_forward.pct_windows_positive_sharpe
        if pct_positive < STAGE0_PCT_POSITIVE_FOLDS_MIN:
            failures.append(
                Stage0Failure(
                    criterion_name="OOS Folds Positive",
                    value=pct_positive,
                    threshold_repr=f"≥ {STAGE0_PCT_POSITIVE_FOLDS_MIN * 100:.0f} %",
                    message=(
                        f"Only {pct_positive * 100:.0f}% of OOS folds had positive "
                        "Sharpe. A signal with edge should win in most folds."
                    ),
                )
            )

    # Criterion D: turnover > 200x AND Sharpe < 0.5
    if best_is is not None:
        turnover = best_is.annualized_turnover
        sharpe = best_is.net_sharpe
        if turnover > STAGE0_TURNOVER_MAX and sharpe < STAGE0_TURNOVER_SHARPE_MIN:
            failures.append(
                Stage0Failure(
                    criterion_name="Turnover vs Edge",
                    value=turnover,
                    threshold_repr=(
                        f"≤ {STAGE0_TURNOVER_MAX:.0f}× / yr OR Sharpe ≥ "
                        f"{STAGE0_TURNOVER_SHARPE_MIN:.1f}"
                    ),
                    message=(
                        f"Best IS turnover is {turnover:.0f}× / yr at Sharpe "
                        f"{sharpe:.2f}. With > {STAGE0_TURNOVER_MAX:.0f}× turnover, "
                        "realistic transaction costs and slippage will eat any "
                        "Sharpe below 0.5 — the signal is uneconomic regardless "
                        "of its statistical properties."
                    ),
                )
            )

    return Stage0Rejection(rejected=len(failures) > 0, failed_criteria=failures)


# ─── Stage ladder ──────────────────────────────────────────────────────────


def _compute_stage_info(
    walk_forward: WalkForwardResult | None,
    param_stability: ParameterStability,
    stage0: Stage0Rejection,
) -> GraduationStageInfo:
    """Determine current stage and the advance criteria to the next one."""
    if stage0.rejected:
        return GraduationStageInfo(
            stage=0,
            label="Rejected",
            description=(
                "Failed one or more Stage 0 kill criteria. Downstream metrics "
                "are not actionable; consider trying a different feature, "
                "horizon, or regime gate."
            ),
            next_stage_label="",
            advance_criteria=[],
        )

    mean_oos = walk_forward.mean_oos_sharpe if walk_forward else 0.0
    stability = param_stability.stability_score
    n_folds = len(walk_forward.windows) if walk_forward else 0
    pct_positive = walk_forward.pct_windows_positive_sharpe if walk_forward else 0.0

    # Stage 3 — Promotion candidate
    if (
        mean_oos > STAGE3_OOS_SHARPE_MIN
        and stability > STAGE3_STABILITY_MIN
        and pct_positive > STAGE3_PCT_POSITIVE_FOLDS_MIN
    ):
        return GraduationStageInfo(
            stage=3,
            label="Promotion Candidate",
            description=(
                "Strong evidence of edge across folds, robust to threshold "
                "choice, and consistent across most folds. Cross-asset "
                "validation and deflated-Sharpe gating apply at this stage."
            ),
            next_stage_label="",
            advance_criteria=[],
        )

    # Stage 2 — Research candidate
    if (
        mean_oos > STAGE2_OOS_SHARPE_MIN
        and stability > STAGE2_STABILITY_MIN
        and n_folds >= STAGE2_FOLDS_MIN
    ):
        return GraduationStageInfo(
            stage=2,
            label="Research Candidate",
            description=(
                "Survives walk-forward and parameter sensitivity checks. "
                "Block-bootstrap CIs and cross-asset validation are enabled "
                "at this stage."
            ),
            next_stage_label="Promotion Candidate",
            advance_criteria=[
                StageAdvanceCriterion(
                    name="Mean OOS Sharpe",
                    description="Headline annualised OOS Sharpe across all folds",
                    current_value=mean_oos,
                    required_repr=f"> {STAGE3_OOS_SHARPE_MIN:.2f}",
                    met=mean_oos > STAGE3_OOS_SHARPE_MIN,
                ),
                StageAdvanceCriterion(
                    name="Parameter Stability",
                    description="1 − coefficient of variation of Sharpe across thresholds",
                    current_value=stability,
                    required_repr=f"> {STAGE3_STABILITY_MIN:.2f}",
                    met=stability > STAGE3_STABILITY_MIN,
                ),
                StageAdvanceCriterion(
                    name="OOS Folds Positive",
                    description="Fraction of folds with positive Sharpe",
                    current_value=pct_positive,
                    required_repr=f"> {STAGE3_PCT_POSITIVE_FOLDS_MIN * 100:.0f} %",
                    met=pct_positive > STAGE3_PCT_POSITIVE_FOLDS_MIN,
                ),
            ],
        )

    # Stage 1 — Weak candidate (survived Stage 0 but doesn't meet Stage 2)
    return GraduationStageInfo(
        stage=1,
        label="Weak Candidate",
        description=(
            "Survived the Stage 0 kill switch. Sharpe CI and walk-forward "
            "details are enabled. The signal needs more evidence before "
            "deeper machinery (cross-asset, bootstrap) is worth running."
        ),
        next_stage_label="Research Candidate",
        advance_criteria=[
            StageAdvanceCriterion(
                name="Mean OOS Sharpe",
                description="Headline annualised OOS Sharpe across all folds",
                current_value=mean_oos,
                required_repr=f"> {STAGE2_OOS_SHARPE_MIN:.2f}",
                met=mean_oos > STAGE2_OOS_SHARPE_MIN,
            ),
            StageAdvanceCriterion(
                name="Parameter Stability",
                description="1 − coefficient of variation of Sharpe across thresholds",
                current_value=stability,
                required_repr=f"> {STAGE2_STABILITY_MIN:.2f}",
                met=stability > STAGE2_STABILITY_MIN,
            ),
            StageAdvanceCriterion(
                name="Walk-Forward Folds",
                description="Independent train/test splits evaluated",
                current_value=float(n_folds),
                required_repr=f"≥ {STAGE2_FOLDS_MIN}",
                met=n_folds >= STAGE2_FOLDS_MIN,
            ),
        ],
    )


# ─── Helpers (legacy) ──────────────────────────────────────────────────────


def _find_best_insample(
    grid: list[BacktestResult],
    default_cost: float,
) -> BacktestResult | None:
    """Find the best result at default cost."""
    candidates = [r for r in grid if abs(r.cost_bps - default_cost) < 0.01]
    if not candidates:
        candidates = grid
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.net_sharpe)


def _compute_parameter_stability(
    grid: list[BacktestResult],
    default_cost: float,
) -> ParameterStability:
    """Assess how sensitive Sharpe is to threshold choice."""
    candidates = [r for r in grid if abs(r.cost_bps - default_cost) < 0.01]
    if not candidates:
        candidates = grid

    sharpe_by_threshold: dict[float, float] = {}
    for r in candidates:
        sharpe_by_threshold[r.threshold] = r.net_sharpe

    values = list(sharpe_by_threshold.values())
    if len(values) < 2:
        return ParameterStability(
            sharpe_values_by_threshold=sharpe_by_threshold,
            stability_score=1.0,
            stability_label="Stable",
        )

    mean_s = float(np.mean(values))
    std_s = float(np.std(values, ddof=1))

    if abs(mean_s) < 1e-10:
        score = 0.0
    else:
        score = max(0.0, 1.0 - std_s / abs(mean_s))

    if score >= 0.7:
        label = "Stable"
    elif score >= 0.4:
        label = "Sensitive"
    else:
        label = "Fragile"

    return ParameterStability(
        sharpe_values_by_threshold=sharpe_by_threshold,
        stability_score=score,
        stability_label=label,
    )


def _pass_label(
    passed: bool,
    value: float,
    threshold: float,
    marginal: float,
    invert: bool = False,
) -> str:
    """Determine Pass/Fail/Marginal label."""
    if passed:
        return "Pass"
    if invert:
        return "Marginal" if value < marginal else "Fail"
    return "Marginal" if value >= marginal else "Fail"


def _compute_grade(passed: int, total: int) -> str:
    """Map pass count to letter grade."""
    if passed >= total:
        return "A"
    if passed >= total - 1:
        return "B"
    if passed >= total - 2:
        return "C"
    if passed >= total - 3:
        return "D"
    return "F"


def _compute_status_label(
    data_sufficiency: DataSufficiency | None,
    walk_forward: WalkForwardResult | None,
    overall_passed: bool,
    param_stability: ParameterStability,
) -> str:
    """Determine signal status label.

    The "Degrading" label requires the alpha-decay test to be both valid
    (≥ 5 folds) and statistically significant. With three or four folds
    the regression is too underpowered to drive a label.
    """
    # Exploratory: insufficient data
    if data_sufficiency and data_sufficiency.effective_oos_bars < 1000:
        return "Exploratory"
    if walk_forward and len(walk_forward.windows) < 3:
        return "Exploratory"
    if not walk_forward or not walk_forward.windows:
        return "Exploratory"

    # Degrading: alpha decay (only when statistically supported).
    decay = walk_forward.alpha_decay
    if (
        getattr(decay, "is_test_valid", False)
        and getattr(decay, "is_significant", False)
        and decay.slope < 0
    ):
        return "Degrading"

    # Robust Alpha
    if (
        overall_passed
        and param_stability.stability_label == "Stable"
        and walk_forward.pct_windows_positive_sharpe >= 0.70
    ):
        return "Robust Alpha"

    # Conditional Alpha
    if overall_passed:
        return "Conditional Alpha"

    return "Exploratory"


def _generate_summary(
    criteria: list[GraduationCriterion],
    grade: str,
    status: str,
    best: BacktestResult | None,
) -> str:
    """Generate human-readable graduation summary."""
    passed = sum(1 for c in criteria if c.passed)
    total = len(criteria)
    parts = [f"Grade: {grade} ({passed}/{total} criteria passed). Status: {status}."]

    if best:
        parts.append(
            f"Best in-sample: Net Sharpe {best.net_sharpe:.2f}, "
            f"Max DD {best.max_drawdown * 100:.1f}%, "
            f"Turnover {best.annualized_turnover:.1f}x/year."
        )

    failed = [c for c in criteria if not c.passed]
    if failed:
        parts.append("Failed: " + "; ".join(c.name for c in failed) + ".")

    return " ".join(parts)
