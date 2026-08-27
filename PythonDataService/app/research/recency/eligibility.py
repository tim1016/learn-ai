"""Recency Chart strategy eligibility.

Formula: a strategy is eligible iff every parameter in its schema, except
``symbol``, is a plain ``int`` or ``float``. This derives D1's "long-only
equity strategies with numeric parameters" gate structurally from the
param schema rather than a hand-maintained per-strategy flag — a
categorical parameter (e.g. the options-spread strategy's ``spread_type``,
``pricing_mode``) fails the check and the strategy is excluded by
default. A newly registered strategy with a non-numeric param is excluded
until proven eligible (fail closed), not silently included.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design
spec docs/superpowers/specs/2026-08-16-recency-chart-design.md D1.
Canonical implementation: this file.
Validated against: tests/research/recency/test_eligibility.py.
"""

from __future__ import annotations

from app.engine.strategy.params import StrategyParamsBase

_NUMERIC_TYPES = (int, float)


def is_recency_supported(schema: type[StrategyParamsBase]) -> bool:
    """Return True iff every non-``symbol`` field of ``schema`` is numeric."""
    for name, field in schema.model_fields.items():
        if name == "symbol":
            continue
        if field.annotation not in _NUMERIC_TYPES:
            return False
    return True
