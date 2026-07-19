from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.fixture
def patch_signals(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    from app.services import deploy_preflight
    from app.services.deploy_preflight import DeployPreflightSignals

    def install(**overrides: object) -> None:
        base = {
            "daemon_reachable": True,
            "broker_connection_state": "connected",
            "account_frozen": False,
            "account_proven": True,
            "fleet_blocks_starts": False,
            "strategy_deployable": True,
            "instance_already_running": False,
            "session_in_start_window": True,
        }
        base.update(overrides)

        async def fake(
            strategy_key: str,
            account_id: str,
            instance_id: str,
            live_config: dict | None = None,
        ) -> DeployPreflightSignals:
            del strategy_key, account_id, instance_id, live_config
            return DeployPreflightSignals(**base)

        monkeypatch.setattr(deploy_preflight, "gather_deploy_preflight_signals", fake)

    return install


async def _get(params: dict[str, str]) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/live-instances/deploy-preflight", params=params)


async def test_preflight_ready_when_all_healthy(patch_signals: Callable[..., None]) -> None:
    patch_signals()

    resp = await _get(
        {"strategy_key": "spy_ema", "account_id": "DUM1", "instance_id": "bot1"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["blockers"] == []


async def test_preflight_blocks_when_broker_down(patch_signals: Callable[..., None]) -> None:
    patch_signals(broker_connection_state="disconnected")

    resp = await _get(
        {"strategy_key": "spy_ema", "account_id": "DUM1", "instance_id": "bot1"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is False
    assert any(
        blocker["condition"]["id"] == "broker_disconnected" for blocker in body["blockers"]
    )
