"""One gate applicability matrix and typed outcome authority for Options studies.

Formula: technical invalidity dominates known performance failure, then sparse
  or unknown evidence, then pass. Exact net and drawdown comparisons are unrounded.
Reference: https://github.com/tim1016/learn-ai/issues/3332.
Canonical implementation: this file.
Validated against: tests/research/options_golden_search/test_policy.py.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol

from app.research.options_golden_search.protocol import FrozenModel, Policy, Window

Scope = Literal["training", "forward", "development", "final", "pooled_forward", "stress"]
Status = Literal["meets_rules", "does_not_meet_rules", "not_enough_evidence", "could_not_evaluate"]


class Metrics(Protocol):
    net_profit_usd: Decimal
    max_drawdown_fraction: Decimal
    sharpe: float | None
    sharpe_unavailable_reason: str | None
    daily_observation_count: int
    completed_traded_sessions: int
    max_recovery_sessions: int
    ongoing_recovery: bool
    technical_failures: tuple[str, ...]


class RuleOutcome(FrozenModel):
    rule: str
    scope: Scope
    status: Status
    actual: str | None
    threshold: str | None
    units: str
    reason: str | None = None
    overridable: bool = True


class Verdict(FrozenModel):
    status: Status
    outcomes: tuple[RuleOutcome, ...]

    @property
    def eligible(self) -> bool:
        return self.status == "meets_rules"


def classify(metrics: Metrics, policy: Policy, window: Window, scope: Scope, *, complete: bool = True) -> Verdict:
    outcomes: list[RuleOutcome] = []

    def add(rule: str, status: Status, actual: object, threshold: object, units: str,
            reason: str | None = None, overridable: bool = True) -> None:
        outcomes.append(RuleOutcome(rule=rule, scope=scope, status=status,
            actual=None if actual is None else str(actual), threshold=None if threshold is None else str(threshold),
            units=units, reason=reason, overridable=overridable))

    technical = list(metrics.technical_failures)
    if not complete or metrics.daily_observation_count != len(window.session_ms):
        technical.append("declared scheduled evidence is incomplete")
    add("technical_validity", "could_not_evaluate" if technical else "meets_rules", not technical, True,
        "validity", "; ".join(technical) or None, False)
    add("positive_net", "meets_rules" if metrics.net_profit_usd > 0 else "does_not_meet_rules", metrics.net_profit_usd, 0, "USD")
    add("drawdown", "meets_rules" if metrics.max_drawdown_fraction <= policy.max_drawdown_fraction else "does_not_meet_rules",
        metrics.max_drawdown_fraction, policy.max_drawdown_fraction, "fraction_of_starting_capital")
    add("sharpe", "not_enough_evidence" if metrics.sharpe is None else "meets_rules" if metrics.sharpe >= policy.min_sharpe else "does_not_meet_rules",
        metrics.sharpe, policy.min_sharpe, "annualized_daily_sharpe", metrics.sharpe_unavailable_reason)
    add("activity", "meets_rules" if metrics.completed_traded_sessions >= window.activity_floor else "not_enough_evidence",
        metrics.completed_traded_sessions, window.activity_floor, "completed_traded_setup_sessions")
    add("observations", "meets_rules" if metrics.daily_observation_count >= policy.min_daily_observations else "not_enough_evidence",
        metrics.daily_observation_count, policy.min_daily_observations, "scheduled_sessions")
    if scope in {"final", "pooled_forward"}:
        minimum = policy.min_final_sessions if scope == "final" else policy.min_forward_sessions
        add("approval_sessions", "meets_rules" if metrics.completed_traded_sessions >= minimum else "not_enough_evidence",
            metrics.completed_traded_sessions, minimum, "completed_traded_setup_sessions")
    if policy.max_recovery_sessions is not None:
        status: Status = "does_not_meet_rules" if metrics.max_recovery_sessions > policy.max_recovery_sessions else "not_enough_evidence" if metrics.ongoing_recovery else "meets_rules"
        add("recovery", status, metrics.max_recovery_sessions, policy.max_recovery_sessions,
            "underwater_sessions", "ongoing recovery is censored" if metrics.ongoing_recovery else None)
    return combine(tuple(outcomes))


def combine(outcomes: tuple[RuleOutcome, ...]) -> Verdict:
    precedence: tuple[Status, ...] = ("could_not_evaluate", "does_not_meet_rules", "not_enough_evidence", "meets_rules")
    status = next((s for s in precedence if any(o.status == s for o in outcomes)), "could_not_evaluate")
    return Verdict(status=status, outcomes=outcomes)


class ExceptionOutcome(Protocol):
    @property
    def scope(self) -> str: ...

    @property
    def rule(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def overridable(self) -> bool: ...


class ExceptionVerdict(Protocol):
    @property
    def outcomes(self) -> tuple[ExceptionOutcome, ...]: ...


def validate_exception(verdicts: tuple[ExceptionVerdict, ...], waived_rules: tuple[str, ...], comment: str) -> None:
    """An exception acknowledges each scoped failure and cannot waive technical defects."""
    if not comment.strip():
        raise ValueError("owner exception needs a comment")
    failures = [o for v in verdicts for o in v.outcomes if o.status != "meets_rules"]
    if any(not o.overridable for o in failures):
        raise ValueError("technical validity cannot be overridden")
    expected = {f"{o.scope}:{o.rule}" for o in failures}
    if set(waived_rules) != expected or len(waived_rules) != len(set(waived_rules)):
        raise ValueError("exception must enumerate exactly every failed or insufficient scoped rule")
