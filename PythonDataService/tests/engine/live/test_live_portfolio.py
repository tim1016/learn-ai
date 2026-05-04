"""Tests for LivePortfolio."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrOrderAck,
    IbkrOrderSpec,
    IbkrPosition,
    IbkrPositionsSnapshot,
)
from app.engine.live.live_portfolio import LivePortfolio


class FakeBroker:
    def __init__(self) -> None:
        self.orders: list[IbkrOrderSpec] = []
        self.account = IbkrAccountSummary(
            account_id="DU123",
            is_paper=True,
            cash_balance=100_000.0,
            net_liquidation=100_000.0,
            fetched_at_ms=1,
        )
        self.positions = IbkrPositionsSnapshot(
            account_id="DU123",
            is_paper=True,
            positions=[],
            fetched_at_ms=1,
        )

    async def fetch_account_summary(self) -> IbkrAccountSummary:
        return self.account

    async def fetch_positions(self) -> IbkrPositionsSnapshot:
        return self.positions

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        self.orders.append(spec)
        return IbkrOrderAck(
            account_id="DU123",
            is_paper=True,
            order_id=len(self.orders),
            client_id=1,
            con_id=756733,
            symbol=spec.symbol,
            action=spec.action,
            quantity=spec.quantity,
            order_type=spec.order_type,
            status="PendingSubmit",
            placed_at_ms=1,
        )


@pytest.mark.asyncio
async def test_refresh_from_broker_loads_account_and_positions() -> None:
    broker = FakeBroker()
    broker.positions = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[
            IbkrPosition(
                account_id="DU123",
                con_id=756733,
                symbol="SPY",
                sec_type="STK",
                quantity=12.0,
                avg_cost=500.25,
                fetched_at_ms=1,
            )
        ],
        fetched_at_ms=1,
    )
    portfolio = LivePortfolio(broker)

    await portfolio.refresh_from_broker()

    assert portfolio.cash == Decimal("100000.0")
    assert portfolio.total_value() == Decimal("100000.0")
    assert portfolio.get_position("SPY").quantity == 12
    assert portfolio.get_position("SPY").average_price == Decimal("500.25")


def test_set_holdings_uses_reference_price_for_integer_share_count() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("501.25"))

    order = portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is not None
    assert order.quantity == 199
    assert order.tag == "SetHoldings"


def test_liquidate_submits_opposite_quantity() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.get_position("SPY").quantity = 17

    order = portfolio.liquidate("SPY", datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    assert order is not None
    assert order.quantity == -17
    assert order.tag == "Liquidate"


@pytest.mark.asyncio
async def test_submit_pending_orders_routes_through_paper_order_spec() -> None:
    broker = FakeBroker()
    portfolio = LivePortfolio(broker)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))
    portfolio.set_holdings("SPY", Decimal("1"), datetime(2026, 5, 4, 14, 45, tzinfo=UTC))

    acks = await portfolio.submit_pending_orders()

    assert len(acks) == 1
    assert broker.orders[0].symbol == "SPY"
    assert broker.orders[0].action == "BUY"
    assert broker.orders[0].quantity == 200
    assert broker.orders[0].order_type == "MKT"
    assert broker.orders[0].confirm_paper is True
    assert broker.orders[0].client_order_id == "live-1"
    assert list(portfolio.drain_pending()) == []
