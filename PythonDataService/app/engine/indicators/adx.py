"""AverageDirectionalIndex — Wilder's ADX with +DI / -DI.

Mirrors LEAN's Indicators/AverageDirectionalIndex.cs and the definition
in Wilder, J. Welles, *New Concepts in Technical Trading Systems* (1978).
Pine Script's ``ta.dmi()`` uses the same spec.

Reproducibility details:
  * Directional movement (requires a prior bar):
        up_move   = high - prev_high
        down_move = prev_low - low
        +DM = up_move   if up_move > down_move  and up_move  > 0 else 0
        -DM = down_move if down_move > up_move  and down_move > 0 else 0
  * True range:
        TR = max(high - low, |high - prev_close|, |low - prev_close|)
  * Wilder smoothing (sum form) of +DM, -DM, TR:
        initial  (at dm_samples == period): sum of the first `period` values
        ongoing: smooth_new = smooth_old - smooth_old/period + current
  * Directional indicators (percent):
        +DI = 100 * smoothed_+DM / smoothed_TR
        -DI = 100 * smoothed_-DM / smoothed_TR
    Both zero if smoothed_TR == 0 (silent bar, no movement).
  * Directional index:
        DX = 100 * |+DI - -DI| / (+DI + -DI),  0 if sum is 0
  * ADX is the Wilder average of DX (average form):
        first ADX (at dx_samples == period): mean of the first `period` DX
        ongoing: ADX_new = (ADX_old * (period - 1) + DX_new) / period
  * Warmup: ``is_ready`` when ``samples >= 2 * period``. For period=14
    the first ADX is emitted at sample 28.

Decimal arithmetic throughout — no float drift across the recursive
smoothing.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.base import BarIndicator


class AverageDirectionalIndex(BarIndicator):
    def __init__(self, name: str, period: int) -> None:
        super().__init__(name, period)
        self._period_dec = Decimal(period)
        self._period_minus_1 = Decimal(period - 1)

        self._prev_high: Decimal | None = None
        self._prev_low: Decimal | None = None
        self._prev_close: Decimal | None = None

        self._plus_dm_sum: Decimal = Decimal(0)
        self._minus_dm_sum: Decimal = Decimal(0)
        self._tr_sum: Decimal = Decimal(0)
        self._dm_samples: int = 0

        self._smoothed_plus_dm: Decimal | None = None
        self._smoothed_minus_dm: Decimal | None = None
        self._smoothed_tr: Decimal | None = None

        self._dx_sum: Decimal = Decimal(0)
        self._dx_samples: int = 0
        self._adx: Decimal | None = None

        self._plus_di: Decimal | None = None
        self._minus_di: Decimal | None = None

    @property
    def is_ready(self) -> bool:
        return self.samples >= 2 * self.period

    @property
    def plus_di(self) -> Decimal | None:
        return self._plus_di

    @property
    def minus_di(self) -> Decimal | None:
        return self._minus_di

    def _compute_next_value(self, bar: TradeBar) -> Decimal | None:
        prev_high = self._prev_high
        prev_low = self._prev_low
        prev_close = self._prev_close

        self._prev_high = bar.high
        self._prev_low = bar.low
        self._prev_close = bar.close

        if prev_high is None:
            return None

        up_move = bar.high - prev_high
        down_move = prev_low - bar.low

        zero = Decimal(0)
        plus_dm = up_move if up_move > down_move and up_move > zero else zero
        minus_dm = down_move if down_move > up_move and down_move > zero else zero

        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))

        self._dm_samples += 1

        if self._dm_samples < self.period:
            self._plus_dm_sum += plus_dm
            self._minus_dm_sum += minus_dm
            self._tr_sum += tr
            return None
        elif self._dm_samples == self.period:
            self._plus_dm_sum += plus_dm
            self._minus_dm_sum += minus_dm
            self._tr_sum += tr
            self._smoothed_plus_dm = self._plus_dm_sum
            self._smoothed_minus_dm = self._minus_dm_sum
            self._smoothed_tr = self._tr_sum
        else:
            assert self._smoothed_plus_dm is not None
            assert self._smoothed_minus_dm is not None
            assert self._smoothed_tr is not None
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / self._period_dec) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / self._period_dec) + minus_dm
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / self._period_dec) + tr

        assert self._smoothed_tr is not None
        assert self._smoothed_plus_dm is not None
        assert self._smoothed_minus_dm is not None

        if self._smoothed_tr == zero:
            self._plus_di = zero
            self._minus_di = zero
        else:
            self._plus_di = Decimal(100) * self._smoothed_plus_dm / self._smoothed_tr
            self._minus_di = Decimal(100) * self._smoothed_minus_dm / self._smoothed_tr

        di_sum = self._plus_di + self._minus_di
        if di_sum == zero:
            dx = zero
        else:
            dx = Decimal(100) * abs(self._plus_di - self._minus_di) / di_sum

        self._dx_samples += 1
        if self._dx_samples < self.period:
            self._dx_sum += dx
            return None
        elif self._dx_samples == self.period:
            self._dx_sum += dx
            self._adx = self._dx_sum / self._period_dec
        else:
            assert self._adx is not None
            self._adx = (self._adx * self._period_minus_1 + dx) / self._period_dec

        return self._adx

    def _reset_state(self) -> None:
        self._prev_high = None
        self._prev_low = None
        self._prev_close = None
        self._plus_dm_sum = Decimal(0)
        self._minus_dm_sum = Decimal(0)
        self._tr_sum = Decimal(0)
        self._dm_samples = 0
        self._smoothed_plus_dm = None
        self._smoothed_minus_dm = None
        self._smoothed_tr = None
        self._dx_sum = Decimal(0)
        self._dx_samples = 0
        self._adx = None
        self._plus_di = None
        self._minus_di = None
