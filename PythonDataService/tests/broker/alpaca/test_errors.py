"""Tests for the Alpaca → contract error map (spec §9)."""

from __future__ import annotations

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.errors import map_api_error, status_of
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerError,
    BrokerOrderRejected,
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
        (409, BrokerOrderRejected),
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


def test_conflict_maps_to_definitive_order_rejected_not_unavailable(
    make_api_error: ApiErrorFactory,
) -> None:
    # A 409 is a definitive order conflict (duplicate client_order_id, order
    # state conflict), not a transient outage. It must NOT be a
    # ``BrokerUnavailable`` — otherwise the Clerk folds it into the S5
    # uncertain-lookup path instead of a clean, definitive ``SUBMIT_FAILED``.
    error = map_api_error(make_api_error(409), broker="alpaca")

    assert isinstance(error, BrokerOrderRejected)
    assert not isinstance(error, BrokerUnavailable)
    assert error.http_status == 409


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
