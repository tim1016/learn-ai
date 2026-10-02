"""The final test's outcome under the frozen rules: meets, does not meet, not enough evidence, or could not evaluate.

Formula, first matching rule wins:
  1. the candidate's run failed                                   → could not evaluate
  2. ``trades < exam_min_trades`` or the objective is undefined   → not enough evidence
  3. any check fails                                              → does not meet the rules
  4. any check is not available                                   → not enough evidence
  5. every check passes                                           → meets the rules
Checks: ``NET_POSITIVE`` net profit after the stated costs ``> 0``;
``DRAWDOWN_WITHIN`` ``max_drawdown_pct <= max_drawdown_ceiling`` (fractions
of peak equity); ``SAMPLE_FLOOR`` ``trades >= exam_min_trades``;
``BEATS_INCUMBENT`` candidate objective ``>=`` the frozen incumbent's
objective over the same interval. An undefined value is ``not_available`` —
never a pass — and a missing, failed or undefined incumbent leaves
``BEATS_INCUMBENT`` not available, so such an exam cannot meet the rules.
Retention ``= exam objective / development objective`` when both are finite
and the development objective is positive, else ``None``; it is descriptive
only and never a check (a selection-inflated denominator over a short, noisy
numerator establishes nothing on its own).
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Open one final test".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_exam_rules.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from app.research.golden_search.protocol import SelectionPolicy
from app.research.golden_search.selection import Metrics, objective_value

ExamOutcome = Literal["meets_rules", "does_not_meet_rules", "not_enough_evidence", "could_not_evaluate"]
CheckStatus = Literal["pass", "fail", "not_available"]

_MEASURE_NAMES = {"sharpe_ratio": "Sharpe ratio", "total_return_pct": "total return", "net_profit": "net profit"}


@dataclass(frozen=True)
class ExamCheck:
    code: str
    label: str
    status: CheckStatus
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "label": self.label, "status": self.status, "detail": self.detail}


class ExamJudgement(NamedTuple):
    outcome: ExamOutcome
    checks: tuple[ExamCheck, ...]
    retention: float | None


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _net_positive(candidate: Metrics) -> ExamCheck:
    label = "Positive net result after stated costs"
    net = _finite(candidate.net_profit)
    if net is None:
        return ExamCheck("NET_POSITIVE", label, "not_available", "Net profit is not available.")
    return ExamCheck("NET_POSITIVE", label, "pass" if net > 0 else "fail", f"Net profit ${net:,.2f}.")


def _drawdown_within(candidate: Metrics, ceiling: float) -> ExamCheck:
    label = "Worst drawdown within your ceiling"
    drawdown = _finite(candidate.max_drawdown_pct)
    if drawdown is None:
        return ExamCheck("DRAWDOWN_WITHIN", label, "not_available", "Worst drawdown is not available.")
    detail = f"Worst drawdown {drawdown:.2%} of peak equity; ceiling {ceiling:.2%}."
    return ExamCheck("DRAWDOWN_WITHIN", label, "pass" if drawdown <= ceiling else "fail", detail)


def _sample_floor(candidate: Metrics, exam_min_trades: int) -> ExamCheck:
    label = "Enough completed trades"
    detail = f"{candidate.total_trades} trades; the minimum is {exam_min_trades}."
    return ExamCheck("SAMPLE_FLOOR", label, "pass" if candidate.total_trades >= exam_min_trades else "fail", detail)


def _beats_incumbent(candidate_objective: float | None, incumbent: Metrics | None, policy: SelectionPolicy) -> ExamCheck:
    label = "At least as good as the current settings"
    measure = _MEASURE_NAMES[policy.objective]
    if incumbent is None or incumbent.status != "completed":
        return ExamCheck("BEATS_INCUMBENT", label, "not_available", "The current settings have no completed final-test run to compare.")
    incumbent_objective = objective_value(incumbent, policy)
    if incumbent_objective is None or candidate_objective is None:
        return ExamCheck("BEATS_INCUMBENT", label, "not_available", f"The {measure} is undefined for one side, so they cannot be compared.")
    status: CheckStatus = "pass" if candidate_objective >= incumbent_objective else "fail"
    return ExamCheck(
        "BEATS_INCUMBENT",
        label,
        status,
        f"Candidate {measure} {candidate_objective:.4g}; current settings {incumbent_objective:.4g}.",
    )


def retention(exam_objective: float | None, development_objective: float | None) -> float | None:
    """Descriptive exam/development objective ratio; ``None`` unless both are finite and development is positive."""
    exam, development = _finite(exam_objective), _finite(development_objective)
    if exam is None or development is None or development <= 0:
        return None
    return exam / development


def judge_exam(
    candidate: Metrics,
    incumbent: Metrics | None,
    *,
    policy: SelectionPolicy,
    exam_min_trades: int,
    development_objective: float | None,
) -> ExamJudgement:
    """Apply the frozen final-test rules to the candidate's and the incumbent's final-interval runs.

    A plan that does not require a positive net result has no net-result
    check: the frozen rules it judges are the plan's own.
    """
    if candidate.status != "completed":
        detail = f"The final-test run failed: {candidate.error or 'no reason recorded'}."
        rules = (
            *((("NET_POSITIVE", "Positive net result after stated costs"),) if policy.require_positive_net else ()),
            ("DRAWDOWN_WITHIN", "Worst drawdown within your ceiling"),
            ("SAMPLE_FLOOR", "Enough completed trades"),
            ("BEATS_INCUMBENT", "At least as good as the current settings"),
        )
        return ExamJudgement("could_not_evaluate", tuple(ExamCheck(code, label, "not_available", detail) for code, label in rules), None)
    candidate_objective = objective_value(candidate, policy)
    checks = (
        *((_net_positive(candidate),) if policy.require_positive_net else ()),
        _drawdown_within(candidate, policy.max_drawdown_ceiling),
        _sample_floor(candidate, exam_min_trades),
        _beats_incumbent(candidate_objective, incumbent, policy),
    )
    kept = retention(candidate_objective, development_objective)
    statuses = {check.status for check in checks}
    if candidate.total_trades < exam_min_trades or candidate_objective is None:
        outcome: ExamOutcome = "not_enough_evidence"
    elif "fail" in statuses:
        outcome = "does_not_meet_rules"
    elif "not_available" in statuses:
        outcome = "not_enough_evidence"
    else:
        outcome = "meets_rules"
    return ExamJudgement(outcome, checks, kept)
