"""Recency Chart grid expansion.

Formula: the launch grid is the cartesian product of symbols x strategies x
each strategy's per-parameter range, where a range is a single value, an
explicit value-list, or an inclusive low/high/step sequence. Expansion is
lazy (a generator) so a large-but-legitimate sweep never fully materializes
in memory; a cheap size computation runs eagerly before any expansion so a
pathological/malformed grid is rejected immediately rather than after it
starts exhausting the service (D11 — no product cap, but engineering rails).
``params_hash`` is a stable, key-order-independent identity for one
strategy's parameter assignment — the first component of the Recency
Chart's canonical evidence fingerprint (design spec §3, D16).
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design spec
docs/superpowers/specs/2026-08-16-recency-chart-design.md §5.3, D11, D16.
Canonical implementation: this file.
Validated against: tests/research/recency/test_grid.py.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import product

# A very high sanity ceiling, not a product-level cap (D11): real sweeps
# (tens of symbols x a handful of strategies x dozens of combos) sit many
# orders of magnitude below this. It exists only to fail fast on a
# fat-fingered range (e.g. step=0.0001 over a wide span) before the grid
# is expanded and run.
MAX_GRID_SIZE = 2_000_000


class RecencyGridTooLargeError(ValueError):
    """Raised when a configured grid's size exceeds the sanity ceiling."""

    def __init__(self, size: int, limit: int = MAX_GRID_SIZE) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"grid would expand to {size} runs, exceeding the sanity ceiling of {limit}")


@dataclass(frozen=True)
class ValueListRange:
    """An explicit, ordered set of values for one parameter."""

    values: tuple[float, ...]


@dataclass(frozen=True)
class LowHighStepRange:
    """An inclusive low..high sweep in fixed increments of step."""

    low: float
    high: float
    step: float


ParamRange = ValueListRange | LowHighStepRange


@dataclass(frozen=True)
class StrategyGridConfig:
    """One strategy's parameter-range configuration for a launch."""

    strategy_key: str
    param_ranges: Mapping[str, ParamRange]


@dataclass(frozen=True)
class RunSpec:
    """One (symbol, strategy, parameter-combo) cell of the expanded grid."""

    symbol: str
    strategy_key: str
    params: dict[str, float]
    params_hash: str


def expand_param(range_spec: ParamRange) -> list[float]:
    """Expand one parameter's range into its ordered list of values."""
    if isinstance(range_spec, ValueListRange):
        if not range_spec.values:
            raise ValueError("value-list range must not be empty")
        return list(range_spec.values)

    if range_spec.step <= 0:
        raise ValueError("low/high/step range requires step > 0")
    if range_spec.low > range_spec.high:
        raise ValueError("low/high/step range requires low <= high")

    count = round((range_spec.high - range_spec.low) / range_spec.step) + 1
    return [range_spec.low + i * range_spec.step for i in range(count) if range_spec.low + i * range_spec.step <= range_spec.high + 1e-9]


def params_hash(strategy_key: str, params: Mapping[str, float]) -> str:
    """Stable, key-order-independent identity for a strategy's parameter assignment."""
    canonical = json.dumps({"strategy_key": strategy_key, "params": params}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _range_size(range_spec: ParamRange) -> int:
    """Count a range's values analytically — never materializes the list.

    Mirrors expand_param's ``<= high + 1e-9`` filter via a closed-form floor
    (not ``round``, which over-counts a non-divisible span — e.g.
    low=0, high=1, step=0.6 rounds to 3 but expand_param's filter only
    keeps [0, 0.6], since 1.2 > 1 + 1e-9). Never allocating means a
    fat-fingered low/high/step (e.g. step=0.0001 over a wide span) can
    still be rejected by size alone before any memory is spent.
    """
    if isinstance(range_spec, ValueListRange):
        if not range_spec.values:
            raise ValueError("value-list range must not be empty")
        return len(range_spec.values)

    if range_spec.step <= 0:
        raise ValueError("low/high/step range requires step > 0")
    if range_spec.low > range_spec.high:
        raise ValueError("low/high/step range requires low <= high")

    return math.floor((range_spec.high - range_spec.low + 1e-9) / range_spec.step) + 1


def _param_combo_count(param_ranges: Mapping[str, ParamRange]) -> int:
    count = 1
    for range_spec in param_ranges.values():
        count *= _range_size(range_spec)
    return count


def grid_size(strategies: list[StrategyGridConfig], symbols: list[str]) -> int:
    """Count the full launch grid analytically, without expanding it — the
    progress-total callers (jobs.py's estimate, runner.py's ``expected``
    count) need this without materializing every RunSpec."""
    if not strategies or not symbols:
        return 0
    return len(symbols) * sum(_param_combo_count(config.param_ranges) for config in strategies)


def _iter_runs(strategies: list[StrategyGridConfig], symbols: list[str]) -> Iterator[RunSpec]:
    for symbol in symbols:
        for config in strategies:
            param_names = sorted(config.param_ranges)
            value_lists = [expand_param(config.param_ranges[name]) for name in param_names]
            for combo in product(*value_lists):
                params = dict(zip(param_names, combo, strict=True))
                yield RunSpec(
                    symbol=symbol,
                    strategy_key=config.strategy_key,
                    params=params,
                    params_hash=params_hash(config.strategy_key, params),
                )


def expand_grid(strategies: list[StrategyGridConfig], symbols: list[str]) -> Iterator[RunSpec]:
    """Lazily expand the launch grid, validating its size eagerly first.

    Raises :class:`RecencyGridTooLargeError` immediately (before any
    enumeration) when the grid's size exceeds :data:`MAX_GRID_SIZE`.
    """
    size = grid_size(strategies, symbols)
    if size > MAX_GRID_SIZE:
        raise RecencyGridTooLargeError(size)
    return _iter_runs(strategies, symbols)
