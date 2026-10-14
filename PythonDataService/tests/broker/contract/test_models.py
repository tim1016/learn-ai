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


def test_limit_order_without_price_is_rejected() -> None:
    with pytest.raises(ValidationError, match="limit order requires a limit_price"):
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, order_type="limit")


def test_market_order_with_price_is_rejected() -> None:
    with pytest.raises(ValidationError, match="market order must not carry a limit_price"):
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1, limit_price=100.0)


def test_non_positive_limit_price_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BrokerOrderLeg(
            symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=0
        )


@pytest.mark.parametrize("limit_price", [240.555, 0.12345])
def test_limit_price_with_too_many_decimal_places_is_rejected(limit_price: float) -> None:
    with pytest.raises(ValidationError, match="limit prices"):
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
