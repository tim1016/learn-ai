"""ExponentialMovingAverage — mirrors LEAN's Indicators/ExponentialMovingAverage.cs.

Formula: EMA[n] = input[n] · k + EMA[n-1] · (1 - k), where k = 2 / (1 + period); SMA-seeded warmup.
Reference: references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/ExponentialMovingAverage.cs
Canonical implementation: this file.
Validated against: PythonDataService/tests/test_indicator_parity.py

Critical reproducibility details:
  * Smoothing constant: ``k = 2 / (1 + period)``
  * Seed: the first ``period`` samples are averaged as a simple moving
    average. At exactly ``samples == period``, the EMA value is that SMA.
  * Thereafter: ``EMA[n] = input[n] * k + EMA[n-1] * (1 - k)``
  * ``is_ready`` becomes True when ``samples >= period``.

LEAN internally feeds the warmup samples to an embedded SMA and uses the
SMA's ``Current.Value`` as the initial EMA. We replicate that behavior.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.engine.indicators.base import Indicator
from app.engine.indicators.sma import SimpleMovingAverage


class ExponentialMovingAverage(Indicator):
    def __init__(self, name: str, period: int) -> None:
        super().__init__(name, period)
        # Smoothing factor as Decimal for precision.
        self.k: Decimal = Decimal(2) / Decimal(1 + period)
        self._one_minus_k: Decimal = Decimal(1) - self.k
        self._sma = SimpleMovingAverage(f"{name}_seed_sma", period)

    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        if self.samples <= self.period:
            # Warmup: feed the SMA. Until we reach the period sample, the
            # EMA value should simply be the current SMA (matches LEAN's
            # behavior where Current.Value tracks the SMA during warmup).
            self._sma.update(time, value)
            return self._sma.current_value

        # Post-warmup: standard EMA recursion.
        prev = self._current_value
        assert prev is not None
        return value * self.k + prev * self._one_minus_k

    def _reset_state(self) -> None:
        self._sma.reset()

    def _to_state_extra(self) -> dict:
        # k and one_minus_k are derived from period in __init__;
        # no need to persist them.
        return {
            "sma_state": self._sma.to_state_dict(),
        }

    def _restore_state_extra(self, state: dict) -> None:
        self._sma.restore_state(state["sma_state"])
