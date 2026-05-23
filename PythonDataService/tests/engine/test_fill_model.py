"""Tests for app.engine.execution.fill_model.FillModel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import DEFERRED_FILL_MODES, FillModel
from app.engine.execution.order import (
    Direction,
    FillMode,
    Order,
    OrderType,
)

NY = ZoneInfo("America/New_York")


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


def test_signal_bar_close_stale_signal_uses_current_open_when_enabled() -> None:
    """LEAN equity market orders use current market data when a consolidator
    emits an old signal bar after a session gap. The matrix enables this
    policy for Friday-close bars that fire on Monday's first minute."""
    model = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fill_stale_signal_at_current_open=True)
    signal = TradeBar(
        symbol="SPY",
        time=datetime(2026, 1, 2, 15, 45, tzinfo=NY),
        end_time=datetime(2026, 1, 2, 16, 0, tzinfo=NY),
        open=Decimal("612.9"),
        high=Decimal("613.4"),
        low=Decimal("612.7"),
        close=Decimal("613.09"),
        volume=10_000,
    )
    current = _ny_bar(datetime(2026, 1, 5, 9, 30, tzinfo=NY), "619.32", "619.98", "618.82", "619.59")

    event = model.fill_market_order(
        _order(Direction.SHORT, quantity=-100),
        signal,
        current_bar=current,
    )

    assert event is not None
    assert event.fill_price == Decimal("619.32")
    assert event.time == datetime(2026, 1, 5, 9, 31, tzinfo=NY)


def test_signal_bar_close_stale_signal_policy_preserves_same_boundary_fill() -> None:
    model = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fill_stale_signal_at_current_open=True)
    signal = TradeBar(
        symbol="SPY",
        time=datetime(2026, 1, 2, 9, 45, tzinfo=NY),
        end_time=datetime(2026, 1, 2, 10, 0, tzinfo=NY),
        open=Decimal("100.0"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100.3"),
        volume=10_000,
    )
    current = _ny_bar(datetime(2026, 1, 2, 10, 0, tzinfo=NY), "100.4", "100.7", "100.2", "100.5")

    event = model.fill_market_order(_order(Direction.LONG), signal, current_bar=current)

    assert event is not None
    assert event.fill_price == Decimal("100.3")
    assert event.time == datetime(2026, 1, 2, 10, 0, tzinfo=NY)


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


def _ny_bar(start: datetime, open_: str, high: str, low: str, close: str) -> TradeBar:
    """Minute bar anchored to NY-local time, for date-comparison testing."""
    return TradeBar(
        symbol="AAPL",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=10_000,
    )


def test_next_session_open_defers_when_candidate_same_trading_date() -> None:
    """A candidate bar on the same NY trading date as signal_bar is ineligible.
    The model returns None so the engine's deferred-fill loop retries on a
    later bar."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 9, 30, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    same_day_candidate = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "101.0", "101.2", "100.9", "101.1")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=same_day_candidate)

    assert event is None


def test_next_session_open_fills_at_later_trading_date_open() -> None:
    """A candidate bar strictly after the signal's NY trading date fills at
    that bar's open, with fill_time = candidate.time (start of bar)."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    next_day_open = _ny_bar(datetime(2026, 2, 10, 9, 30, tzinfo=NY), "102.0", "102.5", "101.8", "102.2")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_day_open)

    assert event is not None
    assert event.fill_price == Decimal("102.0")  # next_day_open.open
    assert event.time == datetime(2026, 2, 10, 9, 30, tzinfo=NY)  # next_day_open.time (start)


def test_next_session_open_returns_none_when_next_bar_missing() -> None:
    """No candidate bar at all -> deferred (None), same as NEXT_BAR_OPEN."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN)
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=None)

    assert event is None


def test_next_session_open_applies_long_slippage() -> None:
    """Slippage in the trade direction applies to the fill price, same as
    other modes."""
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN, slippage_per_share=Decimal("0.05"))
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    next_day_open = _ny_bar(datetime(2026, 2, 10, 9, 30, tzinfo=NY), "102.0", "102.5", "101.8", "102.2")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_day_open)

    assert event is not None
    assert event.fill_price == Decimal("102.05")  # open + slippage


def test_deferred_fill_modes_membership_invariant() -> None:
    """DEFERRED_FILL_MODES contains every mode whose fill is gated on a
    subsequent candidate bar (i.e., where fill_market_order can return None).
    NEXT_BAR_OPEN and NEXT_SESSION_OPEN belong; SIGNAL_BAR_CLOSE does not.
    A regression where a new deferred-mode is added without adding it to
    this set would leave the engine main loop unable to re-try the fill."""
    assert FillMode.NEXT_BAR_OPEN in DEFERRED_FILL_MODES
    assert FillMode.NEXT_SESSION_OPEN in DEFERRED_FILL_MODES
    assert FillMode.SIGNAL_BAR_CLOSE not in DEFERRED_FILL_MODES
    # Every FillMode is either a deferred-fill mode or fills immediately.
    # If a future mode lands without classification, this assertion forces
    # an explicit decision.
    immediate_modes = {FillMode.SIGNAL_BAR_CLOSE}
    assert set(FillMode) == DEFERRED_FILL_MODES | immediate_modes
