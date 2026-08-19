from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.host_daemon import AccountClerkHost, create_app


def _host(tmp_path: Path) -> AccountClerkHost:
    return AccountClerkHost(repo_root=tmp_path, artifacts_root=tmp_path / "artifacts")


def test_host_routes_are_account_capabilities_not_bot_control(tmp_path: Path) -> None:
    app = create_app(_host(tmp_path), auth_token="secret")
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/broker/sockets" in paths
    assert "/accounts/{account_id}/clerk/ensure" in paths
    assert all("/runs" not in path for path in paths)
    assert all("/instances" not in path for path in paths)
    assert "/deploy" not in paths


@pytest.mark.asyncio
async def test_host_health_is_authenticated_and_has_no_live_process(tmp_path: Path) -> None:
    app = create_app(_host(tmp_path), auth_token="secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://host") as client:
        rejected = await client.get("/health")
        accepted = await client.get("/health", headers={"X-Live-Runner-Token": "secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["process"] == {
        "state": "idle",
        "run_id": None,
        "strategy_instance_id": None,
        "pid": None,
        "ibkr_client_id": None,
        "started_at_ms": None,
        "ended_at_ms": None,
        "exit_code": None,
        "exit_reason": None,
        "command": [],
        "log_path": None,
        "message": None,
    }
