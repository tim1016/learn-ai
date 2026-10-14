"""Tests for the async AlpacaTradingClient wrapper.

The SDK is faked via ``client_factory`` — these tests assert the wrapper's
behavior (raw passthrough, request building, error translation), not alpaca-py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from http.client import RemoteDisconnected
from pathlib import Path
from threading import Event
from typing import Any

import anyio
import pytest
import responses
from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, QueryOrderStatus
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.sessions import Session
from urllib3.exceptions import ProtocolError

from app.broker.alpaca.clerk.sqlite.repository import DEFAULT_CLAIM_TTL_MS
from app.broker.alpaca.client import (
    _DEFAULT_TIMEOUT_S,
    _MAX_RATE_LIMIT_RETRIES,
    _RATE_LIMIT_RETRY_CAP_S,
    AlpacaTradingClient,
    _install_session_timeout,
    _install_stale_connection_retry,
)
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerOrderAnswerUnreadable,
    BrokerOrderNotPermitted,
    BrokerOrderRejected,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
    BrokerUnreachable,
)
from tests.broker.alpaca.conftest import ApiErrorFactory


class _FakeAlpaca:
    """A stand-in alpaca-py client returning canned raw payloads."""

    def __init__(self) -> None:
        self.orders_filter: Any = None
        self.assets_filter: Any = None
        self.activities_call: Any = None
        self.post_call: Any = None
        self.delete_call: Any = None
        self.lookup_call: Any = None
        self.asset_lookup_call: Any = None

    def get_account(self) -> dict:
        return {"account_number": "PA1", "status": "ACTIVE"}

    def get_all_positions(self) -> list[dict]:
        return [{"symbol": "AAPL"}]

    def get_orders(self, filter: Any = None) -> list[dict]:
        self.orders_filter = filter
        return [{"id": "o1"}]

    def get_all_assets(self, filter: Any = None) -> list[dict]:
        self.assets_filter = filter
        return [{"id": "a1"}, {"id": "a2"}]

    def get_asset(self, symbol_or_asset_id: str) -> dict[str, Any]:
        self.asset_lookup_call = symbol_or_asset_id
        return {"id": "a1", "symbol": symbol_or_asset_id}

    def get(self, path: str, data: Any = None) -> list[dict]:
        self.activities_call = (path, data)
        return [{"id": "act1"}]

    def get_clock(self) -> dict:
        return {"is_open": True}

    def post(self, path: str, data: Any = None) -> dict:
        self.post_call = (path, data)
        return {"id": "broker-order-1", "status": "accepted"}

    def delete(self, path: str, data: Any = None) -> None:
        # Alpaca's cancel returns HTTP 204 (no body); the SDK yields ``None``.
        self.delete_call = (path, data)
        return None

    def get_order_by_client_id(self, client_id: str) -> dict:
        self.lookup_call = client_id
        return {"id": "broker-order-1", "client_order_id": client_id, "status": "accepted"}

    def get_order_by_id(self, order_id: str) -> dict:
        self.broker_id_lookup_call = order_id
        return {"id": order_id, "client_order_id": "alpaca-generated", "status": "new"}


def _client(fake: _FakeAlpaca) -> AlpacaTradingClient:
    return AlpacaTradingClient(client_factory=lambda: fake)


# An Alpaca order id is a UUID; a cancel is refused for anything else (#3216).
_BROKER_ORDER_ID = "00000000-0000-0000-0000-0000000000a1"


def test_client_can_be_constructed_outside_async_context() -> None:
    client = _client(_FakeAlpaca())

    assert client.broker_id == "alpaca"


async def test_list_orders_builds_filter() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_orders(status="open", limit=5, after_ms=1_700_000_000_000)

    assert fake.orders_filter.status == QueryOrderStatus.OPEN
    assert fake.orders_filter.limit == 5
    assert fake.orders_filter.after is not None
    assert fake.orders_filter.direction == Sort.DESC


async def test_list_orders_narrows_to_a_symbol_and_a_window_for_paging() -> None:
    """One symbol's orders between two times: what a caller pages a long order history with (#2854)."""
    fake = _FakeAlpaca()
    await _client(fake).list_orders(
        status="all", limit=500, after_ms=1_700_000_000_000, symbols=("SPY",), until_ms=1_700_000_600_000
    )

    assert fake.orders_filter.status == QueryOrderStatus.ALL
    assert fake.orders_filter.symbols == ["SPY"]
    assert fake.orders_filter.after == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert fake.orders_filter.until == datetime.fromtimestamp(1_700_000_600, tz=UTC)
    assert fake.orders_filter.direction == Sort.DESC


async def test_list_assets_builds_status_filter_and_caps_response() -> None:
    fake = _FakeAlpaca()
    payloads = await _client(fake).list_assets(status="active", limit=1)

    assert fake.assets_filter.status == AssetStatus.ACTIVE
    assert payloads == [{"id": "a1"}]


async def test_get_asset_uses_the_symbol_specific_sdk_lookup() -> None:
    fake = _FakeAlpaca()

    payload = await _client(fake).get_asset("SPY")

    assert fake.asset_lookup_call == "SPY"
    assert fake.assets_filter is None
    assert payload == {"id": "a1", "symbol": "SPY"}


async def test_get_asset_404_returns_none(make_api_error: ApiErrorFactory) -> None:
    fake = _FakeAlpaca()

    def raise_not_found(symbol_or_asset_id: str) -> dict[str, Any]:
        raise make_api_error(404, message=f"{symbol_or_asset_id} not found")

    fake.get_asset = raise_not_found  # type: ignore[method-assign]

    assert await _client(fake).get_asset("MISSING") is None


async def test_list_activities_calls_low_level_endpoint() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_activities(limit=25)

    path, data = fake.activities_call
    assert path == "/account/activities"
    assert data == {"page_size": 25, "direction": "desc"}


async def test_list_activities_passes_page_token_to_low_level_endpoint() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_activities(limit=25, page_token="activity-25")

    assert fake.activities_call == (
        "/account/activities",
        {"page_size": 25, "direction": "desc", "page_token": "activity-25"},
    )


async def test_list_activities_can_request_the_cash_transaction_group() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_activities(limit=100, activity_type="TRANS")

    assert fake.activities_call == (
        "/account/activities/TRANS",
        {"page_size": 100, "direction": "desc"},
    )


async def test_non_trade_activities_are_asked_for_by_category_and_never_by_an_activity_code() -> None:
    """Alpaca's pages spell the option exercise two ways; the category names neither (#2913)."""
    fake = _FakeAlpaca()
    payload = await _client(fake).list_non_trade_activities(limit=100)

    assert fake.activities_call == (
        "/account/activities",
        {"page_size": 100, "direction": "desc", "category": "non_trade_activity"},
    )
    assert payload == [{"id": "act1"}]


async def test_a_non_trade_activity_walk_continues_from_its_page_token() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_non_trade_activities(limit=100, page_token="20240301000000000::fee-0099")

    assert fake.activities_call == (
        "/account/activities",
        {
            "page_size": 100,
            "direction": "desc",
            "category": "non_trade_activity",
            "page_token": "20240301000000000::fee-0099",
        },
    )


async def test_submit_order_posts_to_orders_endpoint_and_returns_raw() -> None:
    fake = _FakeAlpaca()
    body = {"symbol": "SPY", "qty": "1", "side": "buy", "type": "market"}

    payload = await _client(fake).submit_order(body)

    assert fake.post_call == ("/orders", body)
    assert payload == {"id": "broker-order-1", "status": "accepted"}


@pytest.mark.parametrize("body", ["", "null"])
@responses.activate
async def test_empty_successful_reference_and_submit_answers_stay_unknown(body: str) -> None:
    sdk = TradingClient(api_key="test-key", secret_key="test-secret", paper=True, raw_data=True)
    client = AlpacaTradingClient(client_factory=lambda: sdk)
    base = "https://paper-api.alpaca.markets/v2"
    responses.add(responses.GET, f"{base}/assets/SPY", body=body, status=200)
    responses.add(responses.GET, f"{base}/options/contracts/SPY261016C00600000", body=body, status=200)
    responses.add(responses.POST, f"{base}/orders", body=body, status=200)

    with pytest.raises(BrokerUnavailable):
        await client.get_asset("SPY")
    with pytest.raises(BrokerUnavailable):
        await client.get_option_contract("SPY261016C00600000")
    with pytest.raises(BrokerUnavailable):
        await client.submit_order(_ORDER_BODY)
    assert client._submission_may_become_visible(_ORDER_BODY["client_order_id"])


# A 2xx whose JSON is not an order object: a list, a string, a number, a boolean.
_NON_OBJECT_ANSWERS = ["[]", '[{"id": "o1"}]', '"accepted"', "7", "true"]


@pytest.mark.parametrize("body", _NON_OBJECT_ANSWERS)
@responses.activate
async def test_a_non_object_2xx_answer_to_an_order_submission_stays_unknown_inside_the_window(body: str) -> None:
    """The order may well have been placed, so the answer is unknown (#3216).

    It was handed on as an order and reached the adapter, which raised a raw
    ``TypeError`` after this client had already closed the visibility window
    on the 200: neither a rejection the Clerk could fold nor an unknown one.
    """
    sdk = TradingClient(api_key="test-key", secret_key="test-secret", paper=True, raw_data=True)
    client = AlpacaTradingClient(client_factory=lambda: sdk)
    responses.add(responses.POST, "https://paper-api.alpaca.markets/v2/orders", body=body, status=200)

    with pytest.raises(BrokerOrderAnswerUnreadable) as raised:
        await client.submit_order(_ORDER_BODY)

    # The order is sent once, and the answer is the unreadable-answer kind the port's contract names:
    # a ``BrokerUnavailable`` every catch arm already holds, with no broker order id to hand on.
    assert len(responses.calls) == 1
    assert isinstance(raised.value, BrokerUnavailable)
    assert not isinstance(raised.value, BrokerUnreachable)
    assert raised.value.broker_order_id is None
    assert client._submission_may_become_visible(_ORDER_BODY["client_order_id"])


@pytest.mark.parametrize(
    "answer",
    [{}, {"client_order_id": "manual/inkant/v1:someone-else"}, {"client_order_id": None}],
    ids=["empty-object", "another-client-order-id", "null-client-order-id"],
)
async def test_a_2xx_submission_answer_that_does_not_name_the_order_sent_keeps_the_window_open(
    make_api_error: ApiErrorFactory, answer: dict[str, Any]
) -> None:
    """Only an answer that names the order closes the visibility window (#3216).

    The Clerk folds such an answer as unknown and looks the order up at
    once; a lookup index still catching up answers that with a 404, which
    must not read as "absent".
    """
    fake = _FakeAlpaca()
    fake.post = lambda path, data=None: answer  # type: ignore[method-assign]

    def lookup(client_id: str) -> dict[str, Any]:
        raise make_api_error(404, message="order not found")

    fake.get_order_by_client_id = lookup  # type: ignore[method-assign]
    client = _client(fake)

    returned = await client.submit_order(_ORDER_BODY)

    # Handed on for the caller to judge, as it was: the Clerk's own fold of another id's answer reads it.
    assert returned == answer
    assert client._submission_may_become_visible(_ORDER_BODY["client_order_id"])
    with pytest.raises(BrokerUnavailable, match="still become visible"):
        await client.get_order_by_client_order_id(_ORDER_BODY["client_order_id"])


@pytest.mark.parametrize("names_the_order", [True, False], ids=["names-the-order", "carries-no-client-order-id"])
async def test_a_2xx_submission_answer_that_names_the_order_or_carries_no_name_closes_the_window(
    names_the_order: bool,
) -> None:
    answer = {"id": "broker-order-1", **({"client_order_id": _ORDER_BODY["client_order_id"]} if names_the_order else {})}
    fake = _FakeAlpaca()
    fake.post = lambda path, data=None: answer  # type: ignore[method-assign]
    client = _client(fake)
    client.hold_submission_uncertain(_ORDER_BODY["client_order_id"])

    await client.submit_order(_ORDER_BODY)

    assert not client._submission_may_become_visible(_ORDER_BODY["client_order_id"])


@pytest.mark.parametrize("body", _NON_OBJECT_ANSWERS)
@responses.activate
async def test_a_non_object_2xx_answer_to_an_order_lookup_is_unknown_not_absent(body: str) -> None:
    sdk = TradingClient(api_key="test-key", secret_key="test-secret", paper=True, raw_data=True)
    client = AlpacaTradingClient(client_factory=lambda: sdk)
    base = "https://paper-api.alpaca.markets/v2"
    responses.add(responses.GET, f"{base}/orders:by_client_order_id", body=body, status=200)
    responses.add(responses.GET, f"{base}/orders/{_BROKER_ORDER_ID}", body=body, status=200)

    for lookup in (
        client.get_order_by_client_order_id("manual/inkant/v1:abc"),
        client.get_order_by_broker_order_id(_BROKER_ORDER_ID),
        client.get_nested_order_by_broker_order_id(_BROKER_ORDER_ID),
    ):
        with pytest.raises(BrokerOrderAnswerUnreadable, match="could not be read") as unreadable:
            await lookup
        assert unreadable.value.broker_order_id is None


async def test_a_not_found_after_a_non_object_lookup_answer_is_still_not_absent(
    make_api_error: ApiErrorFactory,
) -> None:
    """Alpaca answered for the order; its not-found a moment later proves nothing (#2913, #3216)."""
    fake = _FakeAlpaca()
    answers: list[object] = []

    def answer(*_args: object, **_kwargs: object) -> object:
        if answers:
            return answers.pop()
        raise make_api_error(404, message="order not found")

    fake.get_order_by_client_id = answer  # type: ignore[method-assign]
    fake.get_order_by_id = answer  # type: ignore[method-assign]
    client = _client(fake)

    answers.append([])
    with pytest.raises(BrokerUnavailable, match="could not be read"):
        await client.get_order_by_client_order_id("manual/inkant/v1:abc")
    with pytest.raises(BrokerUnavailable, match="still become visible"):
        await client.get_order_by_client_order_id("manual/inkant/v1:abc")

    answers.append("accepted")
    with pytest.raises(BrokerUnavailable, match="could not be read"):
        await client.get_order_by_broker_order_id(_BROKER_ORDER_ID)
    with pytest.raises(BrokerUnavailable, match="a moment ago"):
        await client.get_order_by_broker_order_id(_BROKER_ORDER_ID)


async def test_cancel_order_deletes_by_id_and_returns_none() -> None:
    fake = _FakeAlpaca()

    result = await _client(fake).cancel_order(_BROKER_ORDER_ID)

    assert fake.delete_call == (f"/orders/{_BROKER_ORDER_ID}", None)
    assert result is None


@pytest.mark.parametrize(
    "spelling",
    [
        _BROKER_ORDER_ID.upper(),
        _BROKER_ORDER_ID.replace("-", ""),
        "{" + _BROKER_ORDER_ID + "}",
        "urn:uuid:" + _BROKER_ORDER_ID,
    ],
    ids=["upper-case", "no-hyphens", "braces", "urn"],
)
async def test_cancel_order_sends_the_canonical_spelling_of_the_id(spelling: str) -> None:
    """The id goes into the URL path, so every spelling of one UUID is sent as its one canonical form (#3216)."""
    fake = _FakeAlpaca()

    await _client(fake).cancel_order(spelling)

    assert fake.delete_call == (f"/orders/{_BROKER_ORDER_ID}", None)


@pytest.mark.parametrize(
    "not_an_order_id",
    [
        "../positions",
        "order-xyz",
        "",
        "00000000-0000-0000-0000-00000000000",
        _BROKER_ORDER_ID + "/../../positions",
    ],
    ids=["dot-segments", "not-a-uuid", "empty", "one-digit-short", "uuid-then-dot-segments"],
)
async def test_cancel_order_refuses_an_id_that_is_not_a_uuid_before_any_request(not_an_order_id: str) -> None:
    """``requests`` resolves dot-segments, so ``../positions`` was ``DELETE /v2/positions`` (#3216).

    That is Alpaca's close-every-position request. The cancel is held to the
    same UUID gate the order reads are (#2854): refused as a request Alpaca
    could not take, with nothing asked of it.
    """
    fake = _FakeAlpaca()

    with pytest.raises(BrokerRequestInvalid, match="is not an Alpaca order id"):
        await _client(fake).cancel_order(not_an_order_id)

    assert fake.delete_call is None


@responses.activate
async def test_a_cancel_of_a_path_like_id_sends_no_request_through_the_real_sdk() -> None:
    """Over the real SDK and ``requests``: nothing reaches the network, least of all ``DELETE /v2/positions``."""
    sdk = TradingClient(api_key="test-key", secret_key="test-secret", paper=True, raw_data=True)
    base = "https://paper-api.alpaca.markets/v2"
    responses.add(responses.DELETE, f"{base}/positions", json=[], status=207)

    with pytest.raises(BrokerRequestInvalid):
        await AlpacaTradingClient(client_factory=lambda: sdk).cancel_order("../positions")

    assert len(responses.calls) == 0


async def test_cancel_order_non_cancelable_maps_to_contract_error(
    make_api_error: ApiErrorFactory,
) -> None:
    fake = _FakeAlpaca()

    def raise_unprocessable(path: str, data: Any = None) -> None:
        raise make_api_error(422, message="order is not cancelable")

    fake.delete = raise_unprocessable  # type: ignore[method-assign]

    with pytest.raises(BrokerRequestInvalid) as excinfo:
        await _client(fake).cancel_order(_BROKER_ORDER_ID)
    assert "not cancelable" in excinfo.value.message


async def test_a_403_on_order_submission_is_alpaca_refusing_the_order(
    make_api_error: ApiErrorFactory,
) -> None:
    """#2621: ``submit_order`` maps its 403 as a refusal of the new order, keeping Alpaca's code."""
    fake = _FakeAlpaca()

    def refuse(path: str, data: Any = None) -> dict:
        raise make_api_error(403, message="potential wash trade detected. use complex orders")

    fake.post = refuse  # type: ignore[method-assign]

    with pytest.raises(BrokerOrderNotPermitted) as excinfo:
        await _client(fake).submit_order(_ORDER_BODY)
    assert excinfo.value.code == 40010000


async def test_a_403_on_order_cancel_stays_a_credentials_failure(
    make_api_error: ApiErrorFactory,
) -> None:
    """Alpaca documents no 403 on a cancel, so ``cancel_order`` never reads one as an order refusal."""
    fake = _FakeAlpaca()

    def forbid(path: str, data: Any = None) -> None:
        raise make_api_error(403)

    fake.delete = forbid  # type: ignore[method-assign]

    with pytest.raises(BrokerAuthError):
        await _client(fake).cancel_order(_BROKER_ORDER_ID)


async def test_get_order_by_client_order_id_returns_raw_payload() -> None:
    fake = _FakeAlpaca()

    payload = await _client(fake).get_order_by_client_order_id("manual/inkant/v1:abc")

    assert fake.lookup_call == "manual/inkant/v1:abc"
    assert payload == {
        "id": "broker-order-1",
        "client_order_id": "manual/inkant/v1:abc",
        "status": "accepted",
    }


async def test_get_order_by_client_order_id_404_returns_none(
    make_api_error: ApiErrorFactory,
) -> None:
    # A definitively-absent order (404) resolves to None — never
    # BrokerUnavailable — so the clerk can journal submit_failed.
    fake = _FakeAlpaca()

    def raise_not_found(client_id: str) -> dict:
        raise make_api_error(404, message="order not found")

    fake.get_order_by_client_id = raise_not_found  # type: ignore[method-assign]

    result = await _client(fake).get_order_by_client_order_id("manual/inkant/v1:abc")

    assert result is None


async def test_timed_out_submit_stays_uncertain_until_order_becomes_visible(
    make_api_error: ApiErrorFactory,
) -> None:
    fake = _FakeAlpaca()
    started = Event()
    release = Event()
    visible = Event()
    lookup_calls: list[str] = []

    def delayed_post(path: str, data: Any = None) -> dict[str, Any]:
        started.set()
        release.wait()
        return {
            "id": "broker-order-delayed",
            "client_order_id": data["client_order_id"],
            "status": "accepted",
        }

    def lookup_after_visibility(client_id: str) -> dict[str, Any]:
        lookup_calls.append(client_id)
        if not visible.is_set():
            raise make_api_error(404, message="order not found")
        return {
            "id": "broker-order-delayed",
            "client_order_id": client_id,
            "status": "accepted",
        }

    fake.post = delayed_post  # type: ignore[method-assign]
    fake.get_order_by_client_id = lookup_after_visibility  # type: ignore[method-assign]
    # A timed-out write keeps its caller alive until its SDK worker finishes.
    # The subsequent lookup still uses this client's timeout budget.
    client = AlpacaTradingClient(client_factory=lambda: fake, timeout_s=0.25)
    order = {
        "symbol": "SPY",
        "qty": "1",
        "side": "buy",
        "type": "market",
        "client_order_id": "manual/inkant/v1:delayed",
    }

    submit = asyncio.create_task(client.submit_order(order))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        await asyncio.sleep(0.3)
        assert not submit.done()
        release.set()
        with pytest.raises(BrokerUnavailable, match="timed out"):
            await submit

        with pytest.raises(BrokerUnavailable, match="still become visible"):
            await client.get_order_by_client_order_id(order["client_order_id"])
        assert lookup_calls == [order["client_order_id"]]

        visible.set()
        result = await client.get_order_by_client_order_id(order["client_order_id"])
    finally:
        release.set()
        await asyncio.gather(submit, return_exceptions=True)

    assert result is not None
    assert result["id"] == "broker-order-delayed"


async def test_get_order_by_client_order_id_5xx_maps_to_unavailable(
    make_api_error: ApiErrorFactory,
) -> None:
    # A 5xx during the lookup must stay uncertain (BrokerUnavailable), NOT be
    # swallowed as absent — the order may still have landed.
    fake = _FakeAlpaca()

    def raise_server_error(client_id: str) -> dict:
        raise make_api_error(503, message="upstream error")

    fake.get_order_by_client_id = raise_server_error  # type: ignore[method-assign]

    with pytest.raises(BrokerUnavailable):
        await _client(fake).get_order_by_client_order_id("manual/inkant/v1:abc")


async def test_get_order_by_broker_order_id_returns_raw_payload() -> None:
    """The broker-id GET a replaced manual order is followed by (#2656)."""
    fake = _FakeAlpaca()
    order_id = "00000000-0000-0000-0000-00000000000b"

    payload = await _client(fake).get_order_by_broker_order_id(order_id)

    assert fake.broker_id_lookup_call == order_id
    assert payload == {"id": order_id, "client_order_id": "alpaca-generated", "status": "new"}


async def test_an_order_read_answered_200_with_an_empty_body_is_unknown_not_absent() -> None:
    """#2954: alpaca-py hands a 2xx with an empty or ``null`` body back as ``None``.

    That is an answer which could not be read, never Alpaca saying the order
    is absent -- as a 5xx is, it must stay ``BrokerUnavailable`` so a submit
    whose outcome is unknown is not folded into "never reached the broker".
    """
    fake = _FakeAlpaca()
    fake.get_order_by_client_id = lambda client_id: None  # type: ignore[method-assign]
    fake.get_order_by_id = lambda order_id: None  # type: ignore[method-assign]

    with pytest.raises(BrokerUnavailable, match="could not be read"):
        await _client(fake).get_order_by_client_order_id("manual/inkant/v1:abc")
    with pytest.raises(BrokerUnavailable, match="could not be read"):
        await _client(fake).get_order_by_broker_order_id("00000000-0000-0000-0000-00000000000b")


async def test_an_empty_200_inside_the_visibility_window_is_still_unknown_not_absent() -> None:
    """The window's 404 rule must not leak onto the empty body: absent it may not say, either way."""
    fake = _FakeAlpaca()
    fake.get_order_by_client_id = lambda client_id: None  # type: ignore[method-assign]
    client = _client(fake)
    client.hold_submission_uncertain("manual/inkant/v1:abc")

    with pytest.raises(BrokerUnavailable, match="could not be read"):
        await client.get_order_by_client_order_id("manual/inkant/v1:abc")


@pytest.mark.parametrize(("status", "expected"), [(404, None), (503, BrokerUnavailable)])
async def test_get_order_by_broker_order_id_404_is_absent_and_5xx_stays_uncertain(
    make_api_error: ApiErrorFactory, status: int, expected: type[Exception] | None,
) -> None:
    fake = _FakeAlpaca()

    def raise_status(order_id: str) -> dict:
        raise make_api_error(status, message="lookup failed")

    fake.get_order_by_id = raise_status  # type: ignore[method-assign]

    if expected is None:
        assert await _client(fake).get_order_by_broker_order_id("00000000-0000-0000-0000-00000000000b") is None
    else:
        with pytest.raises(expected):
            await _client(fake).get_order_by_broker_order_id("00000000-0000-0000-0000-00000000000b")


@pytest.mark.parametrize(
    "not_an_order_id",
    ["learn-ai/spy-bot/v1:close-1", "", "00000000-0000-0000-0000-00000000000"],
    ids=["a-client-order-id", "empty", "one-digit-short"],
)
async def test_get_order_by_broker_order_id_refuses_an_id_that_is_not_a_uuid(not_an_order_id: str) -> None:
    """The SDK parses the id before any request and raises a bare ``ValueError`` (#2854).

    The client refuses it as a request Alpaca could not take, a contract
    error every caller of an order read already handles, and asks Alpaca
    nothing.
    """
    fake = _FakeAlpaca()

    with pytest.raises(BrokerRequestInvalid, match="is not an Alpaca order id"):
        await _client(fake).get_order_by_broker_order_id(not_an_order_id)

    assert not hasattr(fake, "broker_id_lookup_call")


async def test_api_error_maps_to_contract_error(make_api_error: ApiErrorFactory) -> None:
    fake = _FakeAlpaca()

    def raise_rate_limited() -> dict:
        raise make_api_error(429, headers={"Retry-After": "1"})

    fake.get_account = raise_rate_limited  # type: ignore[method-assign]

    with pytest.raises(BrokerRateLimited) as excinfo:
        await _client(fake).get_account()
    assert excinfo.value.retry_after_ms == 1000


async def _noop_sleep(_seconds: float) -> None:
    return None


_ORDER_BODY = {
    "symbol": "SPY",
    "qty": "1",
    "side": "buy",
    "type": "market",
    "client_order_id": "manual/inkant/v1:cb3",
}


async def test_submit_retries_on_rate_limit_then_succeeds(
    make_api_error: ApiErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 429 means the order did NOT land, so a retry with the same
    # client_order_id is safe. One throttle then success → the bar's decision
    # is not silently lost.
    fake = _FakeAlpaca()
    calls = {"n": 0}
    bodies: list[Any] = []

    def flaky_post(path: str, data: Any = None) -> dict[str, Any]:
        calls["n"] += 1
        bodies.append(data)
        if calls["n"] == 1:
            raise make_api_error(429, headers={"Retry-After": "1"})
        return {"id": "o1", "client_order_id": data["client_order_id"], "status": "accepted"}

    fake.post = flaky_post  # type: ignore[method-assign]
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.broker.alpaca.client.anyio.sleep",
        lambda s: sleeps.append(s) or _noop_sleep(s),
    )

    payload = await _client(fake).submit_order(_ORDER_BODY)

    assert payload["status"] == "accepted"
    assert calls["n"] == 2  # one retry
    assert bodies[0] == bodies[1]  # same body / client_order_id → idempotent
    assert len(sleeps) == 1


async def test_submit_exhausts_rate_limit_retries_with_distinct_signal(
    make_api_error: ApiErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAlpaca()
    calls = {"n": 0}

    def always_throttle(path: str, data: Any = None) -> dict[str, Any]:
        calls["n"] += 1
        raise make_api_error(429, headers={"Retry-After": "1"})

    fake.post = always_throttle  # type: ignore[method-assign]
    monkeypatch.setattr("app.broker.alpaca.client.anyio.sleep", _noop_sleep)

    with pytest.raises(BrokerRateLimited) as excinfo:
        await _client(fake).submit_order(_ORDER_BODY)

    # Bounded: one initial attempt + the retry cap, never an unbounded loop.
    assert calls["n"] == _MAX_RATE_LIMIT_RETRIES + 1
    # Distinct operator signal: throttled-then-exhausted, not a plain reject.
    assert "throttled" in excinfo.value.message.lower()


async def test_submit_rate_limit_backoff_is_capped(
    make_api_error: ApiErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A large Retry-After must never stall a bar: the honored backoff is capped.
    fake = _FakeAlpaca()
    calls = {"n": 0}

    def throttle_then_ok(path: str, data: Any = None) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_api_error(429, headers={"Retry-After": "3600"})
        return {"id": "o1", "client_order_id": data["client_order_id"], "status": "accepted"}

    fake.post = throttle_then_ok  # type: ignore[method-assign]
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.broker.alpaca.client.anyio.sleep",
        lambda s: sleeps.append(s) or _noop_sleep(s),
    )

    await _client(fake).submit_order(_ORDER_BODY)

    assert sleeps
    assert max(sleeps) <= _RATE_LIMIT_RETRY_CAP_S


async def test_cancel_retries_on_rate_limit_then_succeeds(
    make_api_error: ApiErrorFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeAlpaca()
    calls = {"n": 0}

    def flaky_delete(path: str, data: Any = None) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_api_error(429, headers={"Retry-After": "1"})
        return None

    fake.delete = flaky_delete  # type: ignore[method-assign]
    monkeypatch.setattr("app.broker.alpaca.client.anyio.sleep", _noop_sleep)

    result = await _client(fake).cancel_order(_BROKER_ORDER_ID)

    assert result is None
    assert calls["n"] == 2


async def test_read_path_does_not_retry_on_rate_limit(
    make_api_error: ApiErrorFactory,
) -> None:
    # Only writes retry. A throttled read surfaces immediately so the sweep
    # cadence is not silently slowed.
    fake = _FakeAlpaca()
    calls = {"n": 0}

    def throttle(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls["n"] += 1
        raise make_api_error(429, headers={"Retry-After": "1"})

    fake.get_account = throttle  # type: ignore[method-assign]

    with pytest.raises(BrokerRateLimited):
        await _client(fake).get_account()
    assert calls["n"] == 1


async def test_missing_credentials_map_to_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    # AlpacaSettings reads ``env_file=".env"`` relative to CWD, so the repo's
    # real .env would supply credentials and defeat delenv. Run from an empty
    # dir so the settings genuinely find no credentials — host-independent.
    monkeypatch.chdir(tmp_path)
    reset_alpaca_settings_for_testing()
    try:
        # No client_factory → default factory reads settings, which raise.
        client = AlpacaTradingClient()
        with pytest.raises(BrokerAuthError):
            await client.get_account()
    finally:
        reset_alpaca_settings_for_testing()


async def test_hung_sdk_call_maps_to_broker_unavailable() -> None:
    fake = _FakeAlpaca()

    def slow_get_account() -> dict:
        time.sleep(0.05)
        return {"account_number": "PA1", "status": "ACTIVE"}

    fake.get_account = slow_get_account  # type: ignore[method-assign]
    client = AlpacaTradingClient(client_factory=lambda: fake, timeout_s=0.001)

    with pytest.raises(BrokerUnavailable, match="timed out") as excinfo:
        await client.get_account()

    assert excinfo.value.detail == "The broker did not respond within 0.001 seconds."
    # #2582: a timeout is the transient kind a startup reconnects on.
    assert isinstance(excinfo.value, BrokerUnreachable)


def test_order_send_claim_outlasts_every_bounded_submit_attempt() -> None:
    worst_send_ms = int(
        ((_MAX_RATE_LIMIT_RETRIES + 1) * _DEFAULT_TIMEOUT_S
         + _MAX_RATE_LIMIT_RETRIES * _RATE_LIMIT_RETRY_CAP_S) * 1000
    )

    assert DEFAULT_CLAIM_TTL_MS - worst_send_ms >= 13_000


def test_client_rejects_timeout_above_the_claimed_send_bound() -> None:
    with pytest.raises(ValueError, match="Alpaca request timeout"):
        AlpacaTradingClient(client_factory=_FakeAlpaca, timeout_s=_DEFAULT_TIMEOUT_S + 0.001)


def test_session_timeout_defaults_connect_and_read_timeouts() -> None:
    session = Session()
    captured: list[dict[str, Any]] = []

    def request(method: str, url: str, **kwargs: Any) -> None:
        captured.append(kwargs)

    session.request = request  # type: ignore[method-assign]
    _install_session_timeout(session, timeout_s=15.0)

    session.get("https://example.test/default")
    session.get("https://example.test/explicit", timeout=(1.0, 2.0))

    assert captured == [
        {"params": None, "allow_redirects": True, "timeout": (3.05, 15.0)},
        {"params": None, "allow_redirects": True, "timeout": (1.0, 2.0)},
    ]


async def test_timeouts_do_not_exhaust_the_global_anyio_thread_limiter() -> None:
    fake = _FakeAlpaca()
    started = 0
    started_lock = threading.Lock()
    release = threading.Event()

    def blocked_get_account() -> dict:
        nonlocal started
        with started_lock:
            started += 1
        release.wait()
        return {"account_number": "PA1", "status": "ACTIVE"}

    fake.get_account = blocked_get_account  # type: ignore[method-assign]
    client = AlpacaTradingClient(client_factory=lambda: fake, timeout_s=0.01)
    global_limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = global_limiter.total_tokens
    global_limiter.total_tokens = 2
    try:
        calls = [asyncio.create_task(client.get_account()) for _ in range(2)]
        for _ in range(100):
            if started == 2:
                break
            await anyio.sleep(0.001)
        assert started == 2

        results = await asyncio.gather(*calls, return_exceptions=True)
        assert all(isinstance(result, BrokerUnavailable) for result in results)

        with anyio.fail_after(0.05):
            assert await anyio.to_thread.run_sync(lambda: "available") == "available"
    finally:
        release.set()
        global_limiter.total_tokens = original_tokens


# ── Dev-only fault-injection seam: write-path hook (PRD #1354) ─────────────────


@pytest.fixture
def permit_write_injection(monkeypatch: pytest.MonkeyPatch):
    """Permit the seam (flag on + paper) and reset the registry around the test."""
    from types import SimpleNamespace

    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "resolved_alpaca_settings", lambda: SimpleNamespace(is_paper=True))
    fi.reset_fault_injection_for_testing()
    yield fi
    fi.reset_fault_injection_for_testing()


_INJECT_ORDER = {
    "symbol": "SPY",
    "qty": "1",
    "side": "buy",
    "type": "market",
    "client_order_id": "manual/bot-a/v1:inject",
}


async def test_injected_reject_short_circuits_before_sdk(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.REJECT_422
    )
    fake = _FakeAlpaca()
    client = _client(fake)

    with pytest.raises(BrokerRequestInvalid):
        await client.submit_order(_INJECT_ORDER)

    # No abnormal order: the SDK post was never called.
    assert fake.post_call is None


async def test_injected_conflict_is_definitive_reject(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.CONFLICT_409
    )
    fake = _FakeAlpaca()

    with pytest.raises(BrokerOrderRejected):
        await _client(fake).submit_order(_INJECT_ORDER)

    assert fake.post_call is None


async def test_injected_timeout_marks_uncertain_and_skips_sdk(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.TIMEOUT
    )
    fake = _FakeAlpaca()
    client = _client(fake)

    with pytest.raises(BrokerUnavailable, match="timed out"):
        await client.submit_order(_INJECT_ORDER)

    assert fake.post_call is None
    # Uncertain-submission window opened, exactly like a real timeout.
    assert client._submission_may_become_visible(_INJECT_ORDER["client_order_id"])


async def test_injected_post_sdk_submit_hides_landed_response(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.POST_SDK_TIMEOUT
    )
    fake = _FakeAlpaca()
    client = _client(fake)

    with pytest.raises(BrokerUnavailable, match="timed out"):
        await client.submit_order(_INJECT_ORDER)

    assert fake.post_call == ("/orders", _INJECT_ORDER)
    assert client._submission_may_become_visible(_INJECT_ORDER["client_order_id"])


async def test_injected_throttle_retries_then_lands_on_real_sdk(permit_write_injection) -> None:
    # A single injected 429 composes with the bounded rate-limit retry (CB3):
    # attempt 1 is throttled, attempt 2 hits the real SDK and lands.
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.THROTTLE_429, count=1
    )
    fake = _FakeAlpaca()

    result = await _client(fake).submit_order(_INJECT_ORDER)

    assert result == {"id": "broker-order-1", "status": "accepted"}
    # The order landed on the real SDK exactly once (the retry, not the throttle).
    assert fake.post_call == ("/orders", _INJECT_ORDER)


async def test_injected_fault_targets_only_matching_bot(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.REJECT_422, target="bot-b"
    )
    fake = _FakeAlpaca()

    # This order is for bot-a; the fault targets bot-b, so it lands normally.
    result = await _client(fake).submit_order(_INJECT_ORDER)

    assert result == {"id": "broker-order-1", "status": "accepted"}
    assert fake.post_call == ("/orders", _INJECT_ORDER)


async def test_injected_cancel_fault_short_circuits_delete(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.REJECT_422, target=_BROKER_ORDER_ID
    )
    fake = _FakeAlpaca()
    client = _client(fake)

    with pytest.raises(BrokerRequestInvalid):
        await client.cancel_order(_BROKER_ORDER_ID)

    assert fake.delete_call is None


async def test_injected_post_sdk_cancel_hides_landed_response(permit_write_injection) -> None:
    permit_write_injection.get_fault_injection_registry().arm(
        permit_write_injection.WriteFaultKind.POST_SDK_TIMEOUT,
        target=_BROKER_ORDER_ID,
    )
    fake = _FakeAlpaca()

    with pytest.raises(BrokerUnavailable, match="timed out"):
        await _client(fake).cancel_order(_BROKER_ORDER_ID)

    assert fake.delete_call == (f"/orders/{_BROKER_ORDER_ID}", None)


async def test_seam_off_does_not_inject(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the flag off, arming is refused and the submit lands on the SDK.
    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    fi.reset_fault_injection_for_testing()
    fake = _FakeAlpaca()

    result = await _client(fake).submit_order(_INJECT_ORDER)

    assert result == {"id": "broker-order-1", "status": "accepted"}
    assert fake.post_call == ("/orders", _INJECT_ORDER)


# ── Stale pooled connection retry (idempotent reads only, #2082) ───────────

_BASE = "https://paper-api.alpaca.markets"
_FIXED_MS = 1_700_000_000_000


def test_stale_pooled_connection_retries_reads_but_never_writes() -> None:
    """Alpaca closes idle keep-alives; urllib3 hands the dead socket to the
    next request and it fails with RemoteDisconnected before the request was
    answered at all. Observed 16 times in 6 hours on the Live lane, each pair
    degrading the clerk's posture to evidence-unavailable.

    A GET is safe to re-issue. A POST through the SAME session is not — and
    alpaca-py gives order submission and cancellation that same session — so
    this test is the guarantee that the retry can never resubmit an order.
    """
    session = Session()
    attempts: list[str] = []

    def request(method: str, url: str, **kwargs: Any) -> str:
        attempts.append(method)
        if attempts.count(method) == 1:
            raise RequestsConnectionError(
                ProtocolError(
                    "Connection aborted.",
                    RemoteDisconnected("Remote end closed connection without response"),
                )
            )
        return "ok"

    session.request = request  # type: ignore[method-assign]
    _install_stale_connection_retry(session)

    assert session.get(f"{_BASE}/v2/account") == "ok"
    with pytest.raises(RequestsConnectionError):
        session.post(f"{_BASE}/v2/orders", json={"symbol": "SPY"})

    assert attempts == ["GET", "GET", "POST"]


@responses.activate
async def test_account_read_recovers_from_a_stale_pooled_connection(tmp_path: Path) -> None:
    """End-to-end over the real client the service builds, so the wiring in
    ``_build_default_client`` is covered and not just the helper."""
    responses.add(
        responses.GET,
        f"{_BASE}/v2/account",
        body=RequestsConnectionError(
            ProtocolError(
                "Connection aborted.",
                RemoteDisconnected("Remote end closed connection without response"),
            )
        ),
    )
    responses.add(
        responses.GET,
        f"{_BASE}/v2/account",
        json={"account_number": "PA1", "status": "ACTIVE"},
        status=200,
    )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    payload = await client.get_account()

    assert payload["account_number"] == "PA1"
    assert len(responses.calls) == 2


@responses.activate
async def test_an_account_read_that_cannot_connect_is_the_transient_kind(tmp_path: Path) -> None:
    """#2582: Alpaca not reachable at all is what a startup reconnects on."""
    for _attempt in range(2):
        responses.add(
            responses.GET,
            f"{_BASE}/v2/account",
            body=RequestsConnectionError(
                ProtocolError(
                    "Connection aborted.",
                    RemoteDisconnected("Remote end closed connection without response"),
                )
            ),
        )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    with pytest.raises(BrokerUnreachable, match="Could not reach Alpaca while fetching account"):
        await client.get_account()


@responses.activate
async def test_order_submission_is_not_retried_when_the_connection_drops(tmp_path: Path) -> None:
    """The write path shares the pool. One dropped connection must surface as
    one failed submission, never as a second order on the wire."""
    responses.add(
        responses.POST,
        f"{_BASE}/v2/orders",
        body=RequestsConnectionError(
            ProtocolError(
                "Connection aborted.",
                RemoteDisconnected("Remote end closed connection without response"),
            )
        ),
    )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    with pytest.raises(BrokerUnavailable):
        await client.submit_order({"symbol": "SPY", "qty": "1", "side": "buy"})

    assert len(responses.calls) == 1


# ── alpaca-py's own status-code retry is off (#2304, #2342) ────────────────


@responses.activate
async def test_order_submission_is_not_re_posted_by_the_sdk_on_504(tmp_path: Path) -> None:
    """A 504 does not prove Alpaca dropped the order. alpaca-py re-POSTs on
    429/504 by default, so a gateway timeout after Alpaca accepted the order
    came back as a duplicate-client_order_id reject and the Clerk folded a
    live order as "did not reach the broker" (#2304). The abandoned SDK worker
    could also keep re-POSTing past the Clerk's absence window (#2342).

    One 504 must surface as one uncertain submission, with one POST on the
    wire and the order kept inside the visibility window."""
    responses.add(responses.POST, f"{_BASE}/v2/orders", status=504, json={"message": "gateway timeout"})
    responses.add(
        responses.POST,
        f"{_BASE}/v2/orders",
        status=422,
        json={"code": 40010001, "message": "client_order_id must be unique"},
    )
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    with pytest.raises(BrokerUnavailable):
        await client.submit_order({"symbol": "SPY", "qty": "1", "side": "buy", "client_order_id": "coid-1"})

    assert len(responses.calls) == 1
    assert client._submission_may_become_visible("coid-1")


@responses.activate
async def test_reads_are_not_retried_by_the_sdk_on_504(tmp_path: Path) -> None:
    """The SDK retry also slept 3 s between re-issued reads, stalling the
    sweep. Reads fail fast; the next sweep pass is the retry."""
    responses.add(responses.GET, f"{_BASE}/v2/account", status=504, json={"message": "gateway timeout"})
    client = AlpacaTradingClient(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )

    with pytest.raises(BrokerUnavailable):
        await client.get_account()

    assert len(responses.calls) == 1
