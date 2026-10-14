"""Tests for the broker-neutral error taxonomy."""

from __future__ import annotations

from app.broker.contract.errors import (
    BrokerOrderNotPermitted,
    BrokerOrderRejected,
)


def test_a_not_permitted_order_is_an_order_rejection_surfaced_as_409() -> None:
    assert issubclass(BrokerOrderNotPermitted, BrokerOrderRejected)
    assert BrokerOrderNotPermitted.http_status == 409
