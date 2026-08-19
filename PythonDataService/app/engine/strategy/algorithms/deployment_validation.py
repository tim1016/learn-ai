"""DeploymentValidationConsecutiveGreen — minute-bar lifecycle validation strategy.

Formula: Long-only deployment-validation strategy on 1-minute signal bars.
Starting at 09:45 ET, detect two consecutive green minute bars (close > open).
After the second green bar, submit an entry order for the configured trade
symbol intended to fill with Engine Lab's ``next_bar_open`` mode on the third
bar. Hold through the third, fourth, and fifth signal-bar closes, then submit
``Liquidate`` on the fifth bar. Reset detection state after each exit cycle.
Stop detecting new entries at 15:45 ET and liquidate any open position.
Reference: Internal strategy specification from user session 2026-06-02.
Canonical implementation: this file. LEAN companion:
``app/lean_sidecar/trusted_samples/deployment_validation.py``.
Validated against: ``tests/engine/test_deployment_validation_strategy.py`` and
``tests/lean_sidecar/test_deployment_validation_template.py``. No external
golden fixture because this is an internal deployment-validation primitive, not
an alpha port.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from enum import StrEnum

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.strategy.base import LoggedTrade, Strategy, ny_datetime

_DETECTION_START = time(9, 45)
_STOP_AND_FLATTEN = time(15, 45)
_BARS_FROM_ENTRY_FILL_TO_EXIT_SIGNAL = 3


class DeploymentDecision(StrEnum):
    """Broker-neutral semantic output of the validation strategy kernel."""

    HOLD = "HOLD"
    ENTER = "ENTER"
    EXIT = "EXIT"


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    signal_time_ms: int


@dataclass(frozen=True)
class _DeploymentDecisionSnapshot:
    bar_close_ms: int
    signal: str
    intended_price: float


class DeploymentValidationDecisionKernel:
    """Signal-only decision clock shared by Engine Lab and the Alpaca runtime.

    Formula: emit ENTER after two eligible green minute bars, then EXIT after
    three subsequent closed bars; emit EXIT at the daily stop when active.
    Reference: Internal deployment-validation strategy specification from user
    session 2026-06-02.
    Canonical implementation: this file; the Alpaca runtime calls this class
    rather than duplicating the cadence.
    Validated against: tests/engine/test_deployment_validation_strategy.py::
    test_signal_kernel_emits_semantic_enter_then_exit_without_execution_state.

    It owns consecutive-green detection and the three-bar decision cadence. It
    never receives an order acknowledgement, fill, position, or broker side;
    callers send its ``ENTER`` / ``EXIT`` output to their execution authority.
    """

    def __init__(self) -> None:
        self._current_date = None
        self._green_streak = 0
        self._cycle_active = False
        self._bars_since_enter = 0
        self._stopped_for_day = False

    def on_closed_bar(
        self,
        *,
        end_ms: int,
        open_price: Decimal,
        close_price: Decimal,
    ) -> DeploymentDecision:
        end_time = ny_datetime(end_ms)
        if self._current_date != end_time.date():
            self._current_date = end_time.date()
            self._green_streak = 0
            self._cycle_active = False
            self._bars_since_enter = 0
            self._stopped_for_day = False
        if end_time.time() >= _STOP_AND_FLATTEN:
            self._stopped_for_day = True
            self._green_streak = 0
            if self._cycle_active:
                self._cycle_active = False
                self._bars_since_enter = 0
                return DeploymentDecision.EXIT
            return DeploymentDecision.HOLD
        if self._stopped_for_day or end_time.time() < _DETECTION_START:
            self._green_streak = 0
            return DeploymentDecision.HOLD
        if self._cycle_active:
            self._bars_since_enter += 1
            if self._bars_since_enter >= _BARS_FROM_ENTRY_FILL_TO_EXIT_SIGNAL:
                self._cycle_active = False
                self._bars_since_enter = 0
                self._green_streak = 0
                return DeploymentDecision.EXIT
            return DeploymentDecision.HOLD
        self._green_streak = self._green_streak + 1 if close_price > open_price else 0
        if self._green_streak >= 2:
            self._green_streak = 0
            self._cycle_active = True
            self._bars_since_enter = 0
            return DeploymentDecision.ENTER
        return DeploymentDecision.HOLD


class DeploymentValidationConsecutiveGreen(Strategy):
    """Deterministic minute-bar strategy for validating deployment plumbing."""

    STRATEGY_KEY = "deployment_validation"
    CONSOLIDATOR_PERIOD_MIN = 1

    def __init__(self, symbol: str = "SPY", trade_symbol: str | None = None) -> None:
        super().__init__()
        self._signal_symbol_name = symbol.upper()
        self._trade_symbol_name = (trade_symbol or symbol).upper()
        self._signal_symbol: str = ""
        self._trade_symbol: str = ""

        self._current_date = None
        self._green_streak = 0
        self._entry_pending = False
        self._in_position = False
        self._stopped_for_day = False
        self._bars_until_exit_signal = 0
        self._pending_signal_time_ms: int | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

    def _publish_decision(self, bar: TradeBar, signal: str) -> None:
        self.last_decision_snapshot = _DeploymentDecisionSnapshot(
            bar_close_ms=bar.end_ms,
            signal=signal,
            intended_price=float(bar.close),
        )

    def initialize(self) -> None:
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 4, 15)
        self.set_cash(100000)

        assert self.ctx is not None
        self._signal_symbol = self.ctx.add_equity(self._signal_symbol_name)
        self._trade_symbol = self._trade_symbol_name
        # A passthrough 1-minute consolidator keeps this strategy on the same
        # charting/order-drain path as other Engine Lab strategies. Decisions
        # are made in on_minute_bar so next_bar_open fills land on the third
        # raw minute bar after the two green confirmation bars.
        self.ctx.register_consolidator(self._signal_symbol, timedelta(minutes=1), self._on_one_minute_bar)

    def _reset_detection(self) -> None:
        self._green_streak = 0
        self._entry_pending = False
        self._pending_signal_time_ms = None

    def _reset_day(self) -> None:
        self._reset_detection()
        self._stopped_for_day = False

    def _on_one_minute_bar(self, _bar: TradeBar) -> None:
        # Decisions are intentionally driven by on_minute_bar; this handler
        # exists to retain consolidated chart bars and satisfy engine order
        # draining for market orders.
        return

    def on_minute_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None

        end_time = ny_datetime(bar.end_ms)
        bar_date = end_time.date()
        if self._current_date is None or bar_date != self._current_date:
            self._current_date = bar_date
            self._reset_day()

        if end_time.time() >= _STOP_AND_FLATTEN:
            self._stopped_for_day = True
            self._reset_detection()
            signal = "HOLD"
            if self._in_position or self._entry_pending:
                self.ctx.liquidate(self._trade_symbol)
                self.ctx.log(f"SESSION FLATTEN SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')}")
                self._in_position = False
                self._entry_pending = False
                self._bars_until_exit_signal = 0
                signal = "EXIT"
            self._publish_decision(bar, signal)
            return

        if self._stopped_for_day or end_time.time() < _DETECTION_START:
            self._reset_detection()
            self._publish_decision(bar, "HOLD")
            return

        if self._in_position:
            self._bars_until_exit_signal -= 1
            signal = "HOLD"
            if self._bars_until_exit_signal <= 0:
                self.ctx.liquidate(self._trade_symbol)
                self.ctx.log(f"EXIT SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')} Close={bar.close:.2f}")
                self._in_position = False
                self._bars_until_exit_signal = 0
                self._reset_detection()
                signal = "EXIT"
            self._publish_decision(bar, signal)
            return

        if self._entry_pending:
            self._publish_decision(bar, "HOLD")
            return

        if bar.close > bar.open:
            self._green_streak += 1
        else:
            self._green_streak = 0

        if self._green_streak >= 2:
            self._pending_signal_time_ms = bar.end_ms
            # Cross-asset ``trade_symbol`` is legacy deployment metadata. Engine
            # Lab hides/rejects it because the backtest engine has one price
            # stream; the retired IBKR runner was its only execution consumer.
            self.ctx.set_holdings(self._trade_symbol, Decimal(1))
            self._entry_pending = True
            self._green_streak = 0
            self.ctx.log(f"ENTRY SIGNAL: {end_time.strftime('%Y-%m-%d %H:%M')} Close={bar.close:.2f}")
            self._publish_decision(bar, "ENTER")
            return

        self._publish_decision(bar, "HOLD")

    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            signal_time_ms = self._pending_signal_time_ms or event.filled_at_ms
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                signal_time_ms=signal_time_ms,
            )
            self._entry_pending = False
            self._in_position = True
            self._bars_until_exit_signal = _BARS_FROM_ENTRY_FILL_TO_EXIT_SIGNAL
            if self.ctx is not None:
                self.ctx.log(f"ENTRY FILL: {ny_datetime(event.filled_at_ms):%Y-%m-%d %H:%M} Price={event.fill_price:.2f}")
            return

        if self._open_trade is None:
            return

        entry = self._open_trade
        exit_price = event.fill_price
        pnl_pts = exit_price - entry.entry_price
        pnl_pct = pnl_pts / entry.entry_price
        result = "WIN" if pnl_pts >= 0 else "LOSS"
        self.trade_log.append(
            LoggedTrade(
                entry_time_ms=entry.entry_time_ms,
                entry_price=entry.entry_price,
                exit_time_ms=event.filled_at_ms,
                exit_price=exit_price,
                quantity=entry.quantity,
                pnl_pts=pnl_pts,
                pnl_pct=pnl_pct,
                result=result,
                indicators={"signal_time_ms": Decimal(entry.signal_time_ms)},
                signal_reason="two_consecutive_green_minute_bars",
            )
        )
        if self.ctx is not None:
            self.ctx.log(f"EXIT FILL: {ny_datetime(event.filled_at_ms):%Y-%m-%d %H:%M} Price={event.fill_price:.2f}")
        self._open_trade = None
        self._in_position = False
        self._bars_until_exit_signal = 0
        self._reset_detection()

    def on_force_flat(self) -> None:
        self._open_trade = None
        self._in_position = False
        self._bars_until_exit_signal = 0
        self._reset_detection()

    def on_end_of_algorithm(self) -> None:
        if self._in_position or self._entry_pending:
            assert self.ctx is not None
            self.ctx.liquidate(self._trade_symbol)
            self._in_position = False
            self._entry_pending = False
