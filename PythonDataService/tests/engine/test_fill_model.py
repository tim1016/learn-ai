"""Tests for app.engine.execution.fill_model.FillModel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.fill_model import FillModel
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
    assert event.filled_at_ms == signal.end_ms
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
    assert event.filled_at_ms == int(datetime(2026, 1, 5, 9, 31, tzinfo=NY).timestamp() * 1000)


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
    assert event.filled_at_ms == int(datetime(2026, 1, 2, 10, 0, tzinfo=NY).timestamp() * 1000)


def test_next_bar_open_fills_at_next_bar_open():
    model = FillModel(mode=FillMode.NEXT_BAR_OPEN)
    signal = _bar("100.0", "100.5", "99.5", "100.3")
    next_bar = _bar("100.4", "100.9", "100.1", "100.7", offset_min=1)

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_bar)

    assert event is not None
    assert event.fill_price == Decimal("100.4")
    assert event.filled_at_ms == next_bar.start_ms


@pytest.mark.parametrize(
    ("direction", "quantity", "expected_price"),
    [
        pytest.param(Direction.LONG, 100, Decimal("100.02"), id="long_slippage_adds_to_fill_price"),
        pytest.param(Direction.SHORT, -100, Decimal("99.98"), id="short_slippage_subtracts_from_fill_price"),
    ],
)
def test_slippage_moves_the_fill_price_against_the_trade_direction(
    direction: Direction, quantity: int, expected_price: Decimal
) -> None:
    model = FillModel(slippage_per_share=Decimal("0.02"))
    signal = _bar("100.0", "100.5", "99.5", "100.0")

    event = model.fill_market_order(_order(direction, quantity), signal)

    assert event is not None, "the order fills"
    assert event.fill_price == expected_price, "slippage applies against the trade direction"


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
        submitted_at_ms=signal.start_ms,
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


@pytest.mark.parametrize(
    ("mode", "signal", "next_bar"),
    [
        pytest.param(
            FillMode.NEXT_BAR_OPEN,
            _bar("100.0", "100.5", "99.5", "100.3"),
            None,
            id="next_bar_open_defers_when_next_bar_missing",
        ),
        # A candidate bar on the same NY trading date as signal_bar is ineligible.
        # The model returns None so the engine's deferred-fill loop retries on a
        # later bar.
        pytest.param(
            FillMode.NEXT_SESSION_OPEN,
            _ny_bar(datetime(2026, 2, 9, 9, 30, tzinfo=NY), "100.0", "100.5", "99.5", "100.3"),
            _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "101.0", "101.2", "100.9", "101.1"),
            id="next_session_open_defers_when_candidate_same_trading_date",
        ),
        # No candidate bar at all -> deferred (None), same as NEXT_BAR_OPEN.
        pytest.param(
            FillMode.NEXT_SESSION_OPEN,
            _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3"),
            None,
            id="next_session_open_returns_none_when_next_bar_missing",
        ),
    ],
)
def test_a_market_order_with_no_eligible_next_bar_is_deferred(
    mode: FillMode, signal: TradeBar, next_bar: TradeBar | None
) -> None:
    model = FillModel(mode=mode)

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_bar)

    assert event is None


@pytest.mark.parametrize(
    ("model_options", "expected_price"),
    [
        # A candidate bar strictly after the signal's NY trading date fills at
        # that bar's open (next_day_open.open), with fill_time = candidate.time
        # (start of bar).
        pytest.param({}, Decimal("102.0"), id="fills_at_later_trading_date_open"),
        # Slippage in the trade direction applies to the fill price (open +
        # slippage), same as other modes.
        pytest.param({"slippage_per_share": Decimal("0.05")}, Decimal("102.05"), id="applies_long_slippage"),
    ],
)
def test_next_session_open_fills_at_the_later_trading_date_open(
    model_options: dict[str, Decimal], expected_price: Decimal
) -> None:
    model = FillModel(mode=FillMode.NEXT_SESSION_OPEN, **model_options)
    signal = _ny_bar(datetime(2026, 2, 9, 15, 59, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    next_day_open = _ny_bar(datetime(2026, 2, 10, 9, 30, tzinfo=NY), "102.0", "102.5", "101.8", "102.2")

    event = model.fill_market_order(_order(Direction.LONG), signal, next_bar=next_day_open)

    assert event is not None, "the later session's bar fills the order"
    assert event.fill_price == expected_price, "the fill is at the later bar's open, slipped in the trade direction"
    assert event.filled_at_ms == int(datetime(2026, 2, 10, 9, 30, tzinfo=NY).timestamp() * 1000), (
        "the fill is stamped at the start of the candidate bar"
    )


def _order_sent_at(sent: datetime) -> Order:
    return Order(
        order_id=1,
        symbol="SPY",
        quantity=100,
        order_type=OrderType.MARKET,
        time=sent,
        direction=Direction.LONG,
    )


def test_decision_minute_open_fills_at_the_first_bar_opening_at_the_submission() -> None:
    """#2599: an order sent at the decision bar's close fills at the open of the
    minute that starts then -- the first print a live order could get -- stamped
    at that minute's start."""
    model = FillModel(mode=FillMode.DECISION_MINUTE_OPEN, slippage_per_share=Decimal("0.01"))
    decision = _ny_bar(datetime(2026, 2, 9, 9, 44, tzinfo=NY), "100.0", "100.5", "99.5", "100.3")
    emitting = _ny_bar(datetime(2026, 2, 9, 9, 45, tzinfo=NY), "100.4", "100.9", "100.2", "100.8")

    event = model.fill_market_order(_order_sent_at(datetime(2026, 2, 9, 9, 45, tzinfo=NY)), decision, next_bar=emitting)

    assert event is not None
    assert event.fill_price == Decimal("100.41")  # the candidate's open + slippage
    assert event.filled_at_ms == emitting.start_ms


def test_decision_minute_open_defers_a_bar_that_opened_before_the_order_was_sent() -> None:
    """A bar that opened before submission holds prices the order could never
    have had -- for an order decided on a minute's own close, that minute."""
    model = FillModel(mode=FillMode.DECISION_MINUTE_OPEN)
    decided_on = _ny_bar(datetime(2026, 2, 9, 9, 45, tzinfo=NY), "100.4", "100.9", "100.2", "100.8")
    after = _ny_bar(datetime(2026, 2, 9, 9, 46, tzinfo=NY), "101.0", "101.2", "100.9", "101.1")
    order = _order_sent_at(datetime(2026, 2, 9, 9, 46, tzinfo=NY))

    assert model.fill_market_order(order, decided_on, next_bar=decided_on) is None
    assert model.fill_market_order(order, decided_on, next_bar=None) is None
    event = model.fill_market_order(order, decided_on, next_bar=after)
    assert event is not None
    assert event.fill_price == Decimal("101.0")
    assert event.filled_at_ms == after.start_ms
