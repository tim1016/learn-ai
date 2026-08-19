from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engine.live.daemon_transport import DaemonResult
from app.schemas.live_runs import HostRunnerHealth, HostRunnerProcessState, HostRunnerProcessStatus
from app.services import host_capability


@pytest.mark.asyncio
async def test_health_redacts_host_paths_and_process_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_capability,
        "get_settings",
        lambda: SimpleNamespace(live_runner_daemon_url="http://host"),
    )

    async def fetch(_url: str):
        return DaemonResult.connected(), HostRunnerHealth(
            ok=True,
            repo_root="/Users/operator/learn-ai",
            live_runs_root="/Users/operator/learn-ai/PythonDataService/artifacts/live_runs",
            fetched_at_ms=1,
            process=HostRunnerProcessStatus(
                state=HostRunnerProcessState.idle,
                command=["python", "secret.py"],
                log_path="/Users/operator/learn-ai/artifacts/host.log",
            ),
        )

    monkeypatch.setattr(host_capability.host_daemon_client, "fetch_health", fetch)

    health = await host_capability.HostCapabilityService().health()

    assert health.repo_root == "learn-ai"
    assert health.live_runs_root == "PythonDataService/artifacts/live_runs"
    assert health.process.command == []
    assert health.process.log_path == "artifacts/host.log"
