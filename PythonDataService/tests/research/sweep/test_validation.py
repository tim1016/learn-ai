"""Grid validation before launch (PRD #1926 "Grid and workload", review F13)."""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.sweep.grid import LowHighStepRange, ValueListRange
from app.research.sweep.validation import GridInvalidError, WorkloadLimitError, validate_grid

SMA = _STRATEGY_REGISTRY["sma_crossover"]


def _validate(limit: int = 5_000, multiplier: int = 1, **param_ranges):
    return validate_grid(SMA, strategy_key="sma_crossover", symbol="SPY", param_ranges=param_ranges, limit=limit, multiplier=multiplier)


def test_a_well_formed_grid_reports_its_combination_count() -> None:
    validated = _validate(short_window=ValueListRange((5.0, 10.0)), long_window=LowHighStepRange(20, 40, 10))

    assert validated.combinations == 6


def test_a_repeated_explicit_value_is_refused_naming_the_parameter() -> None:
    with pytest.raises(GridInvalidError, match=r"'short_window' lists the value 5\.0 more than once") as excinfo:
        _validate(short_window=ValueListRange((5.0, 10.0, 5.0)))

    assert excinfo.value.parameter == "short_window"


def test_an_interior_fractional_value_for_an_integer_field_refuses_the_whole_grid() -> None:
    with pytest.raises(GridInvalidError, match=r"short_window=2\.5") as excinfo:
        _validate(short_window=ValueListRange((2.0, 2.5, 3.0)), long_window=ValueListRange((10.0,)))

    assert excinfo.value.parameter == "short_window"
    assert excinfo.value.value == 2.5


def test_valid_corners_with_an_invalid_interior_combination_are_refused() -> None:
    # (10,15), (10,30), (20,15), (20,30): the corners pass, (20, 15) violates short < long.
    with pytest.raises(GridInvalidError, match="not executable"):
        _validate(short_window=ValueListRange((10.0, 20.0)), long_window=ValueListRange((15.0, 30.0)))


def test_the_limit_is_on_total_backtests_not_combinations() -> None:
    _validate(limit=10, short_window=ValueListRange((5.0, 6.0)), long_window=ValueListRange((20.0, 30.0, 40.0, 50.0, 60.0)))

    with pytest.raises(WorkloadLimitError, match="20 backtests exceed the limit of 10") as excinfo:
        _validate(limit=10, multiplier=2, short_window=ValueListRange((5.0, 6.0)), long_window=ValueListRange((20.0, 30.0, 40.0, 50.0, 60.0)))

    assert (excinfo.value.total_backtests, excinfo.value.limit) == (20, 10)


def test_one_under_the_limit_is_admitted_and_one_over_is_refused() -> None:
    _validate(limit=5, long_window=LowHighStepRange(20, 60, 10))  # 5 combinations
    with pytest.raises(WorkloadLimitError):
        _validate(limit=4, long_window=LowHighStepRange(20, 60, 10))
