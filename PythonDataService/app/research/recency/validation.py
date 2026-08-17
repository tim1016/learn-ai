"""Recency Chart launch-request validation.

Runs at job-dispatch time (``POST /jobs-internal/recency-chart``), before
the launch row is written and before a single child backtest runs.
Without this, only range arithmetic (grid size) was checked before
returning 202 — an unknown/misspelled strategy, an out-of-bounds
parameter, or an unsupported data policy would leave a durable launch
behind after every child backtest failed identically. Only the range
*endpoints* of each parameter are validated (not every expanded value):
Pydantic constraints (``ge``/``le``) are monotonic over a numeric range,
so a value strictly inside ``[low, high]`` can never violate a bound its
endpoints satisfy — this stays O(strategies) instead of O(grid size).
Reference: PRD https://github.com/tim1016/learn-ai/issues/1577; design
spec docs/superpowers/specs/2026-08-16-recency-chart-design.md.
Canonical implementation: this file.
Validated against: tests/research/recency/test_validation.py.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.recency.eligibility import is_recency_supported
from app.research.recency.grid import ParamRange, StrategyGridConfig, ValueListRange

# The only DataPolicy this launch path can actually honor today: recency
# never threads a caller-supplied policy through to the engine — it relies
# on EngineBacktestRequest's own synthesizer (params.symbol present ->
# source=polygon, adjusted=true, session=regular, minute bars), which is
# exactly what this label describes. A caller asking for anything else
# would silently get this default anyway (see runner.py's fingerprint,
# which now reads the *resolved* policy back off the engine response
# rather than trusting this label) — reject the mismatch here instead of
# letting it clear the boundary.
SUPPORTED_DATA_POLICY_LABELS = frozenset({"polygon-adjusted-regular-minute"})


class RecencyRequestInvalidError(ValueError):
    """Raised when a launch request fails validation before dispatch."""


def _bounds(range_spec: ParamRange) -> tuple[float, float]:
    if isinstance(range_spec, ValueListRange):
        return min(range_spec.values), max(range_spec.values)
    return range_spec.low, range_spec.high


def validate_recency_request(
    strategies: list[StrategyGridConfig],
    symbols: list[str],
    window_start_ms: int,
    window_end_ms: int,
    data_policy: str,
) -> None:
    """Raise :class:`RecencyRequestInvalidError` on any structurally-invalid launch."""
    if not symbols or any(not symbol.strip() for symbol in symbols):
        raise RecencyRequestInvalidError("symbols must be a non-empty list of non-blank tickers")
    if window_start_ms >= window_end_ms:
        raise RecencyRequestInvalidError("window_start_ms must be before window_end_ms")
    if data_policy not in SUPPORTED_DATA_POLICY_LABELS:
        raise RecencyRequestInvalidError(
            f"unsupported data_policy '{data_policy}'; supported: {sorted(SUPPORTED_DATA_POLICY_LABELS)}"
        )

    sample_symbol = symbols[0]
    for strategy in strategies:
        registration = _STRATEGY_REGISTRY.get(strategy.strategy_key)
        if registration is None:
            raise RecencyRequestInvalidError(f"unknown strategy_key '{strategy.strategy_key}'")
        if not is_recency_supported(registration.param_schema):
            raise RecencyRequestInvalidError(f"strategy '{strategy.strategy_key}' is not recency-supported")

        low_candidate = {name: _bounds(r)[0] for name, r in strategy.param_ranges.items()}
        high_candidate = {name: _bounds(r)[1] for name, r in strategy.param_ranges.items()}
        for candidate in (low_candidate, high_candidate):
            try:
                registration.param_schema.model_validate({**candidate, "symbol": sample_symbol})
            except ValidationError as exc:
                raise RecencyRequestInvalidError(
                    f"strategy '{strategy.strategy_key}' parameters invalid: {exc.errors()}"
                ) from exc
