"""Broker lifecycle-collapse coverage for the live engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import OrderEvent
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import CollapsedLifecycleFakeBroker, iter_bars


def _bar(minute: int, open_: str, close: str) -> TradeBar:
    start = datetime(2026, 5, 4, 14, 30, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(open_) + Decimal("0.25"),
        low=Decimal(open_) - Decimal("0.25"),
        close=Decimal(close),
        volume=1000,
    )


class OneFillStrategy(Strategy):
    """Submits one market order from a consolidated bar."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[OrderEvent] = []
        self._ordered = False

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=15), self._on_bar)

    def _on_bar(self, _bar: TradeBar) -> None:
        assert self.ctx is not None
        if not self._ordered:
            self.ctx.set_holdings("SPY", Decimal("1"))
            self._ordered = True

    def on_order_event(self, event: OrderEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_live_engine_applies_collapsed_final_fill_once() -> None:
    bars = [_bar(i, "500.00", "500.00") for i in range(17)]
    broker = CollapsedLifecycleFakeBroker()
    strategy = OneFillStrategy()

    result = await LiveEngine(None, broker=broker).run(strategy, iter_bars(bars))

    assert len(result.submitted_order_ids) == 1
    order_id = result.submitted_order_ids[0]
    assert broker.internal_statuses[order_id] == ["PendingSubmit", "Submitted", "Filled"]
    assert broker.yielded_statuses[order_id] == ["Filled"]

    assert len(strategy.events) == 1
    assert len(result.order_events) == 1
    assert strategy.events[0] == result.order_events[0]
    assert result.open_positions == {"SPY": 200}
    assert result.equity_curve[-1].cash == broker.cash
    assert broker.positions == {"SPY": 200}
