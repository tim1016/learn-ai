from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.account_clerk_supervisor import AccountClerkSupervisor
from app.engine.live.host_daemon import AccountClerkHost, create_app


class _RecordingClerkSupervisor:
    def __init__(self, *, fail_first_supervision: bool = False) -> None:
        self.reconcile_calls = 0
        self.supervise_calls = 0
        self.supervised = threading.Event()
        self._fail_first_supervision = fail_first_supervision

    def reconcile_on_boot(self) -> None:
        self.reconcile_calls += 1

    def supervise(self) -> None:
        self.supervise_calls += 1
        if self._fail_first_supervision and self.supervise_calls == 1:
            raise RuntimeError("simulated supervision pass failure")
        self.supervised.set()

    def health(self) -> list[object]:
        return []


class _BlockingClerkSupervisor(_RecordingClerkSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def supervise(self) -> None:
        self.supervise_calls += 1
        self.started.set()
        self.release.wait(timeout=1)
        self.finished.set()


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


@pytest.mark.asyncio
async def test_host_lifespan_supervises_account_clerks_until_shutdown(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    clerks = _RecordingClerkSupervisor()
    host.clerks = cast(AccountClerkSupervisor, clerks)
    app = create_app(
        host,
        auth_token="secret",
        clerk_supervision_interval_seconds=0.01,
    )

    async with app.router.lifespan_context(app):
        supervised = await asyncio.wait_for(
            asyncio.to_thread(clerks.supervised.wait),
            timeout=1,
        )
        assert supervised
        await asyncio.sleep(0.03)
        assert clerks.reconcile_calls == 1
        assert clerks.supervise_calls >= 2

    calls_after_shutdown = clerks.supervise_calls
    await asyncio.sleep(0.03)
    assert clerks.supervise_calls == calls_after_shutdown


@pytest.mark.asyncio
async def test_host_lifespan_retries_after_supervision_pass_crashes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = _host(tmp_path)
    clerks = _RecordingClerkSupervisor(fail_first_supervision=True)
    host.clerks = cast(AccountClerkSupervisor, clerks)
    app = create_app(
        host,
        auth_token="secret",
        clerk_supervision_interval_seconds=0.01,
    )

    with caplog.at_level(logging.ERROR, logger="app.engine.live.host_daemon"):
        async with app.router.lifespan_context(app):
            recovered = await asyncio.wait_for(
                asyncio.to_thread(clerks.supervised.wait),
                timeout=1,
            )

    assert recovered
    assert clerks.supervise_calls >= 2
    assert "account Clerk supervision pass failed" in caplog.text


@pytest.mark.asyncio
async def test_host_shutdown_waits_for_in_flight_clerk_supervision(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path)
    clerks = _BlockingClerkSupervisor()
    host.clerks = cast(AccountClerkSupervisor, clerks)
    app = create_app(
        host,
        auth_token="secret",
        clerk_supervision_interval_seconds=0.01,
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    started = await asyncio.wait_for(
        asyncio.to_thread(clerks.started.wait),
        timeout=1,
    )
    assert started

    shutdown = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await asyncio.sleep(0.01)
    assert not shutdown.done()

    clerks.release.set()
    await asyncio.wait_for(shutdown, timeout=1)
    assert clerks.finished.is_set()
