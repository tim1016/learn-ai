"""SimpleMovingAverage — used internally to seed the EMA.

Formula: SMA(n) = (1/n) · Σ x_{t-i}, i ∈ [0, n-1]
Reference: references/lean/7986ed0aade3ae5de06121682409f05984e32ff7/Indicators/SimpleMovingAverage.cs
Canonical implementation: this file.
Validated against: PythonDataService/tests/test_indicator_parity.py

Mirrors LEAN's Indicators/SimpleMovingAverage.cs. The indicator becomes
``is_ready`` once ``period`` samples have been received, at which point
``current_value`` equals the arithmetic mean of the most recent ``period``
inputs. Before that, ``current_value`` is the mean of all samples seen so far
(this matches LEAN, where the SMA uses a rolling window that also reports a
value during warmup).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from decimal import Decimal

from app.engine.indicators.base import Indicator


class SimpleMovingAverage(Indicator):
    def __init__(self, name: str, period: int) -> None:
        super().__init__(name, period)
        self._window: deque[Decimal] = deque(maxlen=period)
        self._sum: Decimal = Decimal(0)

    def _compute_next_value(self, time: datetime, value: Decimal) -> Decimal | None:
        if len(self._window) == self.period:
            # Maxlen is already at period, popping is handled by deque,
            # but we track _sum manually for precision.
            self._sum -= self._window[0]
        self._window.append(value)
        self._sum += value
        return self._sum / Decimal(len(self._window))

    def _reset_state(self) -> None:
        self._window.clear()
        self._sum = Decimal(0)

    def _to_state_extra(self) -> dict:
        return {
            "window": [str(v) for v in self._window],
            "sum": str(self._sum),
        }

    def _restore_state_extra(self, state: dict) -> None:
        raw = state["window"]
        if len(raw) > self.period:
            raise ValueError(f"restore_state: window length {len(raw)} exceeds period {self.period}")
        self._window = deque(
            (Decimal(v) for v in raw),
            maxlen=self.period,
        )
        self._sum = Decimal(state["sum"])
