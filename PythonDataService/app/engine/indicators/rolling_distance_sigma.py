"""RollingDistanceSigma — population standard deviation over a rolling window.

Used by the SPY VWAP-reversion strategy on the distance series
``dist = close − vwap`` to size the ``vwap ± K·σ`` bands. Population std
(``ddof=0``) over the trailing ``lookback`` values; ``is_ready`` once the
window is full.

Reference: standard rolling population std. Canonical implementation: this file.
Validated against: tests/engine/indicators/test_vwap_reversion_indicators.py
(independent numpy ``np.std(..., ddof=0)`` computation, atol=1e-9).

``ddof=0`` is pinned to match the QC reference algo (``np.std(arr, ddof=0)``)
so the bands — and therefore the entry signals — reconcile exactly.
"""

from __future__ import annotations

from collections import deque


class RollingDistanceSigma:
    def __init__(self, lookback: int) -> None:
        if lookback <= 0:
            raise ValueError("lookback must be positive")
        self.lookback = lookback
        self._window: deque[float] = deque(maxlen=lookback)
        self._current_value: float | None = None

    @property
    def is_ready(self) -> bool:
        return len(self._window) >= self.lookback

    @property
    def current_value(self) -> float | None:
        return self._current_value

    def update(self, value: float) -> None:
        self._window.append(float(value))
        if len(self._window) >= self.lookback:
            mean = sum(self._window) / len(self._window)
            var = sum((x - mean) ** 2 for x in self._window) / len(self._window)
            self._current_value = var**0.5
        else:
            self._current_value = None

    def reset(self) -> None:
        self._window.clear()
        self._current_value = None
