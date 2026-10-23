"""The convergence count is independently checked against exact recurrences."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.engine.indicators.convergence import cascade_steps, decay_steps


@pytest.mark.parametrize("period", [1, 2, 10, 14, 50, 200, 500])
@pytest.mark.parametrize("numerator", [1, 2])
def test_closed_form_reaches_tolerance_on_exact_first_update(period: int, numerator: int) -> None:
    alpha = Decimal(numerator) / Decimal(period + (numerator == 2))
    expected, weight = 0, Decimal(1)
    if alpha != 1:
        while weight > Decimal("0.001"):
            weight *= 1 - alpha
            expected += 1
    assert decay_steps(float(alpha)) == expected


@pytest.mark.parametrize(("source_period", "output_period"), [(14, 14), (26, 9), (100, 200)])
def test_cascade_matches_independent_decimal_recurrence(source_period: int, output_period: int) -> None:
    a, b = Decimal(1) / source_period, Decimal(1) / output_period
    source = output = Decimal(1)
    expected = 0
    while max(source, output) > Decimal("0.001"):
        source *= 1 - a
        output = b * source + (1 - b) * output
        expected += 1
    assert cascade_steps(float(a), float(b)) == expected


@pytest.mark.parametrize("tolerance", [0, 1, -0.1, float("nan"), float("inf")])
def test_invalid_tolerances_are_refused(tolerance: float) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        decay_steps(0.1, tolerance)
    with pytest.raises(ValueError, match="tolerance"):
        cascade_steps(0.1, 0.2, tolerance)


def test_an_indicator_cannot_omit_its_convergence_rule() -> None:
    from app.engine.indicators.base import Indicator

    class MissingRule(Indicator):
        def _compute_next_value(self, timestamp_ms: int, value: Decimal) -> Decimal:
            return value

    with pytest.raises(TypeError, match='bars_to_converge'):
        MissingRule('missing', 2)
