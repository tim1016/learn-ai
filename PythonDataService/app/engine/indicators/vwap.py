"""SessionAnchoredVwap — intraday volume-weighted average price, reset per session.

Formula: VWAP = Σ(typical·volume) / Σ(volume), accumulated from the session
open, where typical = (high + low + close) / 3. Resets to empty at each new
session date (the caller passes bar timestamps; a date change resets the
accumulators).

Reference: standard intraday VWAP definition (e.g. QuantConnect IntradayVwap,
typical-price formulation). Canonical implementation: this file.
Validated against: tests/engine/indicators/test_vwap_reversion_indicators.py
(independent numpy cumulative computation, atol=1e-9).

float64 throughout — matches the QC reference algo's float arithmetic so the
SPY VWAP-reversion port reconciles trade-by-trade.
"""

from __future__ import annotations

from datetime import datetime


class SessionAnchoredVwap:
    def __init__(self) -> None:
        self._cum_pv: float = 0.0
        self._cum_vol: float = 0.0
        self._session_date = None
        self._current_value: float | None = None

    @property
    def current_value(self) -> float | None:
        return self._current_value

    @property
    def is_ready(self) -> bool:
        return self._current_value is not None

    def update(self, time: datetime, *, high: float, low: float, close: float, volume: float) -> None:
        """Accumulate one bar. Resets the session on a new calendar date."""
        d = time.date()
        if self._session_date != d:
            self._session_date = d
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._current_value = None
        typical = (float(high) + float(low) + float(close)) / 3.0
        self._cum_pv += typical * float(volume)
        self._cum_vol += float(volume)
        if self._cum_vol > 0.0:
            self._current_value = self._cum_pv / self._cum_vol

    def reset(self) -> None:
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._session_date = None
        self._current_value = None
