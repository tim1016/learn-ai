"""Endpoint tests for /api/brokers/{broker}/bots (S2, #1260).

The Button-Rule exit is exercised end-to-end through the HTTP surface:
deploy → running roster row → stop → OFF_DUTY roster row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar
from app.routers.broker_bots import router
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import _custody_proof, _flat_start_guard
from tests._helpers.bot_runner.doubles import _CustodyClerk
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness

_SID = "alpaca-api-bot-1"
_T0 = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_fresh_live_market_liveness(monkeypatch)


class _FakeReadPort:
    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:  # pragma: no cover - registry shape only
        raise NotImplementedError


class _HoldFeed:
    feed_id = "fake"

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del continuity
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

    def health(self, _symbol: str | None = None) -> FeedHealth:
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
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
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
        set_alpaca_clerk(None)
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
    app, registry = api
    deployed = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    assert deployed.running is True
    assert deployed.phase == "ON_DUTY"
    assert deployed.broker == "alpaca"
    assert deployed.symbol == "SPY"

    async with _client(app) as client:
        listed = await client.get("/api/brokers/alpaca/bots")
        assert listed.status_code == 200
        assert [row["strategy_instance_id"] for row in listed.json()] == [_SID]

        stopped = await registry.stop("alpaca", _SID, reason="drill")
        assert stopped.running is False
        assert stopped.phase == "OFF_DUTY"
        assert stopped.desired_state == "STOPPED"
        assert stopped.duty_outcome.kind == "STOPPED"

        status = await client.get(f"/api/brokers/alpaca/bots/{_SID}")
        assert status.status_code == 200
        assert status.json()["running"] is False


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
    deployed = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    first_run_id = deployed.active_run_id
    await registry.stop("alpaca", _SID)
    resumed = await registry.resume_existing("alpaca", _SID)

    async with _client(app) as client:
        current = await client.get(
            f"/api/brokers/alpaca/bots/{_SID}/runs/current"
        )
        history = await client.get(
            f"/api/brokers/alpaca/bots/{_SID}/runs/history",
            params={"limit": 1},
        )
        scoped_current = await client.get(
            f"/api/brokers/alpaca/accounts/paper-account/bots/{_SID}/runs/current"
        )
        scoped_history = await client.get(
            f"/api/brokers/alpaca/accounts/paper-account/bots/{_SID}/runs/history",
            params={"limit": 1},
        )
        wrong_account_current = await client.get(
            f"/api/brokers/alpaca/accounts/account-1/bots/{_SID}/runs/current"
        )
        wrong_account_history = await client.get(
            f"/api/brokers/alpaca/accounts/account-1/bots/{_SID}/runs/history",
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
    assert scoped_current.status_code == 200
    assert scoped_current.json()["run_id"] == resumed.active_run_id
    assert scoped_history.status_code == 200
    assert [run["run_id"] for run in scoped_history.json()["runs"]] == [first_run_id]
    assert wrong_account_current.status_code == 404
    assert wrong_account_history.status_code == 404
    assert "account-1" in wrong_account_current.json()["detail"]["message"]
    assert "account-1" in wrong_account_history.json()["detail"]["message"]
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_run_reads_reject_unknown_bot_and_foreign_cursor(api) -> None:
    app, registry = api
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    async with _client(app) as client:
        missing = await client.get(
            "/api/brokers/alpaca/bots/missing/runs/current"
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
