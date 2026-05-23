"""FillModel.compute_fee is the single seam through which the engine
charges commission. With ``fee_model=None`` it falls back to the legacy
flat ``commission_per_order`` (so historical SPY parity runs stay byte-
identical). With ``fee_model=IbkrEquityCommissionModel()`` it returns the
per-fill IBKR fee — this is the path the cross-engine matrix uses."""

from __future__ import annotations

from decimal import Decimal

from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.fill_model import FillModel


def test_compute_fee_default_returns_flat_commission() -> None:
    model = FillModel()
    assert model.commission_per_order == Decimal("1.00")
    assert model.compute_fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")
    # Flat regardless of quantity/price when fee_model is None.
    assert model.compute_fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.00")


def test_compute_fee_with_ibkr_model_returns_per_fill_fee() -> None:
    model = FillModel(fee_model=IbkrEquityCommissionModel())
    # 150 @ $662.50 → $0.75 raw, floored to $1.00.
    assert model.compute_fee(quantity=150, fill_price=Decimal("662.50")) == Decimal("1.00")
    # 365 @ $270 → $1.83 per-share rate.
    assert model.compute_fee(quantity=365, fill_price=Decimal("270.00")) == Decimal("1.83")
    # 221 @ $450 → $1.11 per-share rate.
    assert model.compute_fee(quantity=221, fill_price=Decimal("450.00")) == Decimal("1.11")


def test_fill_market_order_uses_compute_fee_with_fee_model() -> None:
    """The OrderEvent's fee must come from compute_fee, not commission_per_order."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.engine.data.trade_bar import TradeBar
    from app.engine.execution.order import Direction, FillMode, Order, OrderType

    ny = ZoneInfo("America/New_York")
    bar = TradeBar(
        symbol="AAPL",
        time=datetime(2026, 1, 5, 9, 30, tzinfo=ny),
        end_time=datetime(2026, 1, 5, 9, 45, tzinfo=ny),
        open=Decimal("269.50"),
        high=Decimal("270.50"),
        low=Decimal("269.00"),
        close=Decimal("270.00"),
        volume=10_000,
    )
    order = Order(
        order_id=1,
        symbol="AAPL",
        order_type=OrderType.MARKET,
        time=bar.time,
        direction=Direction.LONG,
        quantity=365,
        tag="ENTER",
    )
    fm = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fee_model=IbkrEquityCommissionModel())
    event = fm.fill_market_order(order, bar)
    assert event is not None
    assert event.fee == Decimal("1.83")
