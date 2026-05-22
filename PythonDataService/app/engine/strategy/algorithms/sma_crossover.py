"""SmaCrossoverAlgorithm — new-engine port of the legacy SMA crossover strategy.

Formula: Long-only golden cross / death cross. Enter long when short SMA crosses above long SMA; exit when short SMA crosses below long SMA. Default periods follow `_rsi_range_base.py` conventions (50/200 for the divergence-research s3 variant; new engine accepts arbitrary).
Reference: Internal port from legacy `app/services/strategies/sma_crossover.py` (pandas-ta version, parity target). LEAN inspiration but no line-for-line port.
Canonical implementation: this file. Parity-pinned secondary: `app/engine/strategy/spec/evaluator.py::SpecAlgorithm` driven by `spec/fixtures/sma_crossover.spec.json` reproduces the hand-coded twin trade-by-trade. Divergence-research-only parallel: `app/research/divergence/strategies/s3_sma_crossover.py` (vectorized pandas).
Validated against: PythonDataService/tests/test_strategy_engine.py; spec ↔ hand-coded parity at `app/engine/strategy/spec/tests/test_spec_sma_parity.py`.

Golden-cross / death-cross rule lifted from
``app/services/strategies/sma_crossover.py``:

    * Enter long when the short SMA crosses **above** the long SMA.
    * Exit when the short SMA crosses **below** the long SMA.

Unlike the legacy pandas-ta version which runs bar-by-bar over a pre-computed
DataFrame, this port plugs into the LEAN-compatible engine: minute bars stream
in, a ``TradeBarConsolidator`` produces bars at ``resolution_minutes``, and both
SMAs update on each consolidated close. Orders go through the portfolio + fill
model, so trades are recorded from ``on_order_event`` fills the same way SPY
does — this keeps trade-log statistics consistent with portfolio net profit in
either fill mode.

The strategy is configurable via constructor kwargs so the registry can build
it with user-supplied parameters; defaults mirror the legacy strategy's defaults
(10/30 windows) but with a 15-minute resolution to match the rest of the Phase 1
data flow. Parity against the legacy strategy is exercised by
``test_sma_crossover_parity`` using a synthetic bar stream — the contract is
"same set of winning vs losing trades on the same input data", not bit-exact
prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.sma import SimpleMovingAverage
from app.engine.strategy.base import LoggedTrade, Strategy


@dataclass
class _PendingEntry:
    sma_short: Decimal
    sma_long: Decimal


@dataclass
class _OpenTrade:
    entry_time: datetime  # type: ignore[name-defined]
    entry_price: Decimal
    quantity: int
    sma_short: Decimal
    sma_long: Decimal


# Avoid a runtime import loop — datetime is only needed for the annotation
# above when type-checking, but we also use it at runtime in some places.
from datetime import datetime  # noqa: E402


class SmaCrossoverAlgorithm(Strategy):
    """Simple-moving-average crossover, long-only.

    Parameters
    ----------
    symbol:
        Ticker to trade. Uppercased on assignment.
    short_window:
        Period of the fast SMA. Must be >= 2.
    long_window:
        Period of the slow SMA. Must be strictly greater than ``short_window``.
    resolution_minutes:
        Consolidated bar size in minutes. Defaults to 15 to match the SPY
        strategy's resolution and reuse the same minute-bar data files.
    """

    def __init__(
        self,
        symbol: str = "SPY",
        short_window: int = 10,
        long_window: int = 30,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        if short_window < 2:
            raise ValueError("short_window must be >= 2")
        if long_window <= short_window:
            raise ValueError("long_window must be strictly greater than short_window")
        if resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be > 0")

        self._symbol_name = symbol.upper()
        self._short_window = short_window
        self._long_window = long_window
        self._resolution = timedelta(minutes=resolution_minutes)

        self._symbol: str = ""
        self._sma_short: SimpleMovingAverage | None = None
        self._sma_long: SimpleMovingAverage | None = None

        # Previous-bar crossover state — starts unknown so the first bar that
        # produces two ready SMAs only seeds the state, it does not trigger an
        # entry.
        self._prev_short_above_long: bool | None = None

        self._in_position: bool = False
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        # Sensible defaults for a standalone run — the router will override
        # these via the request body when invoked from the API.
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._sma_short = SimpleMovingAverage(f"SMA{self._short_window}", self._short_window)
        self._sma_long = SimpleMovingAverage(f"SMA{self._long_window}", self._long_window)

        self._prev_short_above_long = None
        self._in_position = False
        self._pending_entry = None
        self._open_trade = None

        self.ctx.register_consolidator(
            self._symbol,
            self._resolution,
            self._on_consolidated_bar,
        )

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------
    def _on_consolidated_bar(self, bar: TradeBar) -> None:
        assert self._sma_short is not None
        assert self._sma_long is not None
        assert self.ctx is not None

        self._sma_short.update(bar.end_time, bar.close)
        self._sma_long.update(bar.end_time, bar.close)

        # Until both SMAs have enough samples, we can't make a decision. Once
        # they become ready we *seed* the previous-state on the first eligible
        # bar without trading, so the first crossover we observe is a fresh
        # one rather than whatever historical state happens to sit there.
        if not (self._sma_short.is_ready and self._sma_long.is_ready):
            return

        assert self._sma_short.current_value is not None
        assert self._sma_long.current_value is not None

        short_val = self._sma_short.current_value
        long_val = self._sma_long.current_value
        current_above = short_val > long_val

        if self._prev_short_above_long is None:
            # First ready bar — just record the state, no trade.
            self._prev_short_above_long = current_above
            return

        fresh_golden_cross = current_above and not self._prev_short_above_long
        fresh_death_cross = (not current_above) and self._prev_short_above_long

        if self._in_position:
            if fresh_death_cross:
                self.ctx.liquidate(self._symbol)
                self.ctx.log(
                    f"EXIT SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} "
                    f"SMA{self._short_window}={short_val:.4f} "
                    f"SMA{self._long_window}={long_val:.4f}"
                )
                self._in_position = False
        else:
            if fresh_golden_cross:
                self._pending_entry = _PendingEntry(sma_short=short_val, sma_long=long_val)
                self.ctx.set_holdings(self._symbol, Decimal(1))
                self._in_position = True
                self.ctx.log(
                    f"ENTRY SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} "
                    f"SMA{self._short_window}={short_val:.4f} "
                    f"SMA{self._long_window}={long_val:.4f}"
                )

        self._prev_short_above_long = current_above

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping (same shape as SpyEmaCrossoverAlgorithm).
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {event.time} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time=event.time,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                sma_short=self._pending_entry.sma_short,
                sma_long=self._pending_entry.sma_long,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {event.time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={event.fill_price:.2f} "
                    f"SMA{self._short_window}={self._open_trade.sma_short:.4f} "
                    f"SMA{self._long_window}={self._open_trade.sma_long:.4f}"
                )
        else:
            if self._open_trade is None:
                return
            entry = self._open_trade
            pnl_pts = event.fill_price - entry.entry_price
            pnl_pct = pnl_pts / entry.entry_price
            result = "WIN" if pnl_pts >= 0 else "LOSS"
            self.trade_log.append(
                LoggedTrade(
                    entry_time=entry.entry_time,
                    entry_price=entry.entry_price,
                    exit_time=event.time,
                    exit_price=event.fill_price,
                    quantity=entry.quantity,
                    pnl_pts=pnl_pts,
                    pnl_pct=pnl_pct,
                    result=result,
                    indicators={
                        f"sma_{self._short_window}": entry.sma_short,
                        f"sma_{self._long_window}": entry.sma_long,
                    },
                    signal_reason=(f"SMA({self._short_window}) crossed below SMA({self._long_window})"),
                )
            )
            if self.ctx is not None:
                self.ctx.log(
                    f"EXIT: {event.time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={event.fill_price:.2f} PnL={pnl_pts:.2f} "
                    f"({pnl_pct * 100:.2f}%) {result}"
                )
            self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            self.ctx.liquidate(self._symbol)
            self._in_position = False
