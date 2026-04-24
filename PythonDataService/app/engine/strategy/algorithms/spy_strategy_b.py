"""Strategy B — Supertrend + ADX(>threshold) + MACD + RSI-range, ADX(<20) exit.

Entry gates (all evaluated each bar while flat):
    * ``rsi_low_gate <= RSI <= rsi_high_gate``.
    * Supertrend is long (price above the line).
    * ADX > ``adx_entry_threshold`` (default 20).
    * MACD line > 0.

Exit:
    ADX < ``adx_exit_threshold`` (default 20).

Long-only. 15-min RTH bars. 100 %-equity sizing. No SL/TP.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.indicators.supertrend import Supertrend
from app.engine.strategy.algorithms._rsi_range_base import RsiRangeStrategy


class SpyStrategyBAlgorithm(RsiRangeStrategy):
    def __init__(
        self,
        symbol: str = "SPY",
        supertrend_atr_period: int = 10,
        supertrend_multiplier: Decimal | float | int = 3,
        adx_entry_threshold: Decimal | float | int = 20,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        rsi_period: int = 14,
        rsi_low_gate: Decimal | float | int = 38,
        rsi_high_gate: Decimal | float | int = 70,
        adx_period: int = 14,
        adx_exit_threshold: Decimal | float | int = 20,
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
        self.supertrend_atr_period = supertrend_atr_period
        self.supertrend_multiplier = Decimal(str(supertrend_multiplier))
        self.adx_entry_threshold = Decimal(str(adx_entry_threshold))
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

        self._supertrend: Supertrend | None = None
        self._macd: MovingAverageConvergenceDivergence | None = None

    def _init_extra_indicators(self) -> None:
        self._supertrend = Supertrend("B_Supertrend", self.supertrend_atr_period, self.supertrend_multiplier)
        self._macd = MovingAverageConvergenceDivergence("B_MACD", self.macd_fast, self.macd_slow, self.macd_signal)

    def _update_extra_indicators(self, bar: TradeBar) -> None:
        assert self._supertrend is not None
        assert self._macd is not None
        self._supertrend.update(bar)
        self._macd.update(bar.end_time, bar.close)

    def _extra_indicators_ready(self) -> bool:
        assert self._supertrend is not None
        assert self._macd is not None
        return self._supertrend.is_ready and self._macd.is_ready

    def _entry_extra_gate_passes(self, bar: TradeBar) -> bool:
        assert self._supertrend is not None
        assert self._macd is not None
        assert self._adx is not None
        adx_val = self._adx.current_value
        macd_val = self._macd.macd
        return (
            bool(self._supertrend.is_long)
            and adx_val is not None
            and adx_val > self.adx_entry_threshold
            and macd_val is not None
            and macd_val > Decimal(0)
        )

    def _indicator_snapshot(self, bar: TradeBar) -> dict[str, Decimal]:
        snap = super()._indicator_snapshot(bar)
        assert self._supertrend is not None
        assert self._macd is not None
        if self._supertrend.current_value is not None:
            snap["supertrend"] = self._supertrend.current_value
        if self._macd.macd is not None:
            snap["macd"] = self._macd.macd
        return snap
