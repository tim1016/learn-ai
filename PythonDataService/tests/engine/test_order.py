"""Tests for app.engine.execution.order enums and dataclasses."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.engine.execution.order import (
    Direction,
    FillMode,
    Order,
    OrderEvent,
    OrderType,
)


def test_direction_enum_values():
    assert Direction.LONG.value == 1
    assert Direction.FLAT.value == 0
    assert Direction.SHORT.value == -1


def test_order_type_enum_values():
    assert OrderType.MARKET.value == "market"
    assert OrderType.LIMIT.value == "limit"
    assert OrderType.STOP_MARKET.value == "stop_market"


def test_fill_mode_enum_values():
    assert FillMode.SIGNAL_BAR_CLOSE.value == "signal_bar_close"
    assert FillMode.NEXT_BAR_OPEN.value == "next_bar_open"


def test_order_defaults():
    now = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)
    order = Order(
        order_id=1,
        symbol="SPY",
        quantity=100,
        order_type=OrderType.MARKET,
        time=now,
        direction=Direction.LONG,
    )

    assert order.tag == ""
    assert order.limit_price is None
    assert order.stop_price is None
    assert order.take_profit_price is None
    assert order.stop_loss_price is None


def test_order_accepts_brackets():
    now = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)
    order = Order(
        order_id=2,
        symbol="SPY",
        quantity=-100,
        order_type=OrderType.MARKET,
        time=now,
        direction=Direction.SHORT,
        take_profit_price=Decimal("95.00"),
        stop_loss_price=Decimal("105.00"),
        tag="bracketed-short",
    )

    assert order.take_profit_price == Decimal("95.00")
    assert order.stop_loss_price == Decimal("105.00")
    assert order.tag == "bracketed-short"


def test_order_event_fields():
    now = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)
    event = OrderEvent(
        order_id=7,
        symbol="SPY",
        time=now,
        fill_price=Decimal("100.00"),
        fill_quantity=100,
        direction=Direction.LONG,
        fee=Decimal("1.00"),
        tag="entry",
    )

    assert event.order_id == 7
    assert event.fee == Decimal("1.00")
    assert event.tag == "entry"


def test_next_session_open_is_a_known_fill_mode() -> None:
    """NEXT_SESSION_OPEN exists and has the canonical string value the runner
    will normalize to. The string value is what RunRequest.fill_mode carries
    and what ledger persistence stores; renaming it breaks every prior run."""
    assert FillMode.NEXT_SESSION_OPEN.value == "next_session_open"
