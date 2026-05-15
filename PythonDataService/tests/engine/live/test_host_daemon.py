"""Tests for the host-side live-run daemon."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.host_daemon import RunnerProcessManager, create_app

RUN_ID = "run-daemon-" + "a" * 53


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.fixture
def daemon_context(tmp_path: Path) -> tuple[RunnerProcessManager, Path]:
    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    run_dir = live_runs_root / RUN_ID
    run_dir.mkdir(parents=True)
    return RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root), run_dir


async def test_health_reports_idle_process(daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["process"]["state"] == "idle"


async def test_start_launches_existing_run_with_host_env(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    fake_process = FakeProcess()
    captured: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/runs/{RUN_ID}/start",
            json={
                "readonly": True,
                "hydrate_policy": "optional",
                "strategy": "spy_ema_crossover",
                "max_orders_per_day": 3,
                "ibkr_host": "127.0.0.1",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["process"]["state"] == "running"
    assert body["process"]["pid"] == 4242
    assert "--readonly" in captured["command"]
    assert "--hydrate-policy" in captured["command"]
    assert "optional" in captured["command"]
    assert str(run_dir) in captured["command"]
    assert captured["kwargs"]["cwd"] == str(manager.repo_root)
    assert captured["kwargs"]["env"]["IBKR_HOST"] == "127.0.0.1"
    assert "PythonDataService" in captured["kwargs"]["env"]["PYTHONPATH"]


async def test_start_rejects_second_active_run(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = daemon_context
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess())
    app = create_app(manager, allowed_origins=["http://localhost:4200"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/runs/{RUN_ID}/start", json={})
        second = await client.post(f"/runs/{RUN_ID}/start", json={})

    assert first.status_code == 200
    assert second.status_code == 409


async def test_start_rejects_missing_run(daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/runs/missing-run/start", json={})

    assert response.status_code == 404


async def test_stop_force_kills_when_graceful_signal_does_not_exit(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = daemon_context
    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)
    app = create_app(manager, allowed_origins=["http://localhost:4200"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.post(f"/runs/{RUN_ID}/start", json={})
        stop = await client.post(f"/runs/{RUN_ID}/stop", json={"force": True})

    assert start.status_code == 200
    assert stop.status_code == 200
    assert fake_process.signals
    assert fake_process.killed is True
    assert stop.json()["process"]["state"] == "exited"
    assert stop.json()["process"]["exit_code"] == -9
