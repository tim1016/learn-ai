"""Tests for app.engine.execution.portfolio.

Portfolio owns cash + holdings bookkeeping. The averaging, flip-through-zero,
and set_holdings math is subtle — each scenario here exercises a single
branch of that logic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.engine.execution.order import (
    Direction,
    OrderEvent,
    OrderType,
)
from app.engine.execution.portfolio import Portfolio, Position

NOW = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)


def _fill(quantity: int, price: str, fee: str = "0") -> OrderEvent:
    direction = Direction.LONG if quantity > 0 else Direction.SHORT
    return OrderEvent(
        order_id=1,
        symbol="SPY",
        time=NOW,
        fill_price=Decimal(price),
        fill_quantity=quantity,
        direction=direction,
        fee=Decimal(fee),
    )


def test_position_direction_reflects_sign_of_quantity():
    assert Position(symbol="SPY", quantity=10).direction == Direction.LONG
    assert Position(symbol="SPY", quantity=-10).direction == Direction.SHORT
    assert Position(symbol="SPY", quantity=0).direction == Direction.FLAT


def test_position_market_value_scales_with_quantity():
    pos = Position(symbol="SPY", quantity=100)

    assert pos.market_value(Decimal("150.0")) == Decimal("15000.0")


def test_portfolio_cash_initialized_from_initial_cash():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    assert portfolio.cash == Decimal("10000")


def test_submit_market_order_raises_on_zero_quantity():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    with pytest.raises(ValueError):
        portfolio.submit_market_order("SPY", quantity=0, time=NOW)


def test_submit_market_order_generates_incrementing_ids():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    o1 = portfolio.submit_market_order("SPY", 100, NOW)
    o2 = portfolio.submit_market_order("SPY", -50, NOW)

    assert o1.order_id == 1
    assert o2.order_id == 2
    assert o1.direction == Direction.LONG
    assert o2.direction == Direction.SHORT


def test_submit_limit_order_requires_non_zero_quantity():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    with pytest.raises(ValueError):
        portfolio.submit_limit_order("SPY", 0, NOW, limit_price=Decimal("100"))


def test_submit_limit_order_sets_order_type_limit():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    order = portfolio.submit_limit_order("SPY", 100, NOW, limit_price=Decimal("99.5"))

    assert order.order_type == OrderType.LIMIT
    assert order.limit_price == Decimal("99.5")


def test_apply_fill_opens_long_position():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    portfolio.apply_fill(_fill(100, "100.00", fee="1.00"))

    pos = portfolio.get_position("SPY")
    assert pos.quantity == 100
    assert pos.average_price == Decimal("100.00")
    assert portfolio.cash == Decimal("10000") - Decimal("100") * Decimal("100") - Decimal("1.00")
    assert portfolio.total_fees == Decimal("1.00")


def test_apply_fill_adds_to_long_position_with_weighted_average():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100.00"))
    portfolio.apply_fill(_fill(100, "110.00"))

    pos = portfolio.get_position("SPY")
    assert pos.quantity == 200
    # Weighted avg: (100*100 + 100*110) / 200 = 105
    assert pos.average_price == Decimal("105")


def test_apply_fill_reduces_long_position_without_flipping():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100.00"))
    portfolio.apply_fill(_fill(-40, "105.00"))

    pos = portfolio.get_position("SPY")
    assert pos.quantity == 60
    # Partial exits preserve the entry average price — the code path only
    # resets avg on flip-through-zero.
    assert pos.average_price == Decimal("100.00")


def test_apply_fill_flips_position_through_zero_resets_average():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100.00"))
    portfolio.apply_fill(_fill(-150, "110.00"))  # flips long→short

    pos = portfolio.get_position("SPY")
    assert pos.quantity == -50
    assert pos.average_price == Decimal("110.00")


def test_apply_fill_closing_to_zero_resets_average():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100.00"))
    portfolio.apply_fill(_fill(-100, "110.00"))

    pos = portfolio.get_position("SPY")
    assert pos.quantity == 0
    assert pos.average_price == Decimal("0")


def test_total_value_includes_marked_to_market_positions():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100.00"))
    portfolio.update_reference_price("SPY", Decimal("105"))

    # Cash is 10000 - 100*100 = 0; position marked at 105 * 100 = 10500.
    assert portfolio.total_value() == Decimal("10500")


def test_set_holdings_raises_without_reference_price():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    with pytest.raises(RuntimeError):
        portfolio.set_holdings("SPY", target_fraction=0.5, time=NOW)


def test_set_holdings_submits_order_to_reach_target_fraction():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.update_reference_price("SPY", Decimal("100"))

    # Target 50% of 10000 = 5000 → 50 shares at $100.
    order = portfolio.set_holdings("SPY", target_fraction=0.5, time=NOW)

    assert order is not None
    assert order.quantity == 50
    assert order.tag == "SetHoldings"


def test_set_holdings_noop_when_already_at_target():
    # After filling 100 shares at $100, cash=0 and position value=$10000, so
    # total_value=$10000. Target 100% = $10000 = 100 shares; delta=0 → noop.
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.update_reference_price("SPY", Decimal("100"))
    portfolio.apply_fill(_fill(100, "100"))

    order = portfolio.set_holdings("SPY", target_fraction=Decimal("1.0"), time=NOW)

    assert order is None


def test_liquidate_noop_when_flat():
    portfolio = Portfolio(initial_cash=Decimal("10000"))

    assert portfolio.liquidate("SPY", NOW) is None


def test_liquidate_submits_offsetting_order():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.apply_fill(_fill(100, "100"))

    order = portfolio.liquidate("SPY", NOW)

    assert order is not None
    assert order.quantity == -100
    assert order.tag == "Liquidate"


def test_drain_pending_clears_pending_list():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.submit_market_order("SPY", 100, NOW)
    portfolio.submit_market_order("SPY", -100, NOW)

    drained = list(portfolio.drain_pending())

    assert len(drained) == 2
    assert portfolio.pending_orders == []


def test_clear_pending_drops_pending_list():
    portfolio = Portfolio(initial_cash=Decimal("10000"))
    portfolio.submit_market_order("SPY", 100, NOW)

    portfolio.clear_pending()

    assert portfolio.pending_orders == []
