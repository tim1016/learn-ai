"""MovingAverageConvergenceDivergence — classical MACD.

Formula: MACD = EMA(fast) - EMA(slow); Signal = EMA(signal_period) of MACD; Histogram = MACD - Signal.
Reference: references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/MovingAverageConvergenceDivergence.cs; Pine Script ta.macd().
Canonical implementation: this file (streaming variant for engine). Note: the registry's MACD row points at app/services/ta_service.py (pandas-ta one-shot variant) for non-engine callers; this streaming implementation is the engine-internal canonical and has no concept-row of its own.
Validated against: PythonDataService/app/engine/tests/test_macd.py

Mirrors LEAN's ``Indicators/MovingAverageConvergenceDivergence.cs`` and
Pine Script's ``ta.macd()`` when both use simple-moving-average-seeded
EMAs with ``adjust=False`` semantics.

Reproducibility details:
  * ``fast_ema`` and ``slow_ema`` are standard
    :class:`ExponentialMovingAverage` indicators (SMA-seeded, then the
    classical ``value * k + prev * (1 - k)`` recursion with
    ``k = 2 / (1 + period)``).
  * MACD line = ``fast_ema.current - slow_ema.current``. Emitted only
    once both EMAs are ready (``samples >= slow_period``).
  * Signal line = EMA(``signal_period``) of the MACD line. The signal
    EMA starts receiving samples at the first bar the MACD line is
    defined.
  * Histogram = MACD line - signal line.
  * ``current_value`` is the **MACD line** (matching LEAN's
    ``macd.Current.Value``).
  * ``is_ready`` when the signal line is ready (i.e.
    ``samples >= slow_period + signal_period - 1`` with default
    parameters 12/26/9 this is sample 34).

Decimal arithmetic throughout via the embedded EMAs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.engine.indicators.base import Indicator
from app.engine.indicators.ema import ExponentialMovingAverage


class MovingAverageConvergenceDivergence(Indicator):
    def __init__(
        self,
        name: str,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> None:
        # The outer ``period`` is just for bookkeeping; ``is_ready`` is
        # delegated to the signal EMA below.
        super().__init__(name, slow_period + signal_period - 1)
        if fast_period >= slow_period:
            raise ValueError(f"fast_period ({fast_period}) must be < slow_period ({slow_period})")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period

        self._fast = ExponentialMovingAverage(f"{name}_fast", fast_period)
        self._slow = ExponentialMovingAverage(f"{name}_slow", slow_period)
        self._signal = ExponentialMovingAverage(f"{name}_signal", signal_period)

        self._macd_line: Decimal | None = None
        self._signal_line: Decimal | None = None
        self._histogram: Decimal | None = None

    @property
    def is_ready(self) -> bool:
        return self._signal.is_ready

    @property
    def macd(self) -> Decimal | None:
        return self._macd_line

    @property
    def signal(self) -> Decimal | None:
        return self._signal_line

    @property
    def histogram(self) -> Decimal | None:
        return self._histogram

    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        self._fast.update(time, value)
        self._slow.update(time, value)

        if not (self._fast.is_ready and self._slow.is_ready):
            return None

        assert self._fast.current_value is not None
        assert self._slow.current_value is not None
        self._macd_line = self._fast.current_value - self._slow.current_value

        # Feed the signal EMA with each MACD-line value.
        self._signal.update(time, self._macd_line)

        if self._signal.is_ready:
            assert self._signal.current_value is not None
            self._signal_line = self._signal.current_value
            self._histogram = self._macd_line - self._signal_line

        return self._macd_line

    def _reset_state(self) -> None:
        self._fast.reset()
        self._slow.reset()
        self._signal.reset()
        self._macd_line = None
        self._signal_line = None
        self._histogram = None
