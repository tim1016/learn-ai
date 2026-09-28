"""Configuration migrates through Stop/recovery before the durable cutover."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker_configuration import budget_authority
from app.broker_configuration.service import BrokerConfigurationService
from app.routers import broker_configuration as routes
from app.services.bot_runner import LaneStopOutcome, LaneStoppedBot
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_runtime import _Broker
from tests.broker_configuration.conftest import paper_profile


@pytest.fixture
async def upgrade_client(service: BrokerConfigurationService, tmp_path: Path, monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "upgrade-test-control")
    profile = paper_profile(service)
    service.acknowledge_effective(profile_id=profile.profile.profile_id, revision=1, account_id="PA-UPGRADE", expected_selection_generation=0)
    repo = ClerkSqliteRepository.initialize(account_id="PA-UPGRADE", artifacts_root=tmp_path / "custody", clock=_TestClock(NOON))
    repo.register_strategy_instance(strategy_instance_id="old", symbol="SPY", config_hash="old-seal")
    submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="run", clock=repo.clock)
    broker = _Broker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    runtime = ActiveClerkRuntime(authority_kind="sqlite", clerk=facade, _sqlite_repository=repo, account_id=repo.account_id)
    calls = []

    class Stopper:
        artifacts_root = tmp_path

        async def stop_every_running_bot(self, *, updated_by: str, reason: str) -> LaneStopOutcome:
            calls.append((updated_by, reason))
            await facade.stop_strategy_run(strategy_instance_id="old", run_id="run", reason=reason)
            return LaneStopOutcome(stopped=(LaneStoppedBot(strategy_instance_id="old", run_id="run"),), intent_stopped=(), refused=(), still_running=False)

    app.dependency_overrides[routes.get_broker_configuration_service] = lambda: service
    monkeypatch.setattr(routes, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(budget_authority, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(budget_authority, "get_bot_task_registry", lambda: Stopper())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Data-Plane-Control-Secret": "upgrade-test-control"}) as client:
            yield client, repo, calls
    finally:
        app.dependency_overrides.pop(routes.get_broker_configuration_service, None)
        await runtime.close()


async def test_ui_upgrade_stops_old_run_never_infers_budget_and_retry_is_read_only(upgrade_client) -> None:
    client, repo, calls = upgrade_client
    before = await client.get(f"{routes.PREFIX}/budget-authority")
    assert before.status_code == 200, before.text
    assert before.json()["state"] == "legacy" and before.json()["active_run_count"] == 1
    body = {"review_token": before.json()["review_token"]}
    response = await client.post(f"{routes.PREFIX}/budget-authority/apply", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "budget" and response.json()["active_run_count"] == 0
    assert repo.active_run("old") is None and repo.deployment_budget("old") is None
    assert len(calls) == 1
    again = await client.post(f"{routes.PREFIX}/budget-authority/apply", json=body)
    assert again.status_code == 200 and len(calls) == 1
    assert sum(row["transition_kind"] == "BUDGET_AUTHORITY_CUTOVER" for row in repo.custody_transitions()) == 1


async def test_stale_review_refuses_before_stopping_anything(upgrade_client) -> None:
    client, repo, calls = upgrade_client
    response = await client.post(f"{routes.PREFIX}/budget-authority/apply", json={"review_token": "old-review"})
    assert response.status_code == 409
    assert repo.budget_authority_version() == 1 and not calls
    assert repo.active_run("old") is not None


async def test_changed_runtime_refuses_before_stopping_selected_lane(upgrade_client, monkeypatch) -> None:
    client, repo, calls = upgrade_client
    before = await client.get(f"{routes.PREFIX}/budget-authority")
    monkeypatch.setattr(budget_authority, "get_active_clerk_runtime", lambda: None)
    response = await client.post(f"{routes.PREFIX}/budget-authority/apply", json={"review_token": before.json()["review_token"]})
    assert response.status_code == 409
    assert repo.budget_authority_version() == 1 and not calls
    assert repo.active_run("old") is not None
