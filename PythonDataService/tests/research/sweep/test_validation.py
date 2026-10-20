"""Grid validation before launch (PRD #1926 "Grid and workload", review F13)."""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.sweep.grid import LowHighStepRange, ValueListRange
from app.research.sweep.validation import GridInvalidError, WorkloadLimitError, validate_grid

SMA = _STRATEGY_REGISTRY["sma_crossover"]


def _validate(limit: int = 5_000, multiplier: int = 1, **param_ranges):
    return validate_grid(SMA, strategy_key="sma_crossover", symbol="SPY", param_ranges=param_ranges, limit=limit, multiplier=multiplier)


@pytest.mark.parametrize(
    ("ranges", "combinations"),
    [
        pytest.param(
            (("short_window", ValueListRange((5.0, 10.0))), ("long_window", LowHighStepRange(20, 40, 10))),
            6,
            id="well_formed_grid_reports_its_combination_count",
        ),
        pytest.param((("short_window", LowHighStepRange(5.0, 5.0, 1e-20)),), 1, id="single_cell_range_ignores_the_step_size"),
    ],
)
def test_a_valid_grid_reports_its_combination_count(ranges: tuple[tuple[str, ValueListRange | LowHighStepRange], ...], combinations: int) -> None:
    validated = _validate(**dict(ranges))

    assert validated.combinations == combinations


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


def test_a_strategy_rule_refuses_an_invalid_grid_candidate_before_launch() -> None:
    registration = _STRATEGY_REGISTRY["spy_strategy_a"]

    with pytest.raises(GridInvalidError, match=r"macd_fast \(30\) must be less than macd_slow \(26\)"):
        validate_grid(
            registration,
            strategy_key="spy_strategy_a",
            symbol="SPY",
            param_ranges={"macd_fast": ValueListRange((12.0, 30.0))},
            limit=10,
        )


def test_the_limit_is_on_total_backtests_not_combinations() -> None:
    _validate(limit=10, short_window=ValueListRange((5.0, 6.0)), long_window=ValueListRange((20.0, 30.0, 40.0, 50.0, 60.0)))

    with pytest.raises(WorkloadLimitError, match="20 backtests exceed the limit of 10") as excinfo:
        _validate(limit=10, multiplier=2, short_window=ValueListRange((5.0, 6.0)), long_window=ValueListRange((20.0, 30.0, 40.0, 50.0, 60.0)))

    assert (excinfo.value.total_backtests, excinfo.value.limit) == (20, 10)


def test_one_under_the_limit_is_admitted_and_one_over_is_refused() -> None:
    _validate(limit=5, long_window=LowHighStepRange(20, 60, 10))  # 5 combinations
    with pytest.raises(WorkloadLimitError):
        _validate(limit=4, long_window=LowHighStepRange(20, 60, 10))


@pytest.mark.parametrize(
    ("low", "high", "step", "match"),
    [
        pytest.param(
            0.12345678901234566,
            0.12345678901234568,
            1e-18,
            "collapse to the same float",
            id="step_below_float_resolution_is_an_invalid_grid_not_a_server_fault",
        ),
        pytest.param(0.0, 1.0, 1e-28, None, id="absurd_step_is_refused_before_any_expansion"),
        pytest.param(5.0, 10.0, float("nan"), "finite", id="non_finite_bound_is_an_invalid_grid"),
        # Endpoint neighbours resolve, an interior pair does not (review finding).
        pytest.param(
            0.9999999999999944,
            0.9999999999999949,
            1.0880185641326534e-16,
            "collapse to the same float",
            id="interior_float_collapse_is_an_invalid_grid_not_a_server_fault",
        ),
    ],
)
def test_an_unresolvable_step_or_bound_is_an_invalid_grid(low: float, high: float, step: float, match: str | None) -> None:
    with pytest.raises(GridInvalidError, match=match):
        _validate(short_window=LowHighStepRange(low, high, step))
