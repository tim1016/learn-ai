"""Independent review: portfolio marks must use the snapshot's own source bar.

Synthetic invariant fixture, not a port or a golden-reference replacement.
Formula: marked equity = cash + shares * current observed close.
Reference: app.engine.execution.portfolio.Portfolio.total_equity.
Canonical implementation: app.engine.engine.BacktestEngine.
Validated against: exact Decimal identity asserted below; no tolerance needed.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy

_START_MS = 1770647400000  # 2026-02-09 09:30 America/New_York.


class _SyntheticBars:
    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        for index, value in enumerate(('100', '101', '99', '100')):
            price = Decimal(value)
            yield TradeBar(
                symbol=symbol,
                start_ms=_START_MS + index * 60_000,
                end_ms=_START_MS + (index + 1) * 60_000,
                open=price, high=price, low=price, close=price, volume=10000,
            )


class _BuyAndHold(Strategy):
    def initialize(self) -> None:
        self.set_start_date(2026, 2, 9)
        self.set_end_date(2026, 2, 9)
        self.set_cash(10000)
        self.bought = False
        assert self.ctx is not None
        symbol = self.ctx.add_equity('SPY')
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, bar: TradeBar) -> None:
        if not self.bought:
            assert self.ctx is not None
            self.ctx.set_holdings('SPY', Decimal(1))
            self.bought = True


def test_snapshot_marks_holdings_at_its_own_observed_close() -> None:
    result = BacktestEngine(
        _SyntheticBars(),
        fill_model=FillModel(
            mode=FillMode.SIGNAL_BAR_CLOSE,
            commission_per_order=Decimal(0),
            slippage_per_share=Decimal(0),
        ),
    ).run(_BuyAndHold())
    entry = result.order_events[0]
    assert entry.fill_price == Decimal(100)
    assert entry.fill_quantity == 100
    snapshot = next(point for point in result.equity_curve if point.timestamp_ms == _START_MS + 120_000)
    # The fill belongs to the previous completed minute ($100); the snapshot
    # timestamp belongs to the newly observed minute, whose close is $101.
    assert snapshot.equity == snapshot.cash + Decimal(100) * Decimal(101)
