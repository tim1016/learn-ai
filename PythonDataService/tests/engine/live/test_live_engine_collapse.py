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


class EntryThenExitStrategy(Strategy):
    """Submits one entry on the first consolidator emit and one exit on the second.

    Uses a 1-minute consolidator so two emissions land within a small bar
    window; that lets the test verify the collapse handling on **both**
    the entry and the exit fill, closing the loop the Phase 9 plan calls
    for ("Repeat for the symmetric collapse on the exit order").
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[OrderEvent] = []
        self._stage: int = 0  # 0=before entry, 1=entered, 2=exited

    def initialize(self) -> None:
        assert self.ctx is not None
        self.set_cash(Decimal("100000"))
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_bar)

    def _on_bar(self, _bar: TradeBar) -> None:
        assert self.ctx is not None
        if self._stage == 0:
            self.ctx.set_holdings("SPY", Decimal("1"))
            self._stage = 1
        elif self._stage == 1:
            self.ctx.liquidate("SPY")
            self._stage = 2

    def on_order_event(self, event: OrderEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_live_engine_applies_collapsed_exit_fill_once() -> None:
    # Five bars give the 1-minute consolidator three emissions, leaving
    # room for entry submission, entry fill, exit submission, and exit fill
    # to all land before the loop ends.
    bars = [_bar(i, "500.00", "500.00") for i in range(5)]
    broker = CollapsedLifecycleFakeBroker()
    strategy = EntryThenExitStrategy()

    result = await LiveEngine(None, broker=broker).run(strategy, iter_bars(bars))

    # Two orders submitted: entry then exit.
    assert len(result.submitted_order_ids) == 2
    entry_id, exit_id = result.submitted_order_ids
    assert entry_id != exit_id

    # Both lifecycles collapsed — full status churn internally, only the
    # final ``Filled`` yielded to the engine.
    assert broker.internal_statuses[entry_id] == ["PendingSubmit", "Submitted", "Filled"]
    assert broker.yielded_statuses[entry_id] == ["Filled"]
    assert broker.internal_statuses[exit_id] == ["PendingSubmit", "Submitted", "Filled"]
    assert broker.yielded_statuses[exit_id] == ["Filled"]

    # Both fills reached the strategy and were captured in the result.
    assert len(strategy.events) == 2
    assert len(result.order_events) == 2

    # After the exit fills, position is flat; cash is consistent across
    # the engine's equity curve and the broker's internal book.
    assert result.open_positions == {}
    assert broker.positions == {"SPY": 0}
    assert result.equity_curve[-1].cash == broker.cash
