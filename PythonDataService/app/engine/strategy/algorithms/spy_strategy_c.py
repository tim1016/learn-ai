"""Strategy C — ADX(>threshold) + ADX-rising + RSI-range, ADX(<15) exit.

Entry gates (all evaluated each bar while flat):
    * ``rsi_low_gate <= RSI <= rsi_high_gate``.
    * ADX > ``adx_entry_threshold`` (default 20).
    * ADX[current] > ADX[previous] (bar-over-bar rising).

Exit:
    ADX < ``adx_exit_threshold`` (default 15 — same as Strategy A).

Long-only. 15-min RTH bars. 100 %-equity sizing. No SL/TP.
"""

from __future__ import annotations

from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.algorithms._rsi_range_base import RsiRangeStrategy


class SpyStrategyCAlgorithm(RsiRangeStrategy):
    def __init__(
        self,
        symbol: str = "SPY",
        adx_entry_threshold: Decimal | float | int = 20,
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
        self.adx_entry_threshold = Decimal(str(adx_entry_threshold))

    def _entry_extra_gate_passes(self, bar: TradeBar) -> bool:
        assert self._adx is not None
        current = self._adx.current_value
        previous = self._adx.previous_value
        if current is None or previous is None:
            return False
        return current > self.adx_entry_threshold and current > previous

    def _indicator_snapshot(self, bar: TradeBar) -> dict[str, Decimal]:
        snap = super()._indicator_snapshot(bar)
        assert self._adx is not None
        if self._adx.previous_value is not None:
            snap["adx_prev"] = self._adx.previous_value
        return snap
