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

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkStatus,
    HoldState,
)
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.services.broker_v2_panel import panel_data_source
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000


class _FakeAccount:
    account_id = ACCT
    account_mode = "paper"
    account_status = "ACTIVE"
    trading_blocked = False
    account_blocked = False


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
    clear_broker_account_snapshot_cache_for_testing()
    reset_broker_registry_for_testing()
    get_broker_registry().register(_FakeReadPort())  # type: ignore[arg-type]
    registry = _FakeDeployRegistry()
    set_bot_task_registry(registry)  # type: ignore[arg-type]

    async def clerk_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            outstanding_intents=0,
            observed_at_ms=_T0,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", clerk_status)

    fast_app = FastAPI()
    fast_app.include_router(router)

    try:
        yield fast_app, registry
    finally:
        set_bot_task_registry(None)
        clear_broker_account_snapshot_cache_for_testing()
        reset_broker_registry_for_testing()


_BODY = {
    "strategy_instance_id": SID,
    "strategy_key": "deployment_validation",
    "symbol": "SPY",
    "sizing": {"preset": "custom", "quantity": 2},
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
    assert body["status"] == "deployed"
    assert body["bot"]["strategy_instance_id"] == SID
    assert body["bot"]["mode"] == "trade"
    assert body["bot"]["quantity"] == 2
    assert body["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "SPY"
    assert body["action_plan"]["on_exit"] == [
        {"kind": "close_leg", "entry_leg_id": "primary"}
    ]
    assert body["next_action"]
    assert body["panel_path"].endswith(f"/{SID}")
    assert len(registry.deploy_calls) == 1
    call = registry.deploy_calls[0]
    assert call["broker"] == "alpaca"
    assert call["mode"] == "trade"
    assert call["quantity"] == 2
    assert call["use_rth"] is True


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
    assert "is not the account for broker" in resp.json()["detail"]["message"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_deploy_view_is_closed_paper_only_contract(deploy_app) -> None:
    fast_app, _registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["account_mode"] == "paper"
    assert body["allowed_actions"] == ["deploy"]
    assert [row["strategy_key"] for row in body["strategies"]] == [
        "deployment_validation"
    ]
    assert [row["preset"] for row in body["sizing_options"]] == [
        "safe_canary",
        "custom",
    ]
    assert "enter" in body["action_plan_explanation"].lower()
    assert "close" in body["action_plan_explanation"].lower()
    assert body["carryover_available"] is False
    assert body["carryover_label"]
    assert body["carryover_explanation"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {**_BODY, "mode": "log_only"},
        {**_BODY, "strategy_key": "unknown"},
        {
            **_BODY,
            "sizing": {"preset": "safe_canary", "quantity": 2},
        },
    ],
)
async def test_deploy_rejects_semantics_outside_closed_contract(
    deploy_app,
    body: dict,
) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=body,
        )

    assert resp.status_code == 422
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_carryover_requires_account_policy_and_explicit_deploy_opt_in(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    carryover_body = {**_BODY, "carryover_policy": "ALLOW"}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        blocked = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=carryover_body,
        )
        monkeypatch.setattr(settings, "ALPACA_PAPER_CARRYOVER_ENABLED", True)
        accepted = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=carryover_body,
        )

    assert blocked.status_code == 409
    assert accepted.status_code == 201
    assert len(registry.deploy_calls) == 1
    assert registry.deploy_calls[0]["carryover_policy"] == "ALLOW"


@pytest.mark.asyncio
async def test_clerk_hold_authors_blocked_view_and_submission_remedy(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def held_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(
                active=True,
                reason_code="UNEXPLAINED_ORDER_HOLD",
                reason="An unattributed broker order requires operator review.",
                since_ms=_T0 - 1,
            ),
            outstanding_intents=0,
            observed_at_ms=_T0,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", held_status)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        view_response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy"
        )
        deploy_response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json=_BODY,
        )

    view = view_response.json()
    assert view["eligibility"]["reason_code"] == "UNEXPLAINED_ORDER_HOLD"
    assert view["allowed_actions"] == []
    assert deploy_response.status_code == 409
    detail = deploy_response.json()["detail"]
    assert detail["why"] == "An unattributed broker order requires operator review."
    assert detail["next_action"]
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_account_freeze_category_and_remedy_reach_deploy_unchanged(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app

    async def frozen_status() -> ClerkStatus:
        return ClerkStatus(
            broker="alpaca",
            account_id=ACCT,
            hold=HoldState(active=False),
            freeze=AccountFreezeState(
                active=True,
                category="ACCOUNT_STATE_UNPROVABLE",
                explanation="Fresh order and exposure truth is unavailable.",
                next_step="Restore broker observation, then reconcile.",
                observed_at_ms=_T0,
            ),
            outstanding_intents=0,
            observed_at_ms=_T0,
        )

    monkeypatch.setattr(panel_data_source, "_clerk_status", frozen_status)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy"
        )

    eligibility = response.json()["eligibility"]
    assert eligibility["reason_code"] == "ACCOUNT_STATE_UNPROVABLE"
    assert eligibility["explanation"] == "Fresh order and exposure truth is unavailable."
    assert eligibility["next_action"] == "Restore broker observation, then reconcile."
    assert registry.deploy_calls == []


@pytest.mark.asyncio
async def test_account_trading_block_authors_ineligible_deploy_view(
    deploy_app,
    monkeypatch,
) -> None:
    fast_app, registry = deploy_app
    monkeypatch.setattr(_FakeAccount, "trading_blocked", True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=fast_app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy"
        )

    assert response.status_code == 200
    view = response.json()
    assert view["eligibility"]["reason_code"] == "ALPACA_ACCOUNT_NOT_TRADABLE"
    assert view["allowed_actions"] == []
    assert registry.deploy_calls == []
