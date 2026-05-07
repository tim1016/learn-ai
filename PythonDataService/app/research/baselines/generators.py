"""Spec generators for null-baseline runs.

Each generator returns one or more validated ``StrategySpec`` instances
that the runner can submit through ``run_strategy_spec`` like any
other Phase A run. No engine changes are needed: the spec layer's
existing primitives (``BarProperty``, ``FreshCross``, EMA indicators)
are sufficient for the v1 baseline methods.

**Why generators, not specs-on-disk:** every baseline depends on the
parent's symbol / resolution, and most depend on a sampled parameter,
so the natural shape is "parent + RNG → spec". The generated specs
are not persisted anywhere — they're transient inputs to
``run_strategy_spec``, which records them inside the resulting
``RunLedger.strategy_spec_json``.

The ``BaselineMethod`` literal is the discriminator the HTTP layer
forwards as the request body's ``method`` field.
"""

from __future__ import annotations

from typing import Literal

import numpy as np

from app.engine.strategy.spec import StrategySpec
from app.research.runs import RunLedger

BaselineMethod = Literal["buy_and_hold", "random_ema_windows"]


# ---------------------------------------------------------------------------
# Buy and hold — single trade, hold for the entire window.
# ---------------------------------------------------------------------------
def buy_and_hold_spec(parent: RunLedger) -> StrategySpec:
    """Build a single-trade buy-and-hold spec on the parent's symbol.

    Implementation trick: the spec layer doesn't have an "always true"
    primitive, but ``BarProperty: range >= 0`` is *tautologically*
    true (``high >= low`` is an OHLC invariant the engine validates
    at ingestion). Combined with a never-firing exit
    (``BarsSinceEntry >= 999_999``), the strategy enters on bar 1
    and holds through end-of-algorithm flush — a single trade.

    Why not modify the spec to add an "Always" primitive: that's a
    schema change requiring a new ``schema_version`` bump and a
    spec-evaluator code path. The tautology gives the same behaviour
    without touching the contract; if a future spec needs an explicit
    ``Always`` primitive for clarity, it can land separately.

    **Known limitation:** the engine's ``on_end_of_algorithm`` flush
    submits a pending liquidate order that the main bar loop has
    already exited, so the closing fill is never drained. The
    position is correctly tracked in equity (``RunMetrics.total_return_pct``
    and ``max_drawdown_pct`` are computed from the real equity
    curve), but ``RunLedger.trade_log`` ends up empty for
    buy-and-hold. ``RunMetrics.total_trades = 0`` and
    ``exposure_pct = 0.0`` are artefacts of this — null-distribution
    aggregation looks at the equity-curve-derived metrics, which
    are correct, so the baseline is still useful. Fixing the engine
    flush is tracked as a follow-up; not blocking for null-baseline
    research.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": f"baseline:buy_and_hold:{parent.symbol}",
            "description": (
                f"Buy-and-hold null baseline derived from run {parent.run_id}. "
                f"Single trade on the entire window."
            ),
            "symbols": [parent.symbol],
            "resolution": {"period_minutes": parent.resolution_minutes},
            "indicators": [],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {
                        "kind": "BarProperty",
                        "property": "range",
                        "op": ">=",
                        "value": 0.0,
                    },
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {"kind": "BarsSinceEntry", "op": ">=", "value": 999_999},
                ],
            },
            "diagnostics": {},
        }
    )


# ---------------------------------------------------------------------------
# Random EMA window pairs — sample (fast, slow) from a bounded family.
# ---------------------------------------------------------------------------
def random_ema_window_specs(
    parent: RunLedger,
    *,
    count: int,
    fast_range: tuple[int, int] = (3, 12),
    slow_range: tuple[int, int] = (10, 30),
    rng: np.random.Generator,
) -> list[tuple[StrategySpec, dict]]:
    """Sample ``count`` random ``(fast, slow)`` EMA pairs and build a
    SPY-EMA-style spec for each.

    Returns ``(spec, parameters)`` pairs so the runner can record the
    sampled parameters on each ``BaselineRunRecord`` (e.g.,
    ``{"fast": 5, "slow": 12}``). Pairs satisfy ``slow > fast`` —
    invalid combinations are silently re-sampled rather than raising.
    The architecture spec calls this out as the ``EMA window family``
    null baseline.

    The generated specs use the same shape as the canonical SPY EMA
    fixture (``FreshCross`` + a ``>= 0.20`` gap + ``RSI(14)`` band +
    five-bar hold) but with the fast/slow periods swapped in. The RSI
    gate is fixed at the canonical ``50..70`` band — only the EMA
    windows vary, which is the architecture spec's "EMA window family"
    semantics. Tests "is the parent's EMA(5,10) choice better than a
    random pair from the same family on the same data?".
    """
    if count <= 0:
        raise ValueError(f"count must be positive (got {count})")
    fast_lo, fast_hi = fast_range
    slow_lo, slow_hi = slow_range
    if fast_lo < 2 or slow_lo < 2:
        raise ValueError("EMA periods must be >= 2 (matches schema constraint)")
    if fast_hi < fast_lo or slow_hi < slow_lo:
        raise ValueError(
            f"range upper bounds must be >= lower bounds "
            f"(fast={fast_range}, slow={slow_range})"
        )
    # Reject parameter spaces that can't satisfy slow > fast.
    if slow_hi <= fast_lo:
        raise ValueError(
            f"parameter family admits no valid (fast, slow) pair: slow_hi="
            f"{slow_hi} <= fast_lo={fast_lo}"
        )

    out: list[tuple[StrategySpec, dict]] = []
    # Cap re-samples to avoid pathological loops on narrow ranges that
    # mostly draw invalid pairs. ``count * 20`` is generous.
    attempts = 0
    max_attempts = count * 20
    while len(out) < count and attempts < max_attempts:
        attempts += 1
        fast = int(rng.integers(low=fast_lo, high=fast_hi + 1))
        slow = int(rng.integers(low=slow_lo, high=slow_hi + 1))
        if slow <= fast:
            continue
        spec = StrategySpec.model_validate(
            {
                "schema_version": "1.0",
                "name": f"baseline:random_ema:{fast}_{slow}:{parent.symbol}",
                "symbols": [parent.symbol],
                "resolution": {"period_minutes": parent.resolution_minutes},
                "indicators": [
                    {"id": "fast", "kind": "EMA", "period": fast, "source": "close"},
                    {"id": "slow", "kind": "EMA", "period": slow, "source": "close"},
                    {
                        "id": "rsi",
                        "kind": "RSI",
                        "period": 14,
                        "source": "close",
                        "ma_type": "wilders",
                    },
                ],
                "entry": {
                    "logic": "AND",
                    "conditions": [
                        {
                            "kind": "FreshCross",
                            "left": "fast",
                            "right": "slow",
                            "direction": "up",
                        },
                        {
                            "kind": "IndicatorComparison",
                            "left": {
                                "kind": "Subtract",
                                "left": {"kind": "IndicatorRef", "indicator": "fast"},
                                "right": {"kind": "IndicatorRef", "indicator": "slow"},
                            },
                            "op": ">=",
                            "right": {"kind": "Const", "value": 0.20},
                        },
                        {
                            "kind": "IndicatorBetween",
                            "indicator": "rsi",
                            "lo": 50,
                            "hi": 70,
                            "inclusive": True,
                        },
                    ],
                    "size": {"kind": "SetHoldings", "fraction": 1.0},
                    "pyramiding": 1,
                },
                "position": {"kind": "EQUITY_LONG"},
                "survival": [],
                "exit": {
                    "logic": "OR",
                    "conditions": [
                        {"kind": "BarsSinceEntry", "op": ">=", "value": 5},
                    ],
                },
                "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
            }
        )
        out.append((spec, {"fast": fast, "slow": slow}))

    if len(out) < count:
        raise ValueError(
            f"could not sample {count} valid (fast, slow) pairs from "
            f"fast={fast_range} slow={slow_range} after {attempts} attempts"
        )
    return out
