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


def test_conflict_on_order_mutation_maps_to_definitive_order_rejected(
    make_api_error: ApiErrorFactory,
) -> None:
    # A 409 on an order mutation (submit/cancel) is a definitive order
    # conflict, not a transient outage. It must NOT be a
    # ``BrokerUnavailable`` — otherwise the Clerk folds it into the S5
    # uncertain-lookup path instead of a clean, definitive ``SUBMIT_FAILED``.
    error = map_api_error(make_api_error(409), broker="alpaca", is_order_mutation=True)

    assert isinstance(error, BrokerOrderRejected)
    assert not isinstance(error, BrokerUnavailable)
    assert error.http_status == 409


def test_conflict_outside_order_mutation_does_not_raise_order_rejected(
    make_api_error: ApiErrorFactory,
) -> None:
    # ``BrokerOrderRejected`` is declared write-only — its own docstring
    # promises phase-1 read paths never raise it. A 409 on a read (the
    # default when ``is_order_mutation`` is omitted) must fall through to the
    # generic ``BrokerUnavailable``, not misreport a broker-read failure as an
    # order rejection.
    error = map_api_error(make_api_error(409), broker="alpaca")

    assert isinstance(error, BrokerUnavailable)
    assert not isinstance(error, BrokerOrderRejected)


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
