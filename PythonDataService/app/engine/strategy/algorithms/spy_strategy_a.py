"""Strategy A — EMA-gap + MACD + RSI-range, ADX(<15) exit.

Entry gates (all evaluated each bar while flat):
    * ``rsi_low_gate <= RSI <= rsi_high_gate`` (simple range filter).
    * ``EMA(fast) - EMA(slow) > ema_gap_threshold``.
    * MACD line > 0.

Exit:
    ADX(exit_period) < ``adx_exit_threshold`` (default 15).

Long-only. 15-min RTH bars. 100 %-equity sizing. No SL/TP — TV default.
Pyramiding=1 enforced by the base class's ``_in_position`` flag.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.ema import ExponentialMovingAverage
from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.strategy.algorithms._rsi_range_base import RsiRangeStrategy


class SpyStrategyAAlgorithm(RsiRangeStrategy):
    def __init__(
        self,
        symbol: str = "SPY",
        ema_fast_period: int = 20,
        ema_slow_period: int = 50,
        ema_gap_threshold: Decimal | float | int = Decimal("0.5"),
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        rsi_period: int = 14,
        rsi_low_gate: Decimal | float | int = 38,
        rsi_high_gate: Decimal | float | int = 70,
        adx_period: int = 14,
        adx_exit_threshold: Decimal | float | int = 15,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__(
            symbol=symbol,
            rsi_period=rsi_period,
            rsi_low_gate=rsi_low_gate,
            rsi_high_gate=rsi_high_gate,
            adx_period=adx_period,
            adx_exit_threshold=adx_exit_threshold,
            resolution_minutes=resolution_minutes,
        )
        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.ema_gap_threshold = Decimal(str(ema_gap_threshold))
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

        self._ema_fast: ExponentialMovingAverage | None = None
        self._ema_slow: ExponentialMovingAverage | None = None
        self._macd: MovingAverageConvergenceDivergence | None = None

    def _init_extra_indicators(self) -> None:
        self._ema_fast = ExponentialMovingAverage("A_EMA_fast", self.ema_fast_period)
        self._ema_slow = ExponentialMovingAverage("A_EMA_slow", self.ema_slow_period)
        self._macd = MovingAverageConvergenceDivergence("A_MACD", self.macd_fast, self.macd_slow, self.macd_signal)

    def _update_extra_indicators(self, bar: TradeBar) -> None:
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._macd is not None
        self._ema_fast.update(bar.end_time, bar.close)
        self._ema_slow.update(bar.end_time, bar.close)
        self._macd.update(bar.end_time, bar.close)

    def _extra_indicators_ready(self) -> bool:
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._macd is not None
        return self._ema_fast.is_ready and self._ema_slow.is_ready and self._macd.is_ready

    def _entry_extra_gate_passes(self, bar: TradeBar) -> bool:
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._macd is not None
        fast = self._ema_fast.current_value
        slow = self._ema_slow.current_value
        macd_line = self._macd.macd
        if fast is None or slow is None or macd_line is None:
            return False
        gap_ok = (fast - slow) > self.ema_gap_threshold
        macd_ok = macd_line > Decimal(0)
        return gap_ok and macd_ok

    def _indicator_snapshot(self, bar: TradeBar) -> dict[str, Decimal]:
        snap = super()._indicator_snapshot(bar)
        assert self._ema_fast is not None
        assert self._ema_slow is not None
        assert self._macd is not None
        if self._ema_fast.current_value is not None:
            snap["ema_fast"] = self._ema_fast.current_value
        if self._ema_slow.current_value is not None:
            snap["ema_slow"] = self._ema_slow.current_value
        if self._macd.macd is not None:
            snap["macd"] = self._macd.macd
        return snap
