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
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.routers.broker_bots import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.utils.timestamps import now_ms_utc
from tests.services.test_bot_runner import _flat_start_guard

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

    def health(self) -> FeedHealth:
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=_T0,
            reason="",
            active_subscription_count=0,
            observed_at_ms=now_ms_utc(),
        )


@pytest.fixture
def api(tmp_path: Path):
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: _HoldFeed(),
        boot_recovery_required=False,
        start_custody_guard=_flat_start_guard,
    )
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


@pytest.mark.asyncio
async def test_current_and_previous_runs_are_lazy_read_only_views(api) -> None:
    app, registry = api
    async with _client(app) as client:
        deployed = await client.post(
            "/api/brokers/alpaca/bots",
            json={"strategy_instance_id": _SID, "symbol": "SPY"},
        )
        first_run_id = deployed.json()["active_run_id"]
        await client.post(f"/api/brokers/alpaca/bots/{_SID}/stop", json={})
        resumed = await registry.resume_existing("alpaca", _SID)

        current = await client.get(
            f"/api/brokers/alpaca/bots/{_SID}/runs/current"
        )
        history = await client.get(
            f"/api/brokers/alpaca/bots/{_SID}/runs/history",
            params={"limit": 1},
        )

    assert current.status_code == 200
    assert current.json()["run_id"] == resumed.active_run_id
    assert current.json()["is_current"] is True
    assert current.json()["process"]["state"] == "RUNNING"
    assert current.json()["terminal_outcome"] is None
    assert history.status_code == 200
    assert [run["run_id"] for run in history.json()["runs"]] == [first_run_id]
    assert history.json()["runs"][0]["terminal_outcome"]["kind"] == "STOPPED"
    assert history.json()["next_cursor"] is None
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_run_reads_reject_unknown_bot_and_foreign_cursor(api) -> None:
    app, registry = api
    async with _client(app) as client:
        missing = await client.get(
            "/api/brokers/alpaca/bots/missing/runs/current"
        )
        await client.post(
            "/api/brokers/alpaca/bots",
            json={"strategy_instance_id": _SID, "symbol": "SPY"},
        )
        foreign_cursor = await client.get(
            f"/api/brokers/alpaca/bots/{_SID}/runs/history",
            params={"cursor": "another-bot-run"},
        )

    assert missing.status_code == 404
    assert foreign_cursor.status_code == 422
    assert "cursor" in foreign_cursor.json()["detail"]["message"].lower()
    await registry.stop("alpaca", _SID)


def test_run_read_openapi_documents_error_envelopes(api) -> None:
    app, _registry = api
    paths = app.openapi()["paths"]
    current_responses = paths[
        "/api/brokers/{broker}/bots/{strategy_instance_id}/runs/current"
    ]["get"]["responses"]
    history_responses = paths[
        "/api/brokers/{broker}/bots/{strategy_instance_id}/runs/history"
    ]["get"]["responses"]

    assert current_responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/BotRunReadNotFoundResponse"
    )
    assert current_responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/BotRunReadRunnerErrorResponse"
    )
    assert history_responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/BotRunReadNotFoundResponse"
    )
    assert history_responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/BotRunHistoryUnprocessableResponse"
    )
