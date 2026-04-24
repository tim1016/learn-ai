"""Shared base class for the RSI-range strategy family (A / B / C).

All three share the same skeleton:

* 15-min consolidator (minute data → 15-min bars).
* Wilders RSI(14) + ADX(14) as the always-on shared indicators.
* Simple RSI *range* filter on entry — no state machine.
* Shared exit: ``ADX < exit_threshold``.
* Long-only, 100 % equity sizing via ``set_holdings(symbol, 1.0)``.
* LEAN-style two-stage trade bookkeeping
  (pending-entry on signal → open-trade on fill → trade_log on exit fill).

On each bar while flat, all gates are evaluated; if they all pass, a
market order submits for next-bar-open fill. ``pyramiding=1`` is enforced
by the ``_in_position`` flag — the strategy will not resubmit while
holding.

Subclasses override the small extension surface:

    _init_extra_indicators()
    _extra_indicators_ready() -> bool
    _update_extra_indicators(bar)
    _entry_extra_gate_passes(bar) -> bool
    _indicator_snapshot(bar) -> dict[str, Decimal]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.adx import AverageDirectionalIndex
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import LoggedTrade, Strategy


@dataclass
class _OpenTrade:
    entry_time: datetime
    entry_price: Decimal
    indicators: dict[str, Decimal]


class RsiRangeStrategy(Strategy):
    """Template-method base for the RSI-range family."""

    def __init__(
        self,
        symbol: str = "SPY",
        rsi_period: int = 14,
        rsi_low_gate: Decimal | float | int = 38,
        rsi_high_gate: Decimal | float | int = 70,
        adx_period: int = 14,
        adx_exit_threshold: Decimal | float | int = 15,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        self._symbol_name = symbol.upper()
        self._symbol: str = ""
        self._resolution = timedelta(minutes=resolution_minutes)

        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.rsi_low_gate = Decimal(str(rsi_low_gate))
        self.rsi_high_gate = Decimal(str(rsi_high_gate))
        if self.rsi_low_gate >= self.rsi_high_gate:
            raise ValueError(f"rsi_low_gate ({rsi_low_gate}) must be < rsi_high_gate ({rsi_high_gate})")
        self.adx_exit_threshold = Decimal(str(adx_exit_threshold))

        self._rsi: RelativeStrengthIndex | None = None
        self._adx: AverageDirectionalIndex | None = None

        self._in_position: bool = False
        self._pending_entry: dict[str, Decimal] | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

    # ------------------------------------------------------------------
    def initialize(self) -> None:
        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)
        self._rsi = RelativeStrengthIndex(f"{self.__class__.__name__}_RSI", self.rsi_period)
        self._adx = AverageDirectionalIndex(f"{self.__class__.__name__}_ADX", self.adx_period)
        self._init_extra_indicators()
        self.ctx.register_consolidator(self._symbol, self._resolution, self._on_bar)

    # ------------------------------------------------------------------
    def _on_bar(self, bar: TradeBar) -> None:
        assert self._rsi is not None
        assert self._adx is not None
        assert self.ctx is not None

        self._rsi.update(bar.end_time, bar.close)
        self._adx.update(bar)
        self._update_extra_indicators(bar)

        # --- Exit: any existing position exits on ADX < threshold.
        if self._in_position:
            if self._adx.current_value is not None and self._adx.current_value < self.adx_exit_threshold:
                self.ctx.liquidate(self._symbol)
                self._in_position = False
                self.ctx.log(
                    f"EXIT SIGNAL: {bar.end_time:%Y-%m-%d %H:%M} "
                    f"adx={float(self._adx.current_value):.2f} "
                    f"< {float(self.adx_exit_threshold):.2f}"
                )
            return

        # --- Entry: all indicators ready?
        if not (self._rsi.is_ready and self._adx.is_ready):
            return
        if not self._extra_indicators_ready():
            return

        # --- RSI range filter.
        rsi_val = self._rsi.current_value
        if rsi_val is None:
            return
        if not (self.rsi_low_gate <= rsi_val <= self.rsi_high_gate):
            return

        # --- Strategy-specific gates.
        if not self._entry_extra_gate_passes(bar):
            return

        self._pending_entry = self._indicator_snapshot(bar)
        self.ctx.set_holdings(self._symbol, Decimal(1))
        self._in_position = True
        self.ctx.log(f"ENTRY SIGNAL: {bar.end_time:%Y-%m-%d %H:%M} close={bar.close:.2f} rsi={float(rsi_val):.2f}")

    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                return
            self._open_trade = _OpenTrade(
                entry_time=event.time,
                entry_price=event.fill_price,
                indicators=self._pending_entry,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(f"ENTRY: {event.time:%Y-%m-%d %H:%M} price={event.fill_price:.2f}")
            return

        # Exit fill.
        if self._open_trade is None:
            return
        entry = self._open_trade
        pnl_pts = event.fill_price - entry.entry_price
        pnl_pct = pnl_pts / entry.entry_price
        self.trade_log.append(
            LoggedTrade(
                entry_time=entry.entry_time,
                entry_price=entry.entry_price,
                exit_time=event.time,
                exit_price=event.fill_price,
                pnl_pts=pnl_pts,
                pnl_pct=pnl_pct,
                result="WIN" if pnl_pts >= 0 else "LOSS",
                indicators=entry.indicators,
            )
        )
        self._open_trade = None
        if self.ctx is not None:
            self.ctx.log(
                f"EXIT: {event.time:%Y-%m-%d %H:%M} price={event.fill_price:.2f} "
                f"pnl={pnl_pts:.2f} ({float(pnl_pct) * 100:.2f}%)"
            )

    def on_force_flat(self) -> None:
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position and self.ctx is not None:
            self.ctx.liquidate(self._symbol)
            self._in_position = False

    # ------------------------------------------------------------------
    # Subclass extension points.
    # ------------------------------------------------------------------
    def _init_extra_indicators(self) -> None: ...

    def _extra_indicators_ready(self) -> bool:
        return True

    def _update_extra_indicators(self, bar: TradeBar) -> None: ...

    def _entry_extra_gate_passes(self, bar: TradeBar) -> bool:
        return True

    def _indicator_snapshot(self, bar: TradeBar) -> dict[str, Decimal]:
        assert self._rsi is not None
        assert self._adx is not None
        snap: dict[str, Decimal] = {}
        if self._rsi.current_value is not None:
            snap["rsi"] = self._rsi.current_value
        if self._adx.current_value is not None:
            snap["adx"] = self._adx.current_value
        return snap
