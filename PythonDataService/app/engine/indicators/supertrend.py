"""Supertrend — ATR-based trailing-stop indicator.

Formula: hl2 = (H+L)/2; basic upper = hl2 + multiplier·ATR; basic lower = hl2 - multiplier·ATR; band-clamp on direction-preserved bars; direction flips on close crossing prior band.
Reference: Pine Script ta.supertrend(factor, atrPeriod); pandas-ta ta.supertrend(); no LEAN port (Pine is the authority for this indicator's classical form).
Canonical implementation: this file (streaming Decimal variant for engine).
Validated against: PythonDataService/app/engine/tests/test_supertrend.py; golden fixture at app/engine/tests/fixtures/golden/supertrend_10_3/

Mirrors the classical Pine Script ``ta.supertrend(factor, atrPeriod)``
and pandas-ta's ``ta.supertrend(high, low, close, length, multiplier)``.

Reproducibility details:
  * True Range (TR) uses ``high - low`` on the first bar (no prev close);
    subsequent bars use the classic max of ``H-L``, ``|H - prev_close|``,
    ``|L - prev_close|``.
  * Average True Range (ATR) uses Wilder's average-form smoothing:
        initial (samples == atr_period): mean of first atr_period TR values
        ongoing: ATR_new = (ATR_old * (period - 1) + TR_new) / period
  * Basic bands (``hl2 = (high + low) / 2``):
        upper = hl2 + multiplier * ATR
        lower = hl2 - multiplier * ATR
  * Direction flips per pandas-ta's convention:
        close > prev_upper → direction = +1 (uptrend, bullish)
        close < prev_lower → direction = -1 (downtrend, bearish)
        else              → direction preserved, AND band clamp:
            uptrend   : lower never retraces down  (lower = max(basic, prev))
            downtrend : upper never retraces up    (upper = min(basic, prev))
  * Direction is initialised to +1 (uptrend) on the first bar the ATR
    becomes defined.
  * ``current_value`` is the supertrend line itself:
        uptrend   → lower band
        downtrend → upper band
  * ``is_long`` exposes the direction as a bool (True = uptrend / bullish).
  * Warmup: ``is_ready`` when ``samples >= atr_period`` (default 10).

Decimal arithmetic throughout — no float drift in the ATR recursion or
the band clamps.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.base import BarIndicator


class Supertrend(BarIndicator):
    def __init__(
        self,
        name: str,
        atr_period: int = 10,
        multiplier: Decimal | float | int = 3,
    ) -> None:
        super().__init__(name, atr_period)
        self.atr_period = atr_period
        self.multiplier = multiplier if isinstance(multiplier, Decimal) else Decimal(str(multiplier))
        self._period_dec = Decimal(atr_period)
        self._period_minus_1 = Decimal(atr_period - 1)

        self._prev_close: Decimal | None = None

        self._tr_sum: Decimal = Decimal(0)
        self._tr_samples: int = 0
        self._atr: Decimal | None = None

        self._prev_upper_band: Decimal | None = None
        self._prev_lower_band: Decimal | None = None
        self._direction: int = 1

        self._upper_band: Decimal | None = None
        self._lower_band: Decimal | None = None

    @property
    def is_long(self) -> bool | None:
        if self._current_value is None:
            return None
        return self._direction == 1

    @property
    def direction(self) -> int | None:
        if self._current_value is None:
            return None
        return self._direction

    @property
    def atr(self) -> Decimal | None:
        return self._atr

    @property
    def upper_band(self) -> Decimal | None:
        return self._upper_band

    @property
    def lower_band(self) -> Decimal | None:
        return self._lower_band

    def _compute_next_value(self, bar: TradeBar) -> Decimal | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close

        self._tr_samples += 1
        if self._tr_samples < self.atr_period:
            self._tr_sum += tr
            return None
        elif self._tr_samples == self.atr_period:
            self._tr_sum += tr
            self._atr = self._tr_sum / self._period_dec
        else:
            assert self._atr is not None
            self._atr = (self._atr * self._period_minus_1 + tr) / self._period_dec

        assert self._atr is not None

        hl2 = (bar.high + bar.low) / Decimal(2)
        matr = self.multiplier * self._atr
        basic_upper = hl2 + matr
        basic_lower = hl2 - matr

        if self._prev_upper_band is None:
            self._upper_band = basic_upper
            self._lower_band = basic_lower
            self._direction = 1
            self._prev_upper_band = basic_upper
            self._prev_lower_band = basic_lower
            return basic_lower

        prev_upper = self._prev_upper_band
        prev_lower = self._prev_lower_band
        assert prev_lower is not None

        if bar.close > prev_upper:
            new_direction = 1
            upper = basic_upper
            lower = basic_lower
        elif bar.close < prev_lower:
            new_direction = -1
            upper = basic_upper
            lower = basic_lower
        else:
            new_direction = self._direction
            upper = basic_upper
            lower = basic_lower
            if new_direction == 1 and lower < prev_lower:
                lower = prev_lower
            if new_direction == -1 and upper > prev_upper:
                upper = prev_upper

        self._direction = new_direction
        self._upper_band = upper
        self._lower_band = lower
        self._prev_upper_band = upper
        self._prev_lower_band = lower

        return lower if new_direction == 1 else upper

    def _reset_state(self) -> None:
        self._prev_close = None
        self._tr_sum = Decimal(0)
        self._tr_samples = 0
        self._atr = None
        self._prev_upper_band = None
        self._prev_lower_band = None
        self._direction = 1
        self._upper_band = None
        self._lower_band = None
