"""The lane's installation-migration quiesce routes (#2268).

``POST /api/brokers/alpaca/lane/stop-all-bots`` stops every bot on the lane
and returns the durable receipt it wrote; ``GET
/api/brokers/alpaca/lane/account-quiet`` answers #2154's account-quiet read on
demand, with no drain and no registry change. Both are the agent paths the
coordinator forwards the catalog's ``lane_stop_all_bots`` and
``lane_account_quiet_read`` operations to.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.fleet_boot import LaneQuietAnswer
from app.config import settings
from app.main import app
from app.services.bot_runner import set_bot_task_registry
from app.services.lane_quiesce import (
    STOP_ALL_RECEIPTS_DIRECTORY,
    LaneAccountQuietSource,
    set_lane_account_quiet_source,
)
from tests._helpers.bot_runner.custody import _SID, _custody_proof, _registry
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness

_T0 = 1_788_040_000_000
_STOP_PATH = "/api/brokers/alpaca/lane/stop-all-bots"
_QUIET_PATH = "/api/brokers/alpaca/lane/account-quiet"
_BODY = {"operator": "inkant", "change_ref": "migrate-2026-09-22"}


@pytest.fixture(autouse=True)
def _clean_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    patch_fresh_live_market_liveness(monkeypatch)
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))
    set_bot_task_registry(None)
    set_lane_account_quiet_source(None)
    yield
    set_bot_task_registry(None)
    set_lane_account_quiet_source(None)
    set_alpaca_clerk(None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_stop_all_bots_stops_every_bot_and_returns_the_durable_receipt(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    set_bot_task_registry(registry)

    async with _client() as client:
        response = await client.post(_STOP_PATH, json=_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["all_stopped"] is True
    assert body["stopped"] == [
        {"strategy_instance_id": _SID, "run_id": deployed.active_run_id}
    ]
    assert body["operator"] == "inkant"
    assert registry.status("alpaca", _SID).desired_state == "STOPPED"
    receipts = list((tmp_path / STOP_ALL_RECEIPTS_DIRECTORY).glob("*.json"))
    assert [json.loads(p.read_text(encoding="utf-8"))["receipt_id"] for p in receipts] == [
        body["receipt_id"]
    ]


async def test_stop_all_bots_that_leaves_a_bot_running_refuses_with_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.bot_runner import RunAdmissionRefusedError

    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    real_stop = registry.stop

    async def refusing_stop(*_args: object, **_kwargs: object):
        raise RunAdmissionRefusedError("custody unavailable")

    monkeypatch.setattr(registry, "stop", refusing_stop)
    set_bot_task_registry(registry)

    async with _client() as client:
        response = await client.post(_STOP_PATH, json=_BODY)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "lane_stop_all_incomplete"
    assert detail["receipt"]["all_stopped"] is False
    assert detail["receipt"]["refused"][0]["strategy_instance_id"] == _SID
    monkeypatch.setattr(registry, "stop", real_stop)
    await registry.stop("alpaca", _SID)


async def test_stop_all_bots_without_a_bot_runner_refuses() -> None:
    async with _client() as client:
        response = await client.post(_STOP_PATH, json=_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_bot_runner_not_installed"


async def test_stop_all_bots_requires_operator_and_change_ref(tmp_path: Path) -> None:
    set_bot_task_registry(_registry(tmp_path, _FakeFeed([], mode="hold")))

    async with _client() as client:
        response = await client.post(_STOP_PATH, json={"operator": "inkant"})

    assert response.status_code == 422


async def test_stop_all_bots_is_a_guarded_control_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    set_bot_task_registry(_registry(tmp_path, _FakeFeed([], mode="hold")))

    async with _client() as client:
        refused = await client.post(_STOP_PATH, json=_BODY)
        quiet_refused = await client.get(_QUIET_PATH)

    assert refused.status_code == 403
    assert quiet_refused.status_code == 403
    assert not (tmp_path / STOP_ALL_RECEIPTS_DIRECTORY).exists()


def _source(answer: LaneQuietAnswer | None) -> LaneAccountQuietSource:
    async def probe() -> LaneQuietAnswer | None:
        return answer

    return LaneAccountQuietSource(account_id="PA-MIGRATE-1", probe=probe)


async def test_account_quiet_answers_quiet_for_a_flat_idle_account() -> None:
    set_lane_account_quiet_source(
        _source(
            LaneQuietAnswer(
                observed_at_ms=_T0,
                runner_idle=True,
                broker_work_ended=True,
                account_flat=True,
                intents_resolved=True,
            )
        )
    )

    async with _client() as client:
        response = await client.get(_QUIET_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "account_id": "PA-MIGRATE-1",
        "observed_at_ms": _T0,
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": True,
        "intents_resolved": True,
        "quiet": True,
        "outstanding": [],
    }


async def test_account_quiet_names_each_open_condition_not_flat() -> None:
    set_lane_account_quiet_source(
        _source(
            LaneQuietAnswer(
                observed_at_ms=_T0,
                runner_idle=True,
                broker_work_ended=False,
                account_flat=False,
                intents_resolved=True,
            )
        )
    )

    async with _client() as client:
        response = await client.get(_QUIET_PATH)

    body = response.json()
    assert response.status_code == 200
    assert body["quiet"] is False
    assert body["outstanding"] == [
        "a working order on the account has not ended",
        "the account or the lane's custody is not flat",
    ]


async def test_account_quiet_that_cannot_observe_the_broker_is_unavailable() -> None:
    set_lane_account_quiet_source(_source(None))

    async with _client() as client:
        response = await client.get(_QUIET_PATH)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_account_quiet_unobservable"


async def test_account_quiet_on_a_lane_with_no_clerk_is_unanswerable() -> None:
    async with _client() as client:
        response = await client.get(_QUIET_PATH)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_account_quiet_unanswerable"
