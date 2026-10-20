"""Tests for the broker-neutral contract models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.broker.contract.models import (
    BrokerOrderLeg,
    TimeInForce,
)


@pytest.mark.parametrize("symbol", ["BTC/USD", "AAPL240621C00200000", "spy"])
def test_order_leg_rejects_non_equity_symbol_shape(symbol: str) -> None:
    """S1 cannot submit crypto pairs or OCC option identifiers as equity legs."""
    with pytest.raises(ValidationError):
        BrokerOrderLeg(symbol=symbol, side="buy", quantity=1)


@pytest.mark.parametrize(
    ("terms", "message"),
    [
        pytest.param({"order_type": "limit"}, "limit order requires a limit_price", id="limit_order_without_price"),
        pytest.param({"limit_price": 100.0}, "market order must not carry a limit_price", id="market_order_with_price"),
    ],
)
def test_a_limit_order_needs_its_price_and_a_market_order_carries_none(terms: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, **terms)


@pytest.mark.parametrize(
    ("limit_price", "message"),
    [
        pytest.param(0, None, id="non_positive_limit_price"),
        pytest.param(240.555, "limit prices", id="too_many_decimal_places_240.555"),
        pytest.param(0.12345, "limit prices", id="too_many_decimal_places_0.12345"),
    ],
)
def test_a_limit_price_is_above_zero_with_no_more_decimals_than_alpaca_allows(
    limit_price: float, message: str | None
) -> None:
    with pytest.raises(ValidationError, match=message):
        BrokerOrderLeg(
            symbol="SPY",
            side="buy",
            quantity=1,
            order_type="limit",
            limit_price=limit_price,
        )


def test_fractional_gtc_leg_is_rejected() -> None:
    with pytest.raises(ValidationError, match="fractional-share orders"):
        BrokerOrderLeg(
            symbol="SPY",
            side="buy",
            quantity=0.5,
            order_type="limit",
            limit_price=240.5,
            time_in_force="gtc",
        )


def test_fractional_day_leg_is_accepted() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY",
        side="buy",
        quantity=0.5,
        order_type="limit",
        limit_price=240.5,
        time_in_force="day",
    )

    assert leg.quantity == 0.5


def test_extended_hours_leg_requires_a_day_or_gtc_limit() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=100.25, extended_hours=True
    )

    assert leg.extended_hours is True
    assert leg.time_in_force is TimeInForce.DAY


def test_extended_hours_market_leg_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extended-hours orders must be limit orders"):
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, extended_hours=True)
