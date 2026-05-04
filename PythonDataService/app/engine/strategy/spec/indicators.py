"""Map ``IndicatorBlock.kind`` values to the engine's streaming indicators.

The spec layer doesn't reimplement indicator math — it declares which
indicator a spec wants and references it by id. This registry resolves
each block to a concrete instance from ``app.engine.indicators``, which
have their own LEAN-parity tests upstream.

Phase 1 ships SMA, EMA, RSI; ADX, MACD, SUPERTREND are reserved kinds
(declared in the schema for forward-compatibility) but raise on
instantiation here so a Phase-1 spec that uses one fails fast at
``initialize`` time rather than producing silently wrong results.
"""

from __future__ import annotations

from app.engine.indicators.base import Indicator
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.indicators.sma import SimpleMovingAverage
from app.engine.strategy.spec.schema import IndicatorBlock


def build_indicator(block: IndicatorBlock) -> Indicator:
    """Construct an engine indicator from a validated spec block.

    Naming convention: ``{KIND}{period}_{id}`` so log lines and engine
    diagnostics distinguish two RSI(14) instances declared with different
    spec ids. Names don't affect math, only logs.
    """
    name = f"{block.kind}{block.period}_{block.id}"

    if block.kind == "SMA":
        return SimpleMovingAverage(name, block.period)
    if block.kind == "EMA":
        return ExponentialMovingAverage(name, block.period)
    if block.kind == "RSI":
        # RSI is Wilders-only in the engine. The spec's ma_type field exists
        # for explicitness; reject "simple" so a future Wilders/Simple split
        # can be introduced without silently downgrading existing specs.
        if block.ma_type == "simple":
            raise NotImplementedError(
                "RSI ma_type='simple' is not supported in Phase 1 — engine RSI is Wilders-only."
            )
        return RelativeStrengthIndex(name, block.period)

    # ADX, MACD, SUPERTREND: schema admits the kind, evaluator does not.
    raise NotImplementedError(
        f"indicator kind {block.kind!r} is reserved for Phase 2 and is not supported by the Phase 1 evaluator"
    )


def get_indicator_value(indicator: Indicator) -> object:
    """Return the indicator's current value, or None if not ready.

    Thin wrapper for the evaluator so primitives don't need to import
    the indicator base class directly.
    """
    if not indicator.is_ready:
        return None
    return indicator.current_value
