"""Seam tests for the Broker System v2 read router (/api/brokers/...).

A fake ``BrokerReadPort`` is bound into the registry; the router is exercised
over ASGITransport. These assert transport behavior — resolution, contract-error
translation — not any vendor. Grows one endpoint block per read-path slice.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.contract.errors import BrokerAuthError, BrokerRateLimited
from app.broker.contract.models import BrokerAccountSnapshot
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


class _FakeAccountPort:
    broker_id = "alpaca"

    def __init__(self, *, account=None, error=None) -> None:
        self._account = account
        self._error = error

    async def get_account(self) -> BrokerAccountSnapshot:
        if self._error is not None:
            raise self._error
        return self._account


async def _get(path: str):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def test_account_endpoint_returns_snapshot() -> None:
    get_broker_registry().register(_FakeAccountPort(account=_snapshot(account_id="PA9")))

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
        _FakeAccountPort(error=BrokerAuthError("Alpaca rejected our credentials.", broker="alpaca"))
    )

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "Alpaca rejected our credentials."


async def test_rate_limited_sets_retry_after_header() -> None:
    get_broker_registry().register(
        _FakeAccountPort(
            error=BrokerRateLimited("Throttled.", broker="alpaca", retry_after_ms=2000)
        )
    )

    response = await _get("/api/brokers/alpaca/account")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"
