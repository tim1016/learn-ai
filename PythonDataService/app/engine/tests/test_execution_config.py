"""Unit tests for ExecutionConfig → FillModel wiring.

PR 1 of the execution-realism roadmap. Proves that slippage and
commission, when set on the config, actually affect the OrderEvent that
the engine would apply to the portfolio — i.e. that a realistic-costs
run differs from a zero-costs (bit-exact LEAN) run by exactly the
expected amount.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution import (
    Direction,
    ExecutionConfig,
    FillMode,
    Order,
    OrderType,
)


def _bar(close: str) -> TradeBar:
    start = datetime(2024, 1, 2, 14, 45, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1_000_000,
    )


def _market_order(direction: Direction, quantity: int = 100) -> Order:
    return Order(
        order_id=1,
        symbol="SPY",
        quantity=quantity,
        order_type=OrderType.MARKET,
        time=datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc),
        direction=direction,
    )


def test_defaults_preserve_lean_parity():
    """Zero-arg ExecutionConfig must match FillModel's zero-arg defaults
    exactly — otherwise bit-exact LEAN parity breaks for runs that
    supply no overrides."""
    fill_model = ExecutionConfig().build_fill_model()

    assert fill_model.mode is FillMode.SIGNAL_BAR_CLOSE
    assert fill_model.commission_per_order == Decimal("1.00")
    assert fill_model.slippage_per_share == Decimal(0)


def test_long_market_fill_applies_slippage_up_and_commission():
    """2 ticks of slippage on a long MARKET order must push the fill
    price UP by 0.02 and charge the configured commission as the fee."""
    config = ExecutionConfig(
        commission_per_order=Decimal("1.50"),
        slippage_per_share=Decimal("0.02"),
    )
    fill_model = config.build_fill_model()
    signal_bar = _bar("500.00")

    event = fill_model.fill_market_order(_market_order(Direction.LONG), signal_bar)

    assert event is not None
    assert event.fill_price == Decimal("500.02")
    assert event.fee == Decimal("1.50")
    assert event.fill_quantity == 100
    assert event.direction is Direction.LONG


def test_short_market_fill_applies_slippage_down():
    """A short fill pays slippage against its direction too — the fill
    price drops by the slippage amount."""
    config = ExecutionConfig(slippage_per_share=Decimal("0.02"))
    fill_model = config.build_fill_model()
    signal_bar = _bar("500.00")

    event = fill_model.fill_market_order(_market_order(Direction.SHORT), signal_bar)

    assert event is not None
    assert event.fill_price == Decimal("499.98")


def test_slippage_and_commission_change_pnl_versus_zero_costs():
    """Round-trip comparison: a long entry + flat exit at the same
    reference price produces zero PnL under zero costs, but loses
    (2 × slippage × quantity) + (2 × commission) under realistic costs.
    This is the end-to-end proof the PR is supposed to deliver."""
    entry_bar = _bar("500.00")
    exit_bar = _bar("500.00")

    zero_costs = ExecutionConfig(
        commission_per_order=Decimal(0),
        slippage_per_share=Decimal(0),
    ).build_fill_model()
    realistic = ExecutionConfig(
        commission_per_order=Decimal("1.00"),
        slippage_per_share=Decimal("0.02"),
    ).build_fill_model()

    quantity = 100
    entry = _market_order(Direction.LONG, quantity)
    exit_ = _market_order(Direction.SHORT, quantity)

    def round_trip_pnl(model) -> Decimal:
        entry_event = model.fill_market_order(entry, entry_bar)
        exit_event = model.fill_market_order(exit_, exit_bar)
        assert entry_event is not None and exit_event is not None
        gross = (exit_event.fill_price - entry_event.fill_price) * quantity
        return gross - entry_event.fee - exit_event.fee

    assert round_trip_pnl(zero_costs) == Decimal(0)

    # Realistic run pays: 0.02 × 100 on entry (price up), 0.02 × 100 on
    # exit (price down) = Decimal("-4.00") on slippage, plus two $1.00
    # commissions = -$6.00 total.
    assert round_trip_pnl(realistic) == Decimal("-6.00")


@pytest.mark.parametrize("fill_mode", [FillMode.SIGNAL_BAR_CLOSE, FillMode.NEXT_BAR_OPEN])
def test_fill_mode_propagates_through_config(fill_mode: FillMode):
    fill_model = ExecutionConfig(fill_mode=fill_mode).build_fill_model()
    assert fill_model.mode is fill_mode
