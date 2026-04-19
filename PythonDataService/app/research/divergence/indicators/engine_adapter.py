"""Batch-driver for learn-ai's streaming indicators.

The engine's :class:`Indicator` base class (``app.engine.indicators.base``)
was designed for streaming use: ``update(time, value)`` is called once
per bar and the current value is read via ``.current_value``. This
module drives the same indicators in a tight loop over a DataFrame so
their output can be diffed against native/TV indicator values at the
bar level.

Only three indicators are implemented by the engine today: EMA, SMA,
RSI. For the others (MACD, BB, ADX, ATR, SuperTrend) the divergence
report compares Native-vs-TV only and notes that the engine doesn't
have a dedicated implementation.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.indicators.sma import SimpleMovingAverage


def _drive_indicator(
    indicator,  # type: ignore[no-untyped-def]
    times: pd.Series,
    values: pd.Series,
) -> np.ndarray:
    """Feed every (time, value) pair to ``indicator`` and collect outputs."""
    n = len(values)
    out = np.full(n, np.nan, dtype=float)
    # Convert to Python-native types once to avoid per-iteration overhead.
    times_list: list[datetime] = times.tolist()
    values_list: list[float] = values.astype(float).tolist()
    for i in range(n):
        indicator.update(times_list[i], Decimal(str(values_list[i])))
        cv = indicator.current_value
        if cv is not None:
            out[i] = float(cv)
    return out


def compute_engine_ema_batch(
    df: pd.DataFrame,
    length: int,
    time_col: str = "time_utc",
    value_col: str = "close",
) -> np.ndarray:
    ind = ExponentialMovingAverage(f"EMA{length}", length)
    return _drive_indicator(ind, df[time_col], df[value_col])


def compute_engine_sma_batch(
    df: pd.DataFrame,
    length: int,
    time_col: str = "time_utc",
    value_col: str = "close",
) -> np.ndarray:
    ind = SimpleMovingAverage(f"SMA{length}", length)
    return _drive_indicator(ind, df[time_col], df[value_col])


def compute_engine_rsi_batch(
    df: pd.DataFrame,
    length: int = 14,
    time_col: str = "time_utc",
    value_col: str = "close",
) -> np.ndarray:
    ind = RelativeStrengthIndex(f"RSI{length}", length)
    return _drive_indicator(ind, df[time_col], df[value_col])
