"""Strategy C — ADX(>threshold) + ADX-rising + RSI-range, ADX(<15) exit.

Formula: RSI(14) range + ADX > 20 + ADX rising bar-over-bar → entry; ADX < 15 → exit. Extends RsiRangeStrategy base.
Reference: Internal — no external port reference; long-only 15-min RTH SPY strategy.
Canonical implementation: app/engine/strategy/algorithms/spy_strategy_c.py (extends the shared
    custody-split machinery in app/engine/strategy/algorithms/_rsi_range_base.py::RsiRangeStrategy).
Validated against: app/engine/tests/ (gate-wiring unit tests); golden fixture ENG-008
    (tests/fixtures/test_strategy_parity_fixtures.py) pins trade_log self-equivalence
    at registered defaults — the base class's #1700 port to SignalIntent emission
    reproduces this trade_log unchanged, per that fixture's #1699 pre-port receipt;
    tests/engine/strategy/test_spy_strategy_c_signal_program.py::
    test_validated_spy_strategy_c_settings_corpus_has_a_pinned_trace_root (issue #1730 Slice 5).

Entry gates (all evaluated each bar while flat):
    * ``rsi_low_gate <= RSI <= rsi_high_gate``.
    * ADX > ``adx_entry_threshold`` (default 20).
    * ADX[current] > ADX[previous] (bar-over-bar rising).

Exit:
    ADX < ``adx_exit_threshold`` (default 15 — same as Strategy A).

Long-only. 15-min RTH bars. Emits ENTER/EXIT SignalIntents (100 %-equity sizing when
bound by Engine Lab's same-symbol executor). No SL/TP.
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

    def _extra_signal_program_settings(self) -> dict[str, str]:
        return {"adx_entry_threshold": str(self.adx_entry_threshold)}
