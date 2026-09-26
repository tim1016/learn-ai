"""HTTP arming uses the existing reobserving, append-only ceremony."""

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker_configuration.cli_binding import EffectiveBroker
from app.routers.alpaca_live_arming import router
from app.services.alpaca_live_arming import AlpacaLiveArmingService, get_alpaca_live_arming_service
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    LIVE_ACCT,
    arming_ready,
    live_settings,
)


@pytest.fixture
def ceremony(tmp_path):
    root, live = tmp_path / "clerk", tmp_path / "live"
    arming_ready(root, live)
    now = [ARMED_AT_MS]
    settings = [live_settings(clerk_dir=root)]
    service = AlpacaLiveArmingService(
        resolve=lambda: EffectiveBroker(settings[0], None),
        live_state_root=live,
        artifacts_root=root,
        clock=lambda: now[0],
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_alpaca_live_arming_service] = lambda: service
    return app, root, now, settings


def _url(sid=ARMING_SID, account=LIVE_ACCT):
    return f"/api/brokers/alpaca/accounts/{account}/bots/{sid}/arming"


@pytest.mark.parametrize("route_account", [LIVE_ACCT, LIVE_ACCT.lower()])
async def test_plan_confirm_and_disarm_keep_one_durable_seal(ceremony, route_account):
    app, root, _, _ = ceremony
    ledger = LiveArmingLedger(root, live_account_id=LIVE_ACCT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(_url(account=route_account))).json()["state"] == "unarmed"
        response = await client.post(_url(account=route_account) + "/plan")
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan["exit_terms"]["provenance"] == "deployed"
        assert plan["expires_at_ms"] - plan["created_at_ms"] == 120_000
        assert ledger.records() == ()
        body = {"plan_id": plan["plan_id"], "confirmation_token": "wrong"}
        assert (await client.post(_url(account=route_account) + "/apply", json=body)).status_code == 409
        body["confirmation_token"] = plan["confirmation_token"]
        armed = await client.post(_url(account=route_account) + "/apply", json=body)
        assert armed.status_code == 200, armed.text
        assert armed.json()["state"] == "armed"
        assert armed.json()["armed_instance_count"] == 1
        assert (await client.post(_url(account=route_account) + "/apply", json=body)).status_code == 200
        assert len(ledger.records()) == 1
        assert ledger.records()[0].exit_terms == plan["exit_terms"]
        disarmed = await client.post(_url(account=route_account) + "/disarm")
        assert disarmed.status_code == 200, disarmed.text
        assert disarmed.json()["state"] == "disarmed"
        assert disarmed.json()["armed_instance_count"] == 0
        assert len(ledger.records()) == 2


@pytest.mark.parametrize("change", ["expired", "envelope", "account", "instance"])
async def test_changed_or_misdirected_plan_cannot_arm(ceremony, change):
    app, root, now, settings = ceremony
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        plan = (await client.post(_url() + "/plan")).json()
        url = _url()
        if change == "expired":
            now[0] = plan["expires_at_ms"] + 1
        elif change == "envelope":
            settings[0] = settings[0].model_copy(update={"live_loss_usd": 100.0})
        elif change == "account":
            url = _url(account="OTHER")
        else:
            url = _url(sid="other-bot")
        response = await client.post(
            url + "/apply",
            json={
                "plan_id": plan["plan_id"],
                "confirmation_token": plan["confirmation_token"],
            },
        )
        assert response.status_code == 409, response.text
        assert LiveArmingLedger(root, live_account_id=LIVE_ACCT).records() == ()


async def test_disarm_does_not_need_readable_configuration(ceremony):
    app, root, _, settings = ceremony
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        plan = (await client.post(_url() + "/plan")).json()
        assert (
            await client.post(
                _url() + "/apply",
                json={
                    "plan_id": plan["plan_id"],
                    "confirmation_token": plan["confirmation_token"],
                },
            )
        ).status_code == 200
        settings[0] = settings[0].model_copy(update={"mode": "paper"})
        response = await client.post(_url() + "/disarm")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "disarmed"
        assert len(LiveArmingLedger(root, live_account_id=LIVE_ACCT).records()) == 2


async def test_corrupt_sibling_ledger_does_not_hide_the_requested_account(ceremony):
    app, root, _, _ = ceremony
    damaged = LiveArmingLedger(root, live_account_id="OTHER-LIVE")
    damaged.path.parent.mkdir(parents=True, exist_ok=True)
    damaged.path.write_text("corrupt\n")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(_url(account=LIVE_ACCT.lower()))
    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == LIVE_ACCT


async def test_preparing_a_plan_prunes_only_expired_plans(ceremony):
    app, root, now, _ = ceremony
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        first = (await client.post(_url() + "/plan")).json()
        first_path = root / "live-arming-plans" / (first["plan_id"] + ".json")
        now[0] += 1
        second = (await client.post(_url() + "/plan")).json()
        assert first_path.exists()
        now[0] = first["expires_at_ms"] + 1
        response = await client.post(_url() + "/plan")
        assert response.status_code == 200, response.text
        assert not first_path.exists()
        assert (root / "live-arming-plans" / (second["plan_id"] + ".json")).exists()
