"""Recency Chart strategy eligibility.

Formula: a strategy is eligible for the Recency Chart iff the shared sweep
predicate admits its registration — ``production_candidate`` category, a
registered signal program, and every PUBLIC non-``symbol`` parameter a
plain integer or number. Recency used to inspect the raw model here
(hidden parameters included), which could disagree with the schema the
researcher sees; PRD #1926 generalized the rule and this module now
delegates to it, so "which strategies can be swept" has one source.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577 (design spec
D1); PRD https://github.com/tim1016/learn-ai/issues/1926 "Domain and
eligibility".
Canonical implementation: app/research/sweep/eligibility.py.
Validated against: tests/research/recency/test_eligibility.py,
tests/research/sweep/test_eligibility.py.
"""

from __future__ import annotations

from app.engine.strategy.registry import StrategyRegistration
from app.research.sweep.eligibility import sweep_eligibility


def is_recency_supported(registration: StrategyRegistration) -> bool:
    """Return True iff the shared sweep predicate admits ``registration``."""
    return sweep_eligibility(registration).eligible
