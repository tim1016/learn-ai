"""The UI activates only the pinned, inactive simulation and no trade authority."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.active_runtime import unavailable_runtime
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_broker import ShadowNamespaceUnproven
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.services import alpaca_shadow_activation as activation
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, _LiveBroker


@pytest.fixture
def setup_activation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    runtime = unavailable_runtime("SHADOW_ACTIVATION_REQUIRED", account_id=f"shadow:{LIVE_ACCT}", recovery="Activate Shadow.")
    binding = SimpleNamespace(settings=SimpleNamespace(mode="live"), account_pin=LIVE_ACCT)
    port = _LiveBroker(now_ms=1_000)
    service = SimpleNamespace(selection_handover=nullcontext, selection=lambda: SimpleNamespace(effective_account_id=LIVE_ACCT), owner=lambda: SimpleNamespace(owner_id="owner"))
    monkeypatch.setattr(activation, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(activation, "get_active_alpaca_binding", lambda: binding)
    monkeypatch.setattr(activation, "get_broker_registry", lambda: SimpleNamespace(resolve=lambda _: port))
    monkeypatch.setattr(activation, "get_broker_configuration_service", lambda: service)
    monkeypatch.setattr(activation, "live_artifacts_root", lambda: tmp_path)
    monkeypatch.setattr(activation.settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    monkeypatch.setattr(activation.fleet_settings, "WORKER_SERVICE", "live-worker")
    return SimpleNamespace(runtime=runtime, binding=binding, port=port)


async def test_explicit_activation_is_durable_replayable_and_budget_ready(setup_activation: SimpleNamespace, tmp_path: Path) -> None:
    state = activation.inactive_shadow_status(LIVE_ACCT, setup_activation.runtime)
    assert state is not None and state.state == "activation_available"
    first = await activation.activate_shadow_from_configuration(LIVE_ACCT)
    again = await activation.activate_shadow_from_configuration(LIVE_ACCT)
    assert first.activation_sha256 == again.activation_sha256
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCT}") is not None
    assert ActivationStore(tmp_path / "accounts" / "alpaca").latest(LIVE_ACCT) is None
    repo = ClerkSqliteRepository.open(account_id=f"shadow:{LIVE_ACCT}", artifacts_root=tmp_path)
    try:
        assert repo.budget_authority_version() == 2
        assert repo._conn.execute("SELECT COUNT(*) FROM custody_transitions WHERE transition_kind='SIMULATION_SESSION_BASELINE'").fetchone()[0] == 1
        assert repo._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert repo._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    finally:
        repo.close()


@pytest.mark.parametrize("change", ["wrong_account", "paper", "unmanaged", "open_control", "zero_cash", "graduated"])
async def test_activation_prerequisites_cannot_be_bypassed(change: str, setup_activation: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    account = LIVE_ACCT
    if change == "wrong_account":
        account = "another-account"
    elif change == "paper":
        setup_activation.binding.settings.mode = "paper"
    elif change == "unmanaged":
        monkeypatch.setattr(activation.fleet_settings, "WORKER_SERVICE", None)
    elif change == "open_control":
        monkeypatch.setattr(activation.settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", True)
    elif change == "zero_cash":
        setup_activation.port.cash = 0
    else:
        monkeypatch.setattr(ActivationStore, "latest", lambda _self, _account: object())
    with pytest.raises(activation.ShadowActivationRefused):
        await activation.activate_shadow_from_configuration(account)
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCT}") is None


async def test_changed_binding_during_broker_observation_refuses(setup_activation: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = setup_activation.port.get_account
    async def changed() -> object:
        result = await original()
        monkeypatch.setattr(activation, "get_active_alpaca_binding", lambda: SimpleNamespace(settings=SimpleNamespace(mode="live"), account_pin=LIVE_ACCT))
        return result
    monkeypatch.setattr(setup_activation.port, "get_account", changed)
    with pytest.raises(activation.ShadowActivationRefused, match="authority changed"):
        await activation.activate_shadow_from_configuration(LIVE_ACCT)
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCT}") is None


async def test_unproven_namespace_preserves_inactive_authority(setup_activation: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activation, "verify_shadow_namespace_empty", AsyncMock(side_effect=ShadowNamespaceUnproven("Order history is incomplete.")))
    with pytest.raises(activation.ShadowActivationRefused, match="history is incomplete"):
        await activation.activate_shadow_from_configuration(LIVE_ACCT)
    assert ShadowActivationStore(tmp_path).latest(f"shadow:{LIVE_ACCT}") is None


async def test_route_returns_receipt_before_managed_restart(setup_activation: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import alpaca_live_graduation as route
    restart = AsyncMock()
    monkeypatch.setattr(route, "restart_after_graduation", restart)
    app = FastAPI()
    app.include_router(route.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/live-graduation/shadow-activation")
    assert result.status_code == 202, result.text
    assert result.json()["state"] == "restart_scheduled"
    restart.assert_awaited_once()


async def test_existing_history_requires_the_visible_budget_upgrade(setup_activation: SimpleNamespace, tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=f"shadow:{LIVE_ACCT}", artifacts_root=tmp_path)
    repo.register_strategy_instance(strategy_instance_id="old", symbol="SPY", config_hash="old-seal")
    submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="old-run", clock=repo.clock)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="old-run", clock=repo.clock)
    repo.close()
    await activation.activate_shadow_from_configuration(LIVE_ACCT)
    repo = ClerkSqliteRepository.open(account_id=f"shadow:{LIVE_ACCT}", artifacts_root=tmp_path)
    try:
        assert repo.budget_authority_version() == 1
        assert repo._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        repo.close()
