"""SpyOpeningRangeBreakout — Pure price-action ORB strategy, zero warmup.

Formula: Long-only ORB on 15-min SPY bars. Opening range = first 3 consolidated bars of RTH (9:30–10:15 ET). Entry: bar CLOSES above ORB high AND range ∈ [0.30%, 1.50%]. Position: SetHoldings(SPY, 1.0). Exit: 5 consolidated bars (75 minutes) after entry.
Reference: TradingView Pine validation at `docs/validation/SPY_ORB_Strategy.pine`; design doc `docs/validation/SPY_ORB_Strategy_Plan.md`; cross-system validation report `docs/validation/ORB_Cross_System_Validation_Report.pdf`. No external port reference (designed in-house against TV Pine ground truth).
Canonical implementation: this file.
Validated against: TV Pine (manual cross-check) + PDF cross-system validation report. No automated golden fixture under `tests/fixtures/golden/`; behavior is exercised via `app/engine/tests/test_spy_orb_one_trade_per_day.py`.

Strategy rules:
  * 15-minute bars consolidated from minute SPY data.
  * Long-only Opening Range Breakout (ORB).
  * Opening range = first 3 consolidated bars of RTH (9:30–10:15 ET).
  * Entry: bar CLOSES above ORB high AND range ∈ [0.30%, 1.50%].
  * Position: SetHoldings(SPY, 1.0) — all-in on the confirmation bar.
  * Exit: after exactly 5 consolidated bars (75 minutes), Liquidate.

Validation properties:
  * ZERO warmup — no indicators that accumulate state across days.
  * Each day stands alone: ORB high/low reset every session.
  * Entry signal is a deterministic comparison against exact price levels.
  * Both TradingView and Engine Lab will produce identical signals given
    the same OHLC bars, because there is no floating-point state that
    drifts between systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.framework.insight import Insight, InsightDirection
from app.engine.strategy.base import LoggedTrade, Strategy
from app.lean_sidecar.trading_calendar import is_regular_session_ms_utc
from app.utils.timestamps import datetime_at_ms


@dataclass
class _PendingEntry:
    """Snapshot captured at entry signal time."""

    orb_high: Decimal
    orb_low: Decimal


@dataclass
class _OpenTrade:
    """An entry that has filled but not yet exited."""

    entry_time_ms: int
    entry_price: Decimal
    quantity: int
    orb_high: Decimal
    orb_low: Decimal


class SpyOpeningRangeBreakout(Strategy):
    """Pure price-action Opening Range Breakout.

    Parameters
    ----------
    symbol : str
        Ticker to trade (default "SPY").
    orb_bars : int
        Number of 15-min bars forming the opening range (default 3 = 45 min).
    hold_bars : int
        Bars to hold after entry before exiting (default 5 = 75 min).
    min_range_pct : Decimal
        Minimum ORB range as a percentage of price (default 0.30%).
    max_range_pct : Decimal
        Maximum ORB range as a percentage of price (default 1.50%).
    """

    def __init__(
        self,
        symbol: str = "SPY",
        orb_bars: int = 3,
        hold_bars: int = 5,
        min_range_pct: float = 0.30,
        max_range_pct: float = 1.50,
    ) -> None:
        super().__init__()
        self._symbol_name = symbol.upper()
        self._symbol: str = ""
        self._orb_bars = orb_bars
        self._hold_bars = hold_bars
        self._min_range_pct = Decimal(str(min_range_pct))
        self._max_range_pct = Decimal(str(max_range_pct))

        # Daily state — reset each session.
        self._current_date: date | None = None
        self._bar_of_day: int = 0
        self._orb_high: Decimal = Decimal(0)
        self._orb_low: Decimal = Decimal("999999")
        self._orb_complete: bool = False
        self._orb_valid: bool = False
        # One-trade-per-day guard. Without this, the strategy re-enters on
        # every subsequent bar whose close > orb_high after the 5-bar hold
        # exit, producing ~3–4× the intended trade count. The Pine Script
        # reference has carried a `tradedToday` flag since the first SPY
        # ORB validation; the Python port missed it until the QQQ study
        # (2026-04-18) surfaced the mismatch (EL 417 trades vs TV 137).
        self._traded_today: bool = False

        # Trade state.
        self._in_position: bool = False
        self._bars_until_exit: int = 0
        self._pending_entry: _PendingEntry | None = None
        self._open_trade: _OpenTrade | None = None

        self.trade_log: list[LoggedTrade] = []

    def initialize(self) -> None:
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 4, 15)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        # 15-minute consolidator — same as EMA crossover strategy.
        self.ctx.register_consolidator(
            self._symbol,
            timedelta(minutes=15),
            self._on_fifteen_minute_bar,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_rth(bar_end_ms: int) -> bool:
        """True if the bar falls within regular trading hours."""
        return is_regular_session_ms_utc(bar_end_ms)

    def _reset_day(self) -> None:
        """Reset all daily state for a new session."""
        self._bar_of_day = 0
        self._orb_high = Decimal(0)
        self._orb_low = Decimal("999999")
        self._orb_complete = False
        self._orb_valid = False
        self._traded_today = False

    # ------------------------------------------------------------------
    # Bar handler
    # ------------------------------------------------------------------
    def _on_fifteen_minute_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None

        # Filter to RTH only.
        if not self._is_rth(bar.end_ms):
            return

        # Detect new trading day.
        bar_date = _ny_datetime(bar.end_ms).date()
        if self._current_date is None or bar_date != self._current_date:
            self._current_date = bar_date
            self._reset_day()

        self._bar_of_day += 1

        # ── Phase 1: Build the opening range ──
        if self._bar_of_day <= self._orb_bars:
            self._orb_high = max(self._orb_high, bar.high)
            self._orb_low = min(self._orb_low, bar.low)

            if self._bar_of_day == self._orb_bars:
                # ORB complete — validate range size.
                self._orb_complete = True
                range_pct = (self._orb_high - self._orb_low) / self._orb_low * 100
                self._orb_valid = self._min_range_pct <= range_pct <= self._max_range_pct

                self.ctx.log(
                    f"ORB COMPLETE: {_display_time(bar.end_ms)} "
                    f"High={self._orb_high:.2f} Low={self._orb_low:.2f} "
                    f"Range={range_pct:.4f}% Valid={self._orb_valid}"
                )
            return

        # ── Phase 2: Look for breakout / manage position ──
        if not self._orb_complete or not self._orb_valid:
            return

        if self._in_position:
            # Count down hold period.
            self._bars_until_exit -= 1
            if self._bars_until_exit <= 0:
                self.ctx.liquidate(self._symbol)
                self.ctx.log(f"EXIT SIGNAL: {_display_time(bar.end_ms)} Close={bar.close:.2f}")
                self._in_position = False
        else:
            # Entry: bar must CLOSE above ORB high. The `_traded_today`
            # guard is what makes this a one-trade-per-day strategy. See
            # the field's docstring for why it exists.
            if bar.close > self._orb_high and not self._traded_today:
                self._pending_entry = _PendingEntry(
                    orb_high=self._orb_high,
                    orb_low=self._orb_low,
                )
                self.ctx.set_holdings(self._symbol, Decimal(1))
                self._in_position = True
                self._bars_until_exit = self._hold_bars
                self._traded_today = True

                orb_range_pct = (self._orb_high - self._orb_low) / self._orb_low * 100

                self.ctx.emit_insight(
                    Insight.price(
                        symbol=self._symbol,
                        direction=InsightDirection.UP,
                        period=timedelta(minutes=15 * self._hold_bars),
                        magnitude=float((bar.close - self._orb_high) / self._orb_high),
                        confidence=0.60,
                        source_model=f"ORB_{self._orb_bars}_{self._hold_bars}",
                        tag=(f"ORB_HIGH={self._orb_high:.2f} ORB_LOW={self._orb_low:.2f} RANGE={orb_range_pct:.4f}%"),
                    )
                )

                self.ctx.log(
                    f"ENTRY SIGNAL: {_display_time(bar.end_ms)} "
                    f"Close={bar.close:.2f} ORB_HIGH={self._orb_high:.2f} "
                    f"ORB_LOW={self._orb_low:.2f} Range={orb_range_pct:.4f}%"
                )

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping (identical pattern to EMA strategy).
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(f"WARN: LONG fill at {_display_time(event.filled_at_ms)} with no pending entry")
                return
            self._open_trade = _OpenTrade(
                entry_time_ms=event.filled_at_ms,
                entry_price=event.fill_price,
                quantity=event.fill_quantity,
                orb_high=self._pending_entry.orb_high,
                orb_low=self._pending_entry.orb_low,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY FILL: {_display_time(event.filled_at_ms)} "
                    f"Price={event.fill_price:.2f} "
                    f"ORB_HIGH={self._open_trade.orb_high:.2f}"
                )
        else:
            if self._open_trade is None:
                return
            entry = self._open_trade
            exit_price = event.fill_price
            exit_time_ms = event.filled_at_ms
            pnl_pts = exit_price - entry.entry_price
            pnl_pct = pnl_pts / entry.entry_price
            result = "WIN" if pnl_pts >= 0 else "LOSS"
            self.trade_log.append(
                LoggedTrade(
                    entry_time_ms=entry.entry_time_ms,
                    entry_price=entry.entry_price,
                    exit_time_ms=exit_time_ms,
                    exit_price=exit_price,
                    quantity=entry.quantity,
                    pnl_pts=pnl_pts,
                    pnl_pct=pnl_pct,
                    result=result,
                    indicators={
                        "orb_high": entry.orb_high,
                        "orb_low": entry.orb_low,
                    },
                    signal_reason=f"ORB_HIGH={entry.orb_high:.2f}",
                )
            )
            if self.ctx is not None:
                self.ctx.log(
                    f"EXIT FILL: {_display_time(exit_time_ms)} "
                    f"Price={exit_price:.2f} PnL={pnl_pts:.2f} "
                    f"({pnl_pct * 100:.2f}%) {result}"
                )
            self._open_trade = None

    def on_end_of_algorithm(self) -> None:
        if self._in_position:
            assert self.ctx is not None
            self.ctx.liquidate(self._symbol)
            self._in_position = False


def _ny_datetime(timestamp_ms: int):
    from zoneinfo import ZoneInfo

    return datetime_at_ms(timestamp_ms, tz=ZoneInfo("America/New_York"))


def _display_time(timestamp_ms: int) -> str:
    return _ny_datetime(timestamp_ms).strftime("%Y-%m-%d %H:%M")
