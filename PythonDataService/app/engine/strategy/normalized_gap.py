"""Normalized (basis-point) gap between two price-scale quantities.

Formula: ``difference_bps(left, right) = 10,000 * (left - right) / right``.
Reference: ``docs/references/spy-ema-normalized-gap-walk-forward.md``.
Canonical implementation: this file. ``app.engine.strategy.spec.primitives``
re-exports it so the spec evaluator's ``DifferenceBps`` operand and the
hand-coded ``EmaCrossoverSignalAlgorithm`` gate share one implementation
rather than two that can drift.
Validated against: ``tests/engine/strategy/spec/test_difference_bps_operand.py``
and its exact-Decimal golden fixture ``tests/fixtures/golden/spy-ema-difference-bps``.

This module deliberately imports nothing but ``Decimal``. It is inside the
sealed signal-decision closure of every program whose entry gate is
normalized (``EmaCrossoverSignalParams.gap_bps > 0``), and that closure
is re-qualified whenever a file in it changes. Living in ``spec.primitives``
-- which reaches the spec evaluator, schema, indicator adapters, and the ML
artifact loader -- would have put all of those inside the seal, so an
unrelated edit to the spec subsystem would invalidate a strategy's build
proof. Keep this module leaf-shaped.
"""

from __future__ import annotations

from decimal import Decimal


def difference_bps(left: Decimal, right: Decimal) -> Decimal:
    """Return the relative ``left - right`` gap in basis points."""
    if right == 0:
        raise ZeroDivisionError("DifferenceBps denominator evaluated to zero")
    return Decimal(10_000) * (left - right) / right


__all__ = ["difference_bps"]
