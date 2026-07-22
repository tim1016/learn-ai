"""Seam tests for the Broker System v2 read router (/api/brokers/...).

A fake ``BrokerReadPort`` is bound into the registry; the router is exercised
over ASGITransport. These assert transport behavior — resolution, contract-error
translation — not any vendor. Grows one endpoint block per read-path slice.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.contract.errors import BrokerAuthError, BrokerRateLimited
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerOrder,
    BrokerPosition,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.main import app


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_broker_registry_for_testing()
    yield
    reset_broker_registry_for_testing()


def _snapshot(**overrides) -> BrokerAccountSnapshot:
    base = dict(
        broker="alpaca",
        account_id="PA1",
        account_status="ACTIVE",
        currency="USD",
        cash=100.0,
        equity=150.0,
        buying_power=300.0,
        portfolio_value=150.0,
        long_market_value=50.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1_600_000_000_000,
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerAccountSnapshot(**base)


class _FakePort:
    broker_id = "alpaca"

    def __init__(
        self, *, account=None, positions=None, orders=None, activities=None, error=None
    ) -> None:
        self._account = account
        self._positions = positions if positions is not None else []
        self._orders = orders if orders is not None else []
        self._activities = activities if activities is not None else []
        self._error = error
        self.orders_call = None
        self.activities_call = None

    async def get_account(self) -> BrokerAccountSnapshot:
        if self._error is not None:
            raise self._error
        return self._account

    async def list_positions(self):
        if self._error is not None:
            raise self._error
        return self._positions

    async def list_orders(self, *, status=None, limit=None, after_ms=None):
        if self._error is not None:
            raise self._error
        self.orders_call = {"status": status, "limit": limit, "after_ms": after_ms}
        return self._orders

    async def list_activities(self, *, after_ms=None):
        if self._error is not None:
            raise self._error
        self.activities_call = {"after_ms": after_ms}
        return self._activities


async def _get(path: str):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def test_account_endpoint_returns_snapshot() -> None:
    get_broker_registry().register(_FakePort(account=_snapshot(account_id="PA9")))

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == "PA9"
    assert body["broker"] == "alpaca"
    assert body["buying_power"] == 300.0


async def test_unknown_broker_returns_404() -> None:
    response = await _get("/api/brokers/nope/account")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["broker"] == "nope"
    assert "Unknown broker" in detail["message"]


async def test_auth_error_translates_to_502() -> None:
    get_broker_registry().register(
        _FakePort(error=BrokerAuthError("Alpaca rejected our credentials.", broker="alpaca"))
    )

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "Alpaca rejected our credentials."


async def test_rate_limited_sets_retry_after_header() -> None:
    get_broker_registry().register(
        _FakePort(
            error=BrokerRateLimited("Throttled.", broker="alpaca", retry_after_ms=2000)
        )
    )

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def _position(**overrides) -> BrokerPosition:
    base = dict(
        broker="alpaca",
        symbol="AAPL",
        asset_id="a-1",
        asset_class="us_equity",
        quantity=10.0,
        side="long",
        average_entry_price=135.8,
        market_value=1358.02,
        cost_basis=1358.0,
        current_price=135.8,
        unrealized_pl=0.02,
        unrealized_plpc=0.0,
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerPosition(**base)


async def test_positions_endpoint_returns_list() -> None:
    get_broker_registry().register(_FakePort(positions=[_position(symbol="MSFT")]))

    response = await _get("/api/brokers/alpaca/positions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "MSFT"


async def test_positions_endpoint_returns_empty_list() -> None:
    get_broker_registry().register(_FakePort(positions=[]))

    response = await _get("/api/brokers/alpaca/positions")

    assert response.status_code == 200
    assert response.json() == []


def _order(**overrides) -> BrokerOrder:
    base = dict(
        broker="alpaca",
        order_id="o-1",
        client_order_id=None,
        symbol="AAPL",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=10.0,
        filled_quantity=10.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=135.8,
        status="filled",
        submitted_at_ms=1_700_000_000_000,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
        filled_at_ms=1_700_000_000_500,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerOrder(**base)


def _activity(**overrides) -> BrokerActivity:
    base = dict(
        broker="alpaca",
        activity_id="act-1",
        activity_type="FILL",
        category="trade_activity",
        symbol="AAPL",
        side="buy",
        quantity=10.0,
        price=135.8,
        net_amount=None,
        occurred_at_ms=1_700_000_000_000,
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerActivity(**base)


async def test_orders_endpoint_returns_list_and_forwards_query_params() -> None:
    port = _FakePort(orders=[_order(order_id="o-9")])
    get_broker_registry().register(port)

    response = await _get("/api/brokers/alpaca/orders?status=open&limit=5&after_ms=123")

    assert response.status_code == 200
    assert response.json()[0]["order_id"] == "o-9"
    assert port.orders_call == {"status": "open", "limit": 5, "after_ms": 123}


async def test_orders_endpoint_rejects_invalid_status() -> None:
    get_broker_registry().register(_FakePort(orders=[]))

    response = await _get("/api/brokers/alpaca/orders?status=bogus")

    assert response.status_code == 422


async def test_activities_endpoint_returns_list_and_forwards_after_ms() -> None:
    port = _FakePort(activities=[_activity(activity_id="act-9")])
    get_broker_registry().register(port)

    response = await _get("/api/brokers/alpaca/activities?after_ms=999")

    assert response.status_code == 200
    assert response.json()[0]["activity_id"] == "act-9"
    assert port.activities_call == {"after_ms": 999}
