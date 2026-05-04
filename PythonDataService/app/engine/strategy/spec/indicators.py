"""Map ``IndicatorBlock.kind`` values to the engine's streaming indicators.

The spec layer doesn't reimplement indicator math — it declares which
indicator a spec wants and references it by id. This registry resolves
each block to a concrete instance from ``app.engine.indicators``, which
have their own LEAN-parity tests upstream.

Supported kinds:
  * SMA, EMA — single-price moving averages
  * RSI — Wilders RSI
  * MACD — classical fast/slow/signal MACD line
  * ADX — Wilder ADX (consumes full OHLC bars)
  * SUPERTREND — ATR-band trend follower (consumes full OHLC bars)

Bar-vs-price update dispatch lives in ``evaluator.py`` — the evaluator
checks ``isinstance(indicator, BarIndicator)`` and either passes the
full ``TradeBar`` or just the configured source price.
"""

from __future__ import annotations

from app.engine.indicators.adx import AverageDirectionalIndex
from app.engine.indicators.base import BarIndicator, Indicator
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.indicators.sma import SimpleMovingAverage
from app.engine.indicators.supertrend import Supertrend
from app.engine.strategy.spec.schema import IndicatorBlock


def build_indicator(block: IndicatorBlock) -> Indicator | BarIndicator:
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
        if block.ma_type == "simple":
            raise NotImplementedError(
                "RSI ma_type='simple' is not supported — engine RSI is Wilders-only."
            )
        return RelativeStrengthIndex(name, block.period)
    if block.kind == "MACD":
        # ``period`` is the slow_period; fast/signal default to 12/9.
        fast = block.fast_period if block.fast_period is not None else 12
        signal = block.signal_period if block.signal_period is not None else 9
        if fast >= block.period:
            raise ValueError(
                f"MACD {block.id!r}: fast_period ({fast}) must be < slow_period ({block.period})"
            )
        return MovingAverageConvergenceDivergence(name, fast, block.period, signal)
    if block.kind == "ADX":
        return AverageDirectionalIndex(name, block.period)
    if block.kind == "SUPERTREND":
        # ``period`` is the ATR period; multiplier defaults to 3.0.
        mult = block.multiplier if block.multiplier is not None else 3.0
        return Supertrend(name, atr_period=block.period, multiplier=mult)

    raise NotImplementedError(f"unknown indicator kind: {block.kind!r}")


def is_bar_indicator(indicator: Indicator | BarIndicator) -> bool:
    """Return True iff the indicator consumes full ``TradeBar`` updates."""
    return isinstance(indicator, BarIndicator)
