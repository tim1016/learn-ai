"""Endpoint tests for /api/brokers/{broker}/bots (S2, #1260).

The Button-Rule exit is exercised end-to-end through the HTTP surface:
deploy → running roster row → stop → OFF_DUTY roster row.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.marketdata.feed import MarketDataBar
from app.routers.broker_bots import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry

_SID = "alpaca-api-bot-1"
_T0 = 1_700_000_000_000


class _FakeReadPort:
    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:  # pragma: no cover - registry shape only
        raise NotImplementedError


class _HoldFeed:
    feed_id = "fake"

    async def stream_bars(self, symbol: str, *, use_rth: bool = True):
        yield MarketDataBar(
            symbol=symbol,
            start_ms=_T0,
            end_ms=_T0 + 60_000,
            open=Decimal("400"),
            high=Decimal("401"),
            low=Decimal("399"),
            close=Decimal("400.5"),
            volume=100,
            fetched_at_ms=_T0 + 500,
            feed_id="fake",
            session_phase="RTH",
        )
        await asyncio.Event().wait()

    def health(self):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def api(tmp_path: Path):
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())
    registry = BotTaskRegistry(tmp_path, feed_resolver=lambda: _HoldFeed())
    set_bot_task_registry(registry)
    app = FastAPI()
    app.include_router(router)
    try:
        yield app, registry
    finally:
        set_bot_task_registry(None)
        reset_broker_registry_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unknown_broker_is_typed_404(api) -> None:
    app, _registry = api
    async with _client(app) as client:
        response = await client.get("/api/brokers/ibkr/bots")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["broker"] == "ibkr"


@pytest.mark.asyncio
async def test_deploy_stop_button_rule_end_to_end(api) -> None:
    app, _registry = api
    async with _client(app) as client:
        deployed = await client.post(
            "/api/brokers/alpaca/bots",
            json={"strategy_instance_id": _SID, "symbol": "spy"},
        )
        assert deployed.status_code == 201
        body = deployed.json()
        assert body["running"] is True
        assert body["phase"] == "ON_DUTY"
        assert body["broker"] == "alpaca"
        assert body["symbol"] == "SPY"  # normalized at the boundary

        listed = await client.get("/api/brokers/alpaca/bots")
        assert listed.status_code == 200
        assert [row["strategy_instance_id"] for row in listed.json()] == [_SID]

        stopped = await client.post(
            f"/api/brokers/alpaca/bots/{_SID}/stop", json={"reason": "drill"}
        )
        assert stopped.status_code == 200
        stopped_body = stopped.json()
        assert stopped_body["running"] is False
        assert stopped_body["phase"] == "OFF_DUTY"
        assert stopped_body["desired_state"] == "STOPPED"
        assert stopped_body["duty_outcome"]["kind"] == "STOPPED"

        status = await client.get(f"/api/brokers/alpaca/bots/{_SID}")
        assert status.status_code == 200
        assert status.json()["running"] is False


@pytest.mark.asyncio
async def test_stop_unknown_bot_is_404(api) -> None:
    app, _registry = api
    async with _client(app) as client:
        response = await client.post(f"/api/brokers/alpaca/bots/{_SID}/stop", json={})

    assert response.status_code == 404
    assert "not running" in response.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_invalid_strategy_instance_id_is_422(api) -> None:
    app, _registry = api
    async with _client(app) as client:
        response = await client.post(
            "/api/brokers/alpaca/bots",
            json={"strategy_instance_id": "  padded  ", "symbol": "SPY"},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_registry_not_installed_is_503(api) -> None:
    app, _registry = api
    set_bot_task_registry(None)
    async with _client(app) as client:
        response = await client.get("/api/brokers/alpaca/bots")

    assert response.status_code == 503
