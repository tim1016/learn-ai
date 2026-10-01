"""Tests for the broker-neutral error taxonomy."""

from __future__ import annotations

import pytest

from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerEvidenceUnavailable,
    BrokerOrderNotPermitted,
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
        (BrokerEvidenceUnavailable, 503),
    ],
)
def test_http_status_mapping(error_cls: type[BrokerError], expected_status: int) -> None:
    assert error_cls.http_status == expected_status


def test_a_not_permitted_order_is_an_order_rejection_surfaced_as_409() -> None:
    assert issubclass(BrokerOrderNotPermitted, BrokerOrderRejected)
    assert BrokerOrderNotPermitted.http_status == 409
