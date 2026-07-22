"""Tests for the Alpaca → contract error map (spec §9)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.errors import map_api_error
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, BrokerAuthError),
        (403, BrokerAuthError),
        (429, BrokerRateLimited),
        (400, BrokerRequestInvalid),
        (422, BrokerRequestInvalid),
        (500, BrokerUnavailable),
        (503, BrokerUnavailable),
    ],
)
def test_status_maps_to_contract_error(
    make_api_error, status: int, expected: type[BrokerError]
) -> None:
    error = map_api_error(make_api_error(status), broker="alpaca")

    assert isinstance(error, expected)
    assert error.broker == "alpaca"
    assert "denied" in error.message


def test_rate_limited_parses_retry_after_seconds(make_api_error) -> None:
    error = map_api_error(
        make_api_error(429, headers={"Retry-After": "2"}), broker="alpaca"
    )

    assert isinstance(error, BrokerRateLimited)
    assert error.retry_after_ms == 2000


def test_rate_limited_without_header_has_no_retry_hint(make_api_error) -> None:
    error = map_api_error(make_api_error(429), broker="alpaca")

    assert isinstance(error, BrokerRateLimited)
    assert error.retry_after_ms is None


def test_unknown_status_defaults_to_unavailable(make_api_error) -> None:
    error = map_api_error(make_api_error(None), broker="alpaca")

    assert isinstance(error, BrokerUnavailable)
