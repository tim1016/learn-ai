"""Tests for the async AlpacaTradingClient wrapper.

The SDK is faked via ``client_factory`` — these tests assert the wrapper's
behavior (raw passthrough, request building, error translation), not alpaca-py.
"""

from __future__ import annotations

import asyncio
import time
from threading import Event
from typing import Any

import pytest
from alpaca.trading.enums import AssetStatus, QueryOrderStatus

from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.broker.contract.errors import (
    BrokerAuthError,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
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


def _client(fake: _FakeAlpaca) -> AlpacaTradingClient:
    return AlpacaTradingClient(client_factory=lambda: fake)


async def test_get_account_returns_raw_payload() -> None:
    assert await _client(_FakeAlpaca()).get_account() == {
        "account_number": "PA1",
        "status": "ACTIVE",
    }


async def test_list_positions_returns_raw_list() -> None:
    assert await _client(_FakeAlpaca()).list_positions() == [{"symbol": "AAPL"}]


async def test_list_orders_builds_filter() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_orders(status="open", limit=5, after_ms=1_700_000_000_000)

    assert fake.orders_filter.status == QueryOrderStatus.OPEN
    assert fake.orders_filter.limit == 5
    assert fake.orders_filter.after is not None


async def test_list_assets_builds_status_filter_and_caps_response() -> None:
    fake = _FakeAlpaca()
    payloads = await _client(fake).list_assets(status="active", limit=1)

    assert fake.assets_filter.status == AssetStatus.ACTIVE
    assert payloads == [{"id": "a1"}]


async def test_list_activities_calls_low_level_endpoint() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_activities(limit=25)

    path, data = fake.activities_call
    assert path == "/account/activities"
    assert data == {"page_size": 25, "direction": "desc"}


async def test_get_clock_returns_raw() -> None:
    assert await _client(_FakeAlpaca()).get_clock() == {"is_open": True}


async def test_submit_order_posts_to_orders_endpoint_and_returns_raw() -> None:
    fake = _FakeAlpaca()
    body = {"symbol": "SPY", "qty": "1", "side": "buy", "type": "market"}

    payload = await _client(fake).submit_order(body)

    assert fake.post_call == ("/orders", body)
    assert payload == {"id": "broker-order-1", "status": "accepted"}


async def test_cancel_order_deletes_by_id_and_returns_none() -> None:
    fake = _FakeAlpaca()

    result = await _client(fake).cancel_order("broker-order-1")

    assert fake.delete_call == ("/orders/broker-order-1", None)
    assert result is None


async def test_cancel_order_non_cancelable_maps_to_contract_error(
    make_api_error: ApiErrorFactory,
) -> None:
    fake = _FakeAlpaca()

    def raise_unprocessable(path: str, data: Any = None) -> None:
        raise make_api_error(422, message="order is not cancelable")

    fake.delete = raise_unprocessable  # type: ignore[method-assign]

    with pytest.raises(BrokerRequestInvalid) as excinfo:
        await _client(fake).cancel_order("broker-order-1")
    assert "not cancelable" in excinfo.value.message


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


async def test_timed_out_submit_stays_uncertain_while_sdk_worker_is_in_flight(
    make_api_error: ApiErrorFactory,
) -> None:
    fake = _FakeAlpaca()
    started = Event()
    release = Event()
    completed = Event()
    lookup_calls: list[str] = []

    def delayed_post(path: str, data: Any = None) -> dict[str, Any]:
        started.set()
        release.wait(timeout=1)
        completed.set()
        return {
            "id": "broker-order-delayed",
            "client_order_id": data["client_order_id"],
            "status": "accepted",
        }

    def lookup_after_visibility(client_id: str) -> dict[str, Any]:
        lookup_calls.append(client_id)
        if not completed.is_set():
            raise make_api_error(404, message="order not found")
        return {
            "id": "broker-order-delayed",
            "client_order_id": client_id,
            "status": "accepted",
        }

    fake.post = delayed_post  # type: ignore[method-assign]
    fake.get_order_by_client_id = lookup_after_visibility  # type: ignore[method-assign]
    client = AlpacaTradingClient(client_factory=lambda: fake, timeout_s=0.001)
    order = {
        "symbol": "SPY",
        "qty": "1",
        "side": "buy",
        "type": "market",
        "client_order_id": "manual/inkant/v1:delayed",
    }

    with pytest.raises(BrokerUnavailable, match="timed out"):
        await client.submit_order(order)
    assert started.is_set()

    with pytest.raises(BrokerUnavailable, match="still become visible"):
        await client.get_order_by_client_order_id(order["client_order_id"])
    assert lookup_calls == [order["client_order_id"]]

    release.set()
    for _ in range(100):
        try:
            result = await client.get_order_by_client_order_id(
                order["client_order_id"]
            )
            break
        except BrokerUnavailable:
            await asyncio.sleep(0.005)
    else:
        pytest.fail("timed-out SDK worker did not settle")

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


async def test_api_error_maps_to_contract_error(make_api_error: ApiErrorFactory) -> None:
    fake = _FakeAlpaca()

    def raise_rate_limited() -> dict:
        raise make_api_error(429, headers={"Retry-After": "1"})

    fake.get_account = raise_rate_limited  # type: ignore[method-assign]

    with pytest.raises(BrokerRateLimited) as excinfo:
        await _client(fake).get_account()
    assert excinfo.value.retry_after_ms == 1000


async def test_missing_credentials_map_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
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
