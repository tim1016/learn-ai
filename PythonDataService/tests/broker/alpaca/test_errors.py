"""Tests for the Alpaca → contract error map (spec §9)."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    BrokerUnreachable,
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


def test_a_403_on_an_order_mutation_is_a_definitive_order_rejection_keeping_alpacas_code(
    make_api_error: ApiErrorFactory,
) -> None:
    """#2621: Alpaca answers 403 on ``POST /v2/orders`` for too little buying
    power or shares and for its wash-trade protection. That is a refusal of
    this order, never a credentials failure, and the Clerk must fold it as
    definitive -- so never the uncertain ``BrokerUnavailable`` kind."""
    error = map_api_error(
        make_api_error(403, message="insufficient buying power"),
        broker="alpaca",
        is_order_mutation=True,
    )

    assert isinstance(error, BrokerOrderRejected)
    assert not isinstance(error, BrokerUnavailable)
    assert error.message == "Alpaca refused the order: insufficient buying power"
    assert error.detail == "HTTP 403"
    assert error.code == 40010000


def test_a_403_on_a_read_stays_a_credentials_failure(make_api_error: ApiErrorFactory) -> None:
    error = map_api_error(make_api_error(403), broker="alpaca")

    assert isinstance(error, BrokerAuthError)
    assert error.message == "Alpaca rejected our credentials: denied"


def test_a_401_on_an_order_mutation_stays_a_credentials_failure(
    make_api_error: ApiErrorFactory,
) -> None:
    error = map_api_error(make_api_error(401), broker="alpaca", is_order_mutation=True)

    assert isinstance(error, BrokerAuthError)


@pytest.mark.parametrize("status", [None, 400, 401, 403, 404, 409, 422, 429, 500])
@pytest.mark.parametrize("is_order_mutation", [False, True])
def test_every_mapped_error_keeps_alpacas_numeric_code(
    make_api_error: ApiErrorFactory, status: int | None, is_order_mutation: bool
) -> None:
    error = map_api_error(make_api_error(status), broker="alpaca", is_order_mutation=is_order_mutation)

    assert error.code == 40010000


def _raw_api_error(status: int, body: str) -> APIError:
    """An ``APIError`` carrying ``body`` verbatim, the way alpaca-py raises one."""
    response = SimpleNamespace(status_code=status, headers={})
    return APIError(body, http_error=SimpleNamespace(response=response, request=None))


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>502 Bad Gateway</body></html>",
        "",
        '["not", "an", "object"]',
    ],
)
def test_a_body_that_is_not_a_json_object_keeps_its_raw_text_and_no_code(body: str) -> None:
    """alpaca-py's own ``APIError.code``/``.message`` raise on such a body; the map must not."""
    error = map_api_error(_raw_api_error(502, body), broker="alpaca")

    assert isinstance(error, BrokerUnreachable)
    assert error.message == f"Alpaca returned a server error: {body}"
    assert error.code is None


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"message": "no code here"}),
        json.dumps({"code": "40310000", "message": "no code here"}),
        json.dumps({"code": True, "message": "no code here"}),
        json.dumps({"code": None, "message": "no code here"}),
    ],
)
def test_a_body_without_an_integer_code_carries_no_code(body: str) -> None:
    error = map_api_error(_raw_api_error(403, body), broker="alpaca", is_order_mutation=True)

    assert isinstance(error, BrokerOrderRejected)
    assert error.message == "Alpaca refused the order: no code here"
    assert error.code is None


def test_a_json_body_without_a_message_falls_back_to_its_raw_text() -> None:
    body = json.dumps({"code": 40310000})

    error = map_api_error(_raw_api_error(403, body), broker="alpaca", is_order_mutation=True)

    assert error.message == f"Alpaca refused the order: {body}"
    assert error.code == 40310000


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


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_is_the_transient_kind(make_api_error: ApiErrorFactory, status: int) -> None:
    """#2582: Alpaca failing on its own side can pass, so a startup may retry it."""
    error = map_api_error(make_api_error(status), broker="alpaca")

    assert isinstance(error, BrokerUnreachable)


@pytest.mark.parametrize("status", [404, 409, None])
def test_an_unrecognized_answer_is_not_the_transient_kind(
    make_api_error: ApiErrorFactory, status: int | None
) -> None:
    """#2582: the catch-all answer a retry cannot fix never promises a reconnect."""
    error = map_api_error(make_api_error(status), broker="alpaca")

    assert isinstance(error, BrokerUnavailable)
    assert not isinstance(error, BrokerUnreachable)


def test_status_access_failure_is_not_suppressed() -> None:
    class BrokenStatusApiError(APIError):
        @property
        def status_code(self) -> int:
            raise RuntimeError("unexpected SDK status failure")

    error = BrokenStatusApiError('{"code": 1, "message": "broken"}')

    with pytest.raises(RuntimeError, match="unexpected SDK status failure"):
        status_of(error)
