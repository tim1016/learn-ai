"""Tests for LiveEngine driver behavior before the full replay gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int, open_: str, close: str) -> TradeBar:
    start = datetime(2026, 5, 4, 14, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=max(Decimal(open_), Decimal(close)),
        low=min(Decimal(open_), Decimal(close)),
        close=Decimal(close),
        volume=100,
    )


class OneEntryStrategy(Strategy):
    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        if not self.ctx.portfolio.pending_orders and not self.ctx.portfolio.get_position("SPY").quantity:
            self.ctx.set_holdings("SPY", Decimal("1"))


@pytest.mark.asyncio
async def test_live_engine_processes_bar_signal_submission_and_next_bar_fill() -> None:
    broker = FakeBroker()
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    bars = [_bar(minute, "500", "500") for minute in range(30, 47)]

    result = await engine.run(OneEntryStrategy(), iter_bars(bars))

    assert result.submitted_order_ids == [1]
    assert len(result.order_events) == 1
    assert result.order_events[0].time == bars[-1].time
    assert result.order_events[0].fill_price == Decimal("500")
    assert result.order_events[0].fill_quantity == 200
    assert result.pending_orders == 0
    assert result.open_positions == {"SPY": 200}
    assert len(result.equity_curve) == len(bars)
