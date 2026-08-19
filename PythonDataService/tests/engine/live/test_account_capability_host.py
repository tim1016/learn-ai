from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.host_daemon import AccountCapabilityHost, create_app


def _host(tmp_path: Path) -> AccountCapabilityHost:
    return AccountCapabilityHost(
        repo_root=tmp_path,
        artifacts_root=tmp_path / "artifacts",
    )


def test_host_routes_are_read_only_account_capabilities(tmp_path: Path) -> None:
    app = create_app(_host(tmp_path), auth_token="secret")
    methods_and_paths = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }

    assert ("GET", "/health") in methods_and_paths
    assert ("GET", "/broker/sockets") in methods_and_paths
    assert ("POST", "/control-plane/renew-lease") in methods_and_paths
    assert not any(path.startswith("/accounts/") for _method, path in methods_and_paths)
    assert all("/runs" not in path for _method, path in methods_and_paths)
    assert all("/instances" not in path for _method, path in methods_and_paths)


@pytest.mark.asyncio
async def test_host_health_is_authenticated_and_has_no_executable_worker(
    tmp_path: Path,
) -> None:
    app = create_app(_host(tmp_path), auth_token="secret")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://host",
    ) as client:
        rejected = await client.get("/health")
        accepted = await client.get(
            "/health",
            headers={"X-Live-Runner-Token": "secret"},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["clerks"] == []
    assert accepted.json()["process"]["state"] == "idle"
