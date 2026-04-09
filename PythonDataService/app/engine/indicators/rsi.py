"""RelativeStrengthIndex with Wilders smoothing.

Mirrors LEAN's Indicators/RelativeStrengthIndex.cs with
``MovingAverageType.Wilders``.

Critical reproducibility details:
  * Per-step gain: ``max(0, input - prev_input)`` when ``input >= prev_input``,
    else 0. (Equality is a gain of 0, not a loss.)
  * Per-step loss: ``max(0, prev_input - input)`` when ``input < prev_input``,
    else 0.
  * Wilders smoothing for both averages:
        avg_new = (avg_old * (period - 1) + sample_new) / period
    with initial averages = SMA of the first ``period`` gain/loss samples.
  * Warmup: ``is_ready`` when ``samples >= period + 1`` (one extra sample
    for the first delta). The initial averages become available at exactly
    ``samples == period + 1``.
  * Edge case: if ``round(avg_loss, 10) == 0``, RSI = 100.
  * Formula: ``RS = avg_gain / avg_loss``; ``RSI = 100 - 100 / (1 + RS)``.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.engine.indicators.base import Indicator


class RelativeStrengthIndex(Indicator):
    def __init__(self, name: str, period: int) -> None:
        # RSI requires one extra sample before it can produce a value.
        super().__init__(name, period)
        self._period_dec = Decimal(period)
        self._period_minus_1 = Decimal(period - 1)
        self._prev_input: Optional[Decimal] = None
        self._avg_gain: Optional[Decimal] = None
        self._avg_loss: Optional[Decimal] = None
        # Initial accumulators for the warmup window.
        self._gain_sum: Decimal = Decimal(0)
        self._loss_sum: Decimal = Decimal(0)
        self._delta_samples: int = 0

    @property
    def is_ready(self) -> bool:
        # RSI(period) needs period+1 samples (for period deltas).
        return self.samples >= self.period + 1

    def _compute_next_value(
        self, time: datetime, value: Decimal
    ) -> Optional[Decimal]:
        prev = self._prev_input
        self._prev_input = value
        if prev is None:
            # First sample — no delta yet.
            return None

        if value >= prev:
            gain = value - prev
            loss = Decimal(0)
        else:
            gain = Decimal(0)
            loss = prev - value

        self._delta_samples += 1

        if self._delta_samples < self.period:
            # Accumulate for the initial SMA.
            self._gain_sum += gain
            self._loss_sum += loss
            return None
        elif self._delta_samples == self.period:
            # Final accumulator — seed the Wilders averages.
            self._gain_sum += gain
            self._loss_sum += loss
            self._avg_gain = self._gain_sum / self._period_dec
            self._avg_loss = self._loss_sum / self._period_dec
        else:
            # Wilders smoothing:
            #   avg_new = (avg_old * (period - 1) + sample) / period
            assert self._avg_gain is not None and self._avg_loss is not None
            self._avg_gain = (
                self._avg_gain * self._period_minus_1 + gain
            ) / self._period_dec
            self._avg_loss = (
                self._avg_loss * self._period_minus_1 + loss
            ) / self._period_dec

        # Compute RSI from the current averages.
        assert self._avg_gain is not None and self._avg_loss is not None
        # LEAN's edge case: if rounded-to-10 avg_loss is zero, RSI is 100.
        if round(self._avg_loss, 10) == Decimal(0):
            return Decimal(100)
        rs = self._avg_gain / self._avg_loss
        return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))

    def _reset_state(self) -> None:
        self._prev_input = None
        self._avg_gain = None
        self._avg_loss = None
        self._gain_sum = Decimal(0)
        self._loss_sum = Decimal(0)
        self._delta_samples = 0
