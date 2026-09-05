"""Which registered strategies a parameter sweep may offer, and why not.

Formula: a registration is sweepable iff (a) its ``strategy_category`` is
``production_candidate`` — the operational validation harness is excluded by
category, never by a hand-maintained list — (b) it registers a signal
program, because the warmup requirement is measured through that program's
decision seam, and (c) every parameter in the PUBLIC schema except
``symbol`` is a plain integer or number. Judging the public schema — the
one Strategy Lab renders — rather than the raw model means a hidden,
internally injected parameter can never explain an exclusion the researcher
cannot see. The answer is structured (flag, reason codes, offending public
parameters) so the catalogue can explain itself; Recency Chart, Grid
Search, and Walk-Forward all derive from this one predicate.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Domain and
  eligibility"; Recency Chart design spec D1 (the original numeric-only rule).
Canonical implementation: this file.
Validated against: tests/research/sweep/test_eligibility.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration, public_params_schema

REASON_NOT_PRODUCTION_CANDIDATE = "NOT_PRODUCTION_CANDIDATE"
REASON_NO_SIGNAL_PROGRAM = "NO_SIGNAL_PROGRAM"
REASON_NON_NUMERIC_PUBLIC_PARAMETER = "NON_NUMERIC_PUBLIC_PARAMETER"

_NUMERIC_JSON_TYPES = frozenset({"integer", "number"})


@dataclass(frozen=True)
class SweepEligibility:
    eligible: bool
    reason_codes: tuple[str, ...]
    offending_parameters: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "offending_parameters": list(self.offending_parameters),
        }


def non_numeric_parameters(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Names of every non-``symbol`` property in a JSON schema that is not a plain number."""
    properties: Mapping[str, Any] = schema.get("properties", {}) or {}
    return tuple(
        sorted(
            name
            for name, property_schema in properties.items()
            if name != "symbol" and property_schema.get("type") not in _NUMERIC_JSON_TYPES
        )
    )


def sweep_eligibility(registration: StrategyRegistration) -> SweepEligibility:
    """The structured answer for one registration."""
    reasons: list[str] = []
    if registration.strategy_category != "production_candidate":
        reasons.append(REASON_NOT_PRODUCTION_CANDIDATE)
    if registration.signal_program_factory is None:
        reasons.append(REASON_NO_SIGNAL_PROGRAM)
    offending = non_numeric_parameters(public_params_schema(registration))
    if offending:
        reasons.append(REASON_NON_NUMERIC_PUBLIC_PARAMETER)
    return SweepEligibility(eligible=not reasons, reason_codes=tuple(reasons), offending_parameters=offending)


def eligible_strategy_keys() -> list[str]:
    """Catalogue-visible registrations a sweep may offer, in registry order."""
    return [
        key
        for key, registration in _STRATEGY_REGISTRY.items()
        if registration.catalog_visible and sweep_eligibility(registration).eligible
    ]
