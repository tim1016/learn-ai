"""RsiMeanReversionAlgorithm — new-engine port of the legacy RSI mean-reversion strategy.

Legacy source: ``app/services/strategies/rsi_mean_reversion.py``

Rule set (unchanged from the pandas-ta reference):
    * **Entry (long)** — when RSI(window) drops **strictly below** ``oversold``.
    * **Exit**          — when RSI(window) rises **strictly above** ``overbought``.
    * **End of run**    — any open position is closed on ``on_end_of_algorithm``.

The legacy strategy consumes a pre-computed DataFrame and enters/exits at the
bar's close. The new engine routes minute bars through a ``TradeBarConsolidator``
and fills through the configured fill model, so actual fill prices will drift
slightly (signal-bar-close vs next-bar-open). The trade-set contract — same
entries, same exits, same WIN/LOSS verdict on the same input data — is
validated by ``test_rsi_mean_reversion_parity`` against the legacy module.

Parameters are constructor kwargs so the router's strategy registry can build
instances from a user-supplied ``RsiMeanReversionParams`` Pydantic model.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import Direction, OrderEvent
from app.engine.indicators.rsi import RelativeStrengthIndex
from app.engine.strategy.base import LoggedTrade, Strategy


@dataclass
class _PendingEntry:
    rsi: Decimal


@dataclass
class _OpenTrade:
    entry_time: datetime
    entry_price: Decimal
    entry_rsi: Decimal


class RsiMeanReversionAlgorithm(Strategy):
    """RSI-threshold mean reversion, long-only.

    Parameters
    ----------
    symbol:
        Ticker to trade. Uppercased on assignment.
    window:
        RSI period. Must be >= 2.
    oversold:
        Entry threshold. The strategy goes long when RSI drops strictly
        below this value. Legacy default: 30.
    overbought:
        Exit threshold. The strategy exits when RSI rises strictly above
        this value. Legacy default: 70.
    resolution_minutes:
        Consolidated bar size in minutes. Defaults to 15 to match the rest
        of the Phase 1 data flow.
    """

    def __init__(
        self,
        symbol: str = "SPY",
        window: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        resolution_minutes: int = 15,
    ) -> None:
        super().__init__()
        if window < 2:
            raise ValueError("window must be >= 2")
        if not 0 < oversold < overbought < 100:
            raise ValueError(
                "require 0 < oversold < overbought < 100 "
                f"(got oversold={oversold}, overbought={overbought})"
            )
        if resolution_minutes <= 0:
            raise ValueError("resolution_minutes must be > 0")

        self._symbol_name = symbol.upper()
        self._window = window
        self._oversold = Decimal(str(oversold))
        self._overbought = Decimal(str(overbought))
        self._resolution = timedelta(minutes=resolution_minutes)

        self._symbol: str = ""
        self._rsi: RelativeStrengthIndex | None = None

        self._in_position: bool = False
        self._pending_entry: Optional[_PendingEntry] = None
        self._open_trade: Optional[_OpenTrade] = None

        self.trade_log: list[LoggedTrade] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        self.set_start_date(2024, 3, 28)
        self.set_end_date(2026, 3, 27)
        self.set_cash(100000)

        assert self.ctx is not None
        self._symbol = self.ctx.add_equity(self._symbol_name)

        self._rsi = RelativeStrengthIndex(f"RSI{self._window}", self._window)

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
        assert self._rsi is not None
        assert self.ctx is not None

        self._rsi.update(bar.end_time, bar.close)
        if not self._rsi.is_ready:
            return

        rsi_val = self._rsi.current_value
        assert rsi_val is not None

        if not self._in_position:
            if rsi_val < self._oversold:
                self._pending_entry = _PendingEntry(rsi=rsi_val)
                self.ctx.set_holdings(self._symbol, Decimal(1))
                self._in_position = True
                self.ctx.log(
                    f"ENTRY SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} RSI{self._window}={rsi_val:.2f} "
                    f"< oversold({self._oversold})"
                )
        else:
            if rsi_val > self._overbought:
                self.ctx.liquidate(self._symbol)
                self.ctx.log(
                    f"EXIT SIGNAL: {bar.end_time.strftime('%Y-%m-%d %H:%M')} "
                    f"Close={bar.close:.2f} RSI{self._window}={rsi_val:.2f} "
                    f"> overbought({self._overbought})"
                )
                self._in_position = False

    # ------------------------------------------------------------------
    # Fill-driven trade bookkeeping
    # ------------------------------------------------------------------
    def on_order_event(self, event: OrderEvent) -> None:
        if event.direction == Direction.LONG:
            if self._pending_entry is None:
                if self.ctx is not None:
                    self.ctx.log(
                        f"WARN: LONG fill at {event.time} with no pending entry"
                    )
                return
            self._open_trade = _OpenTrade(
                entry_time=event.time,
                entry_price=event.fill_price,
                entry_rsi=self._pending_entry.rsi,
            )
            self._pending_entry = None
            if self.ctx is not None:
                self.ctx.log(
                    f"ENTRY: {event.time.strftime('%Y-%m-%d %H:%M')} "
                    f"Price={event.fill_price:.2f} "
                    f"RSI{self._window}={self._open_trade.entry_rsi:.2f}"
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
                    pnl_pts=pnl_pts,
                    pnl_pct=pnl_pct,
                    result=result,
                    indicators={
                        f"rsi_{self._window}": entry.entry_rsi,
                    },
                    signal_reason=(
                        f"RSI({self._window}) crossed above "
                        f"overbought({self._overbought})"
                    ),
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
