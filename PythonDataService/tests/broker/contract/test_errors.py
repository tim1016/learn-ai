"""Tests for the broker-neutral error taxonomy."""

from __future__ import annotations

import pytest

from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOrderRejected,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
    UnknownBrokerError,
)


@pytest.mark.parametrize(
    ("error_cls", "expected_status"),
    [
        (UnknownBrokerError, 404),
        (BrokerAuthError, 502),
        (BrokerRateLimited, 503),
        (BrokerRequestInvalid, 400),
        (BrokerOrderRejected, 409),
        (BrokerUnavailable, 503),
    ],
)
def test_http_status_mapping(error_cls: type[BrokerError], expected_status: int) -> None:
    assert error_cls.http_status == expected_status


def test_all_errors_subclass_broker_error() -> None:
    for error_cls in (
        UnknownBrokerError,
        BrokerAuthError,
        BrokerRateLimited,
        BrokerRequestInvalid,
        BrokerOrderRejected,
        BrokerUnavailable,
    ):
        assert issubclass(error_cls, BrokerError)


def test_error_carries_message_broker_and_detail() -> None:
    error = BrokerAuthError("Alpaca rejected our credentials.", broker="alpaca", detail="401")

    assert error.message == "Alpaca rejected our credentials."
    assert error.broker == "alpaca"
    assert error.detail == "401"
    assert str(error) == "Alpaca rejected our credentials."


def test_rate_limited_carries_retry_after() -> None:
    error = BrokerRateLimited("Throttled.", broker="alpaca", retry_after_ms=1500)

    assert error.retry_after_ms == 1500
    assert error.http_status == 503


def test_rate_limited_retry_after_optional() -> None:
    assert BrokerRateLimited("Throttled.").retry_after_ms is None
