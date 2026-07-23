"""Tests for the Alpaca → contract error map (spec §9)."""

from __future__ import annotations

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.errors import map_api_error, status_of
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
)
from tests.broker.alpaca.conftest import ApiErrorFactory


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
    make_api_error: ApiErrorFactory,
    status: int,
    expected: type[BrokerError],
) -> None:
    error = map_api_error(make_api_error(status), broker="alpaca")

    assert isinstance(error, expected)
    assert error.broker == "alpaca"
    assert "denied" in error.message


def test_rate_limited_parses_retry_after_seconds(make_api_error: ApiErrorFactory) -> None:
    error = map_api_error(
        make_api_error(429, headers={"Retry-After": "2"}), broker="alpaca"
    )

    assert isinstance(error, BrokerRateLimited)
    assert error.retry_after_ms == 2000


def test_rate_limited_without_header_has_no_retry_hint(make_api_error: ApiErrorFactory) -> None:
    error = map_api_error(make_api_error(429), broker="alpaca")

    assert isinstance(error, BrokerRateLimited)
    assert error.retry_after_ms is None


def test_unknown_status_defaults_to_unavailable(make_api_error: ApiErrorFactory) -> None:
    error = map_api_error(make_api_error(None), broker="alpaca")

    assert isinstance(error, BrokerUnavailable)


def test_status_access_failure_is_not_suppressed() -> None:
    class BrokenStatusApiError(APIError):
        @property
        def status_code(self) -> int:
            raise RuntimeError("unexpected SDK status failure")

    error = BrokenStatusApiError('{"code": 1, "message": "broken"}')

    with pytest.raises(RuntimeError, match="unexpected SDK status failure"):
        status_of(error)
