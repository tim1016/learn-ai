"""Tests for app.engine.execution.fill_model.FillModel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import (
    Direction,
    FillMode,
    Order,
    OrderType,
)


def _bar(open_: str, high: str, low: str, close: str, offset_min: int = 0) -> TradeBar:
    start = datetime(2024, 1, 1, 14, 30, tzinfo=UTC) + timedelta(minutes=offset_min)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def _order(direction: Direction, quantity: int = 100) -> Order:
    return Order(
        order_id=1,
        symbol="SPY",
        quantity=quantity,
        order_type=OrderType.MARKET,
        time=datetime(2024, 1, 1, 14, 30, tzinfo=UTC),
        direction=direction,
    )


def test_signal_bar_close_fills_at_close_with_no_slippage():
    model = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE)
    signal = _bar("100.0", "100.5", "99.5", "100.3")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=None)

    assert event is not None
    assert event.fill_price == Decimal("100.3")
    assert event.fill_time if False else event.time == signal.end_time
    assert event.fee == Decimal("1.00")


def test_next_bar_open_defers_when_next_bar_missing():
    model = FillModel(mode=FillMode.NEXT_BAR_OPEN)
    signal = _bar("100.0", "100.5", "99.5", "100.3")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=None)

    assert event is None


def test_next_bar_open_fills_at_next_bar_open():
    model = FillModel(mode=FillMode.NEXT_BAR_OPEN)
    signal = _bar("100.0", "100.5", "99.5", "100.3")
    next_bar = _bar("100.4", "100.9", "100.1", "100.7", offset_min=1)

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_bar)

    assert event is not None
    assert event.fill_price == Decimal("100.4")
    assert event.time == next_bar.time


def test_long_slippage_adds_to_fill_price():
    model = FillModel(slippage_per_share=Decimal("0.02"))
    signal = _bar("100.0", "100.5", "99.5", "100.0")

    event = model.fill_market_order(_order(Direction.LONG), signal)

    assert event is not None
    assert event.fill_price == Decimal("100.02")


def test_short_slippage_subtracts_from_fill_price():
    model = FillModel(slippage_per_share=Decimal("0.02"))
    signal = _bar("100.0", "100.5", "99.5", "100.0")

    event = model.fill_market_order(_order(Direction.SHORT, quantity=-100), signal)

    assert event is not None
    assert event.fill_price == Decimal("99.98")


def test_commission_propagates_to_order_event():
    model = FillModel(commission_per_order=Decimal("0.50"))
    signal = _bar("100.0", "100.5", "99.5", "100.3")

    event = model.fill_market_order(_order(Direction.LONG), signal)

    assert event is not None
    assert event.fee == Decimal("0.50")


def test_non_market_orders_rejected():
    model = FillModel()
    signal = _bar("100.0", "100.5", "99.5", "100.3")
    limit_order = Order(
        order_id=2,
        symbol="SPY",
        quantity=100,
        order_type=OrderType.LIMIT,
        time=signal.time,
        direction=Direction.LONG,
        limit_price=Decimal("99.0"),
    )

    with pytest.raises(NotImplementedError):
        model.fill_market_order(limit_order, signal)
