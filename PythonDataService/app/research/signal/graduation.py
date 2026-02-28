"""Graduation criteria evaluation for signal promotion."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.research.signal.backtest import BacktestResult
from app.research.signal.diagnostics import DataSufficiency, SignalDiagnostics
from app.research.signal.walk_forward import WalkForwardResult


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
class GraduationResult:
    """Complete graduation assessment."""

    criteria: list[GraduationCriterion] = field(default_factory=list)
    overall_passed: bool = False
    overall_grade: str = "F"
    summary: str = ""
    status_label: str = "Exploratory"
    parameter_stability: ParameterStability = field(default_factory=ParameterStability)


def evaluate_graduation(
    walk_forward: WalkForwardResult | None,
    backtest_grid: list[BacktestResult],
    regime_coverage: dict[str, int],
    signal_diagnostics: SignalDiagnostics | None,
    data_sufficiency: DataSufficiency | None,
) -> GraduationResult:
    """Evaluate all graduation criteria and determine overall grade."""
    # Find best in-sample result at default cost
    best_is = _find_best_insample(backtest_grid, default_cost=2.0)

    # Parameter stability
    param_stability = _compute_parameter_stability(backtest_grid, default_cost=2.0)

    # Build criteria
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
            f"Max Drawdown = {max_dd * 100:.1f}% exceeds 15% threshold. "
            "Consider tighter threshold or shorter holding."
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
            f"Only {regimes_covered}/6 regimes covered. "
            "Need more data spanning different market conditions."
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

    # Overall
    passed_count = sum(1 for c in criteria if c.passed)
    overall_passed = passed_count == len(criteria)
    overall_grade = _compute_grade(passed_count, len(criteria))

    # Status label
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
    )


def _find_best_insample(
    grid: list[BacktestResult], default_cost: float,
) -> BacktestResult | None:
    """Find the best result at default cost."""
    candidates = [r for r in grid if abs(r.cost_bps - default_cost) < 0.01]
    if not candidates:
        candidates = grid
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.net_sharpe)


def _compute_parameter_stability(
    grid: list[BacktestResult], default_cost: float,
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
    passed: bool, value: float, threshold: float, marginal: float,
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
    """Determine signal status label."""
    # Exploratory: insufficient data
    if data_sufficiency and data_sufficiency.effective_oos_bars < 1000:
        return "Exploratory"
    if walk_forward and len(walk_forward.windows) < 3:
        return "Exploratory"
    if not walk_forward or not walk_forward.windows:
        return "Exploratory"

    # Degrading: alpha decay
    if walk_forward.oos_sharpe_trend_slope < -0.1:
        return "Degrading"

    # Robust Alpha
    if (overall_passed
            and param_stability.stability_label == "Stable"
            and walk_forward.pct_windows_positive_sharpe >= 0.70):
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
