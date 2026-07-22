"""Tests for the async AlpacaTradingClient wrapper.

The SDK is faked via ``client_factory`` — these tests assert the wrapper's
behavior (raw passthrough, request building, error translation), not alpaca-py.
"""

from __future__ import annotations

from typing import Any

import pytest
from alpaca.trading.enums import AssetStatus, QueryOrderStatus

from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.broker.contract.errors import BrokerAuthError, BrokerRateLimited


class _FakeAlpaca:
    """A stand-in alpaca-py client returning canned raw payloads."""

    def __init__(self) -> None:
        self.orders_filter: Any = None
        self.assets_filter: Any = None
        self.activities_call: Any = None

    def get_account(self) -> dict:
        return {"account_number": "PA1", "status": "ACTIVE"}

    def get_all_positions(self) -> list[dict]:
        return [{"symbol": "AAPL"}]

    def get_orders(self, filter: Any = None) -> list[dict]:
        self.orders_filter = filter
        return [{"id": "o1"}]

    def get_all_assets(self, filter: Any = None) -> list[dict]:
        self.assets_filter = filter
        return [{"id": "a1"}]

    def get(self, path: str, data: Any = None) -> list[dict]:
        self.activities_call = (path, data)
        return [{"id": "act1"}]

    def get_clock(self) -> dict:
        return {"is_open": True}


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


async def test_list_assets_builds_status_filter() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_assets(status="active")

    assert fake.assets_filter.status == AssetStatus.ACTIVE


async def test_list_activities_calls_low_level_endpoint() -> None:
    fake = _FakeAlpaca()
    await _client(fake).list_activities(after_ms=1_700_000_000_000)

    path, data = fake.activities_call
    assert path == "/account/activities"
    assert "after" in data


async def test_get_clock_returns_raw() -> None:
    assert await _client(_FakeAlpaca()).get_clock() == {"is_open": True}


async def test_api_error_maps_to_contract_error(make_api_error) -> None:
    fake = _FakeAlpaca()

    def raise_rate_limited() -> dict:
        raise make_api_error(429, headers={"Retry-After": "1"})

    fake.get_account = raise_rate_limited  # type: ignore[method-assign]

    with pytest.raises(BrokerRateLimited) as excinfo:
        await _client(fake).get_account()
    assert excinfo.value.retry_after_ms == 1000


async def test_missing_credentials_map_to_auth_error(monkeypatch) -> None:
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
