"""Seam tests for the Broker System v2 read router (/api/brokers/...).

A fake ``BrokerReadPort`` is bound into the registry; the router is exercised
over ASGITransport. These assert transport behavior — resolution, contract-error
translation — not any vendor. Grows one endpoint block per read-path slice.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.broker.contract.errors import BrokerAuthError, BrokerError, BrokerRateLimited
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerPosition,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.main import app
from app.security.data_plane_control import CONTROL_SECRET_HEADER


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    reset_broker_registry_for_testing()
    yield
    reset_broker_registry_for_testing()


def _snapshot(**overrides: Any) -> BrokerAccountSnapshot:
    base: dict[str, Any] = dict(
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
    broker_id: str = "alpaca"

    def __init__(
        self,
        *,
        account: BrokerAccountSnapshot | None = None,
        positions: list[BrokerPosition] | None = None,
        orders: list[BrokerOrder] | None = None,
        activities: list[BrokerActivity] | None = None,
        assets: list[BrokerAsset] | None = None,
        clock: BrokerClockEvidence | None = None,
        error: BrokerError | None = None,
    ) -> None:
        self._account = account
        self._positions = positions if positions is not None else []
        self._orders = orders if orders is not None else []
        self._activities = activities if activities is not None else []
        self._assets = assets if assets is not None else []
        self._clock = clock
        self._error = error
        self.orders_call: dict[str, str | int | None] | None = None
        self.activities_call: dict[str, int | None] | None = None
        self.assets_call: dict[str, str | int | None] | None = None

    async def get_account(self) -> BrokerAccountSnapshot:
        if self._error is not None:
            raise self._error
        assert self._account is not None
        return self._account

    async def list_positions(self) -> list[BrokerPosition]:
        if self._error is not None:
            raise self._error
        return self._positions

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        if self._error is not None:
            raise self._error
        self.orders_call = {"status": status, "limit": limit, "after_ms": after_ms}
        return self._orders

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        if self._error is not None:
            raise self._error
        self.activities_call = {"after_ms": after_ms, "limit": limit}
        return self._activities

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[BrokerAsset]:
        if self._error is not None:
            raise self._error
        self.assets_call = {"status": status, "limit": limit}
        return self._assets

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        if self._error is not None:
            raise self._error
        assert self._clock is not None
        return self._clock


async def _get(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


def _control_headers() -> dict[str, str]:
    secret = settings.DATA_PLANE_CONTROL_SECRET.strip()
    return {CONTROL_SECRET_HEADER: secret} if secret else {}


async def _delete(path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.delete(path, headers=_control_headers())


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


async def test_known_read_only_broker_cannot_use_alpaca_trade_clerk() -> None:
    port = _FakePort()
    port.broker_id = "ibkr"
    get_broker_registry().register(port)

    response = await _delete(
        "/api/brokers/ibkr/orders/11111111-1111-4111-8111-111111111111"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["broker"] == "ibkr"
    assert "not supported" in response.json()["detail"]["message"]


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


async def test_rate_limited_rounds_retry_after_up_to_whole_second() -> None:
    get_broker_registry().register(
        _FakePort(
            error=BrokerRateLimited("Throttled.", broker="alpaca", retry_after_ms=2500)
        )
    )

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"


def _position(**overrides: Any) -> BrokerPosition:
    base: dict[str, Any] = dict(
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


def _order(**overrides: Any) -> BrokerOrder:
    base: dict[str, Any] = dict(
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


def _activity(**overrides: Any) -> BrokerActivity:
    base: dict[str, Any] = dict(
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


async def test_activities_endpoint_returns_list_and_forwards_query_params() -> None:
    port = _FakePort(activities=[_activity(activity_id="act-9")])
    get_broker_registry().register(port)

    response = await _get("/api/brokers/alpaca/activities?after_ms=999&limit=5")

    assert response.status_code == 200
    assert response.json()[0]["activity_id"] == "act-9"
    assert port.activities_call == {"after_ms": 999, "limit": 5}


@pytest.mark.parametrize(
    "path",
    [
        "/api/brokers/alpaca/orders?after_ms=-1",
        "/api/brokers/alpaca/orders?after_ms=9223372036854775808",
        "/api/brokers/alpaca/activities?after_ms=-1",
        "/api/brokers/alpaca/activities?after_ms=9223372036854775808",
        "/api/brokers/alpaca/activities?limit=101",
    ],
)
async def test_timestamp_cursors_reject_values_outside_non_negative_int64_ms(
    path: str,
) -> None:
    get_broker_registry().register(_FakePort())

    response = await _get(path)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "expected_call"),
    [
        (
            "/api/brokers/alpaca/orders?after_ms=0",
            {"status": None, "limit": None, "after_ms": 0},
        ),
        (
            "/api/brokers/alpaca/orders?after_ms=9223372036854775807",
            {"status": None, "limit": None, "after_ms": 9_223_372_036_854_775_807},
        ),
        (
            "/api/brokers/alpaca/activities?after_ms=0",
            {"after_ms": 0, "limit": 100},
        ),
        (
            "/api/brokers/alpaca/activities?after_ms=9223372036854775807",
            {"after_ms": 9_223_372_036_854_775_807, "limit": 100},
        ),
    ],
)
async def test_timestamp_cursors_accept_non_negative_int64_bounds(
    path: str,
    expected_call: dict[str, str | int | None],
) -> None:
    port = _FakePort()
    get_broker_registry().register(port)

    response = await _get(path)

    assert response.status_code == 200
    actual_call = port.orders_call if "/orders" in path else port.activities_call
    assert actual_call == expected_call


def _asset(**overrides: Any) -> BrokerAsset:
    base: dict[str, Any] = dict(
        broker="alpaca",
        asset_id="a-1",
        symbol="AAPL",
        name="Apple Inc.",
        asset_class="us_equity",
        exchange="NASDAQ",
        status="active",
        tradable=True,
        fractionable=True,
        shortable=True,
        marginable=True,
    )
    base.update(overrides)
    return BrokerAsset(**base)


def _clock(**overrides: Any) -> BrokerClockEvidence:
    base: dict[str, Any] = dict(
        broker="alpaca",
        is_open=True,
        vendor_timestamp_ms=1_700_000_000_000,
        next_open_ms=1_700_050_000_000,
        next_close_ms=1_700_020_000_000,
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerClockEvidence(**base)


async def test_assets_endpoint_returns_list_and_forwards_query_params() -> None:
    port = _FakePort(assets=[_asset(symbol="MSFT")])
    get_broker_registry().register(port)

    response = await _get("/api/brokers/alpaca/assets?status=active&limit=5")

    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "MSFT"
    assert port.assets_call == {"status": "active", "limit": 5}


async def test_assets_endpoint_rejects_invalid_status() -> None:
    get_broker_registry().register(_FakePort(assets=[]))

    response = await _get("/api/brokers/alpaca/assets?status=bogus")

    assert response.status_code == 422


async def test_clock_endpoint_returns_vendor_evidence() -> None:
    get_broker_registry().register(_FakePort(clock=_clock(is_open=False)))

    response = await _get("/api/brokers/alpaca/clock")

    assert response.status_code == 200
    body = response.json()
    assert body["is_open"] is False
    assert body["vendor_timestamp_ms"] == 1_700_000_000_000
