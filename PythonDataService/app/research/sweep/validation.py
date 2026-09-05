"""Pre-launch validation of one strategy's parameter grid.

Two checks the size arithmetic alone cannot make:

* **Duplicate values.** ``expand_param`` returns an explicit value list
  verbatim, so a repeated value yields two candidates with the same
  ``params_hash`` — an inflated backtest count and two cells sharing one
  identity. Rejected before sizing, naming the parameter.
* **Every expanded candidate validates.** Range endpoints are not enough:
  ``short_window=[2, 2.5, 3]`` has valid endpoints and an interior value
  that fails integer validation, and ``short=[10, 20]`` with ``long=[15, 30]``
  has valid corners and an invalid ``(20, 15)`` combination (review F13).
  Under the admission limit this is bounded work, and an invalid candidate
  refuses the whole grid, naming the parameter and value: a sweep quietly
  narrower than requested is worse than one that will not start.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Grid and
  workload", review amendment F13 and its revision-4 decision.
Canonical implementation: this file.
Validated against: tests/research/sweep/test_validation.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from app.engine.strategy.registry import StrategyRegistration
from app.research.sweep.grid import (
    ParamRange,
    StrategyGridConfig,
    ValueListRange,
    expand_grid,
    grid_size,
)


class GridInvalidError(ValueError):
    """The grid names a value the strategy cannot execute."""

    def __init__(self, message: str, *, parameter: str | None = None, value: object = None) -> None:
        super().__init__(message)
        self.parameter = parameter
        self.value = value


class WorkloadLimitError(ValueError):
    """The grid would launch more backtests than the documented limit admits."""

    def __init__(self, total_backtests: int, limit: int) -> None:
        super().__init__(f"{total_backtests} backtests exceed the limit of {limit}; narrow the grid")
        self.total_backtests = total_backtests
        self.limit = limit


@dataclass(frozen=True)
class ValidatedGrid:
    strategy_key: str
    symbol: str
    combinations: int


def reject_duplicate_values(param_ranges: Mapping[str, ParamRange]) -> None:
    for name, range_spec in param_ranges.items():
        if isinstance(range_spec, ValueListRange) and len(set(range_spec.values)) != len(range_spec.values):
            seen: set[float] = set()
            repeated = next(value for value in range_spec.values if value in seen or seen.add(value))  # type: ignore[func-returns-value]
            raise GridInvalidError(
                f"parameter {name!r} lists the value {repeated!r} more than once; each value may appear once",
                parameter=name,
                value=repeated,
            )


def validate_grid(
    registration: StrategyRegistration,
    *,
    strategy_key: str,
    symbol: str,
    param_ranges: Mapping[str, ParamRange],
    limit: int,
    multiplier: int = 1,
) -> ValidatedGrid:
    """Refuse duplicates, oversized workloads, and any candidate the model rejects.

    ``multiplier`` is how many backtests each combination launches (one for
    a grid search; ``folds x 2`` for a walk-forward study), so the limit is
    on total backtests, not combinations.
    """
    reject_duplicate_values(param_ranges)
    config = StrategyGridConfig(strategy_key=strategy_key, param_ranges=dict(param_ranges))
    combinations = grid_size([config], [symbol])
    if combinations == 0:
        raise GridInvalidError("the grid expands to zero combinations")
    total = combinations * max(1, multiplier)
    if total > limit:
        raise WorkloadLimitError(total, limit)
    for candidate in expand_grid([config], [symbol]):
        try:
            registration.param_schema.model_validate({**candidate.params, "symbol": symbol})
        except ValidationError as exc:
            first = exc.errors()[0]
            location = first.get("loc") or ()
            parameter = str(location[0]) if location else None
            value = candidate.params.get(parameter) if parameter else None
            raise GridInvalidError(
                f"candidate {candidate.params} is not executable: {parameter or 'parameters'}={value!r} — {first.get('msg')}",
                parameter=parameter,
                value=value,
            ) from exc
    return ValidatedGrid(strategy_key=strategy_key, symbol=symbol, combinations=combinations)
