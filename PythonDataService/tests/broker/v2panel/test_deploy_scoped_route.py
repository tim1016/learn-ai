"""Regression tests for the account-scoped deploy alias (§3, §5).

The deploy dialog POSTs to ``/api/brokers/{broker}/accounts/{account_id}/bots``
but only the unscoped ``/{broker}/bots`` route existed — the scoped form
404'd for the *correct* account (found live 2026-07-30, canary run). These
tests pin the scoped alias: correct account delegates to the bot runner,
mismatched account gets the documented typed 404.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.services.bot_runner import set_bot_task_registry
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT


class _FakeReadPort:
    broker_id = "alpaca"

    async def get_account(self) -> _FakeAccount:
        return _FakeAccount()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _FakeDeployRegistry:
    def __init__(self) -> None:
        self.deploy_calls: list[dict] = []

    async def deploy(self, **kwargs) -> BotStatusView:
        self.deploy_calls.append(kwargs)
        return BotStatusView(
            strategy_instance_id=kwargs["strategy_instance_id"],
            broker=kwargs["broker"],
            symbol=kwargs["symbol"],
            mode=kwargs["mode"],
            quantity=kwargs["quantity"],
            running=True,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id=None,
            duty_outcome=None,
            binding_created_at_ms=_T0,
            last_transition_at_ms=None,
        )


@pytest.fixture()
def deploy_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(tmp_path))
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    registry = _FakeDeployRegistry()
    set_bot_task_registry(registry)  # type: ignore[arg-type]

    fast_app = FastAPI()
    fast_app.include_router(router)

    try:
        yield fast_app, registry
    finally:
        set_bot_task_registry(None)
        reset_broker_registry_for_testing()


_BODY = {
    "strategy_instance_id": SID,
    "symbol": "SPY",
    "use_rth": True,
    "mode": "trade",
    "quantity": 2,
}


@pytest.mark.asyncio
async def test_deploy_scoped_correct_account_delegates(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["strategy_instance_id"] == SID
    assert body["mode"] == "trade"
    assert body["quantity"] == 2
    assert len(registry.deploy_calls) == 1
    call = registry.deploy_calls[0]
    assert call["broker"] == "alpaca"
    assert call["mode"] == "trade"
    assert call["quantity"] == 2


@pytest.mark.asyncio
async def test_deploy_scoped_account_mismatch_404(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/brokers/alpaca/accounts/WRONGACCT/bots", json=_BODY
        )

    assert resp.status_code == 404
    assert "not served by broker" in resp.json()["detail"]["message"]
    assert registry.deploy_calls == []
