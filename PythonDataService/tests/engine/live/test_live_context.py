"""Tests for LiveContext."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.framework.insight import Insight, InsightDirection
from app.engine.live.live_context import LiveContext
from app.engine.live.live_portfolio import LivePortfolio
from tests.engine.live.fixtures.fake_broker import FakeBroker


def _minute_bar(minute: int, close: str) -> TradeBar:
    start = datetime(2026, 5, 4, 14, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


def test_register_consolidator_uses_consolidated_close_as_reference_price() -> None:
    portfolio = LivePortfolio(FakeBroker())
    ctx = LiveContext(portfolio)
    fired: list[TradeBar] = []
    ctx.register_consolidator("SPY", timedelta(minutes=15), fired.append)

    for minute in range(30, 46):
        for consolidator in ctx.get_consolidators("SPY"):
            consolidator.update(_minute_bar(minute, str(500 + minute)))

    assert len(fired) == 1
    assert ctx.current_time == fired[0].end_time
    assert ctx.consolidated_bars == fired
    assert portfolio.reference_price["SPY"] == fired[0].close


def test_set_holdings_delegates_to_live_portfolio_with_current_time() -> None:
    portfolio = LivePortfolio(FakeBroker())
    ctx = LiveContext(portfolio)
    ctx.current_time = datetime(2026, 5, 4, 14, 45, tzinfo=UTC)
    portfolio.net_liquidation = Decimal("100000")
    portfolio.update_reference_price("SPY", Decimal("500"))

    ctx.set_holdings("SPY", Decimal("1"))

    assert portfolio.pending_orders[0].time == ctx.current_time
    assert portfolio.pending_orders[0].quantity == 200


def test_liquidate_delegates_to_live_portfolio_with_current_time() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.get_position("SPY").quantity = 5
    ctx = LiveContext(portfolio)
    ctx.current_time = datetime(2026, 5, 4, 14, 45, tzinfo=UTC)

    ctx.liquidate("SPY")

    assert portfolio.pending_orders[0].quantity == -5
    assert portfolio.pending_orders[0].time == ctx.current_time


def test_emit_insight_records_reference_price_and_time() -> None:
    portfolio = LivePortfolio(FakeBroker())
    portfolio.update_reference_price("SPY", Decimal("500"))
    ctx = LiveContext(portfolio)
    ctx.current_time = datetime(2026, 5, 4, 14, 45, tzinfo=UTC)
    insight = Insight.price(
        symbol="SPY",
        direction=InsightDirection.UP,
        period=timedelta(minutes=75),
    )

    ctx.emit_insight(insight)

    assert insight.generated_time == ctx.current_time
    assert insight.close_time == ctx.current_time + timedelta(minutes=75)
    assert ctx.insight_manager.all_insights[0].reference_value == Decimal("500")


def test_set_holdings_without_current_time_raises() -> None:
    ctx = LiveContext(LivePortfolio(FakeBroker()))
    with pytest.raises(RuntimeError, match="current live bar time"):
        ctx.set_holdings("SPY", Decimal("1"))
