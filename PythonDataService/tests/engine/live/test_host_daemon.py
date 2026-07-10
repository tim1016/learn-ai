"""Tests for the host-side live-run daemon."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.account_artifacts import (
    append_account_event,
    read_account_freeze,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    bot_order_namespace_for_instance,
    compute_reconcile_namespaces,
    read_account_instance_registry,
    write_account_instance_binding,
)
from app.engine.live.daemon_auth import TOKEN_HEADER
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.host_daemon import (
    HostRunnerError,
    RunnerProcessManager,
    _parse_ibkr_client_id_pool,
    build_parser,
    create_app,
)
from app.engine.live.host_runner_policy import validate_ibkr_host_allowed
from app.engine.live.run_status import write_run_status
from app.operator.incidents.store import IncidentStore
from app.schemas.bot_events import (
    BotEventRawType,
    SourceAuthority,
    TerminalErrorCode,
    TerminalErrorSource,
)
from app.schemas.broker_session import GatewaySocketRow
from app.schemas.live_runs import (
    ExitReason,
    HostRunnerActionResponse,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
    HostRunnerStartRequest,
    RunStatusSidecar,
)
from app.services.bot_event_wal import BotEventRawWal, run_bot_event_wal_path

RUN_ID = "run-daemon-" + "a" * 53
# Every protected route requires the shared secret (ADR 0007); tests pin a known
# token into create_app and send it on the client via the _AUTH header.
_TEST_TOKEN = "test-daemon-token"
_AUTH = {TOKEN_HEADER: _TEST_TOKEN}

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in this environment",
)


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


def _add_managed_process(
    manager: RunnerProcessManager,
    *,
    key: str,
    ended_at_ms: int | None,
    returncode: int | None,
) -> None:
    from app.engine.live.host_daemon import ManagedProcess

    run_id = f"run-{key}"
    run_dir = manager.live_runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "host_daemon.log"
    log_handle = log_path.open("a", encoding="utf-8")
    if ended_at_ms is not None:
        log_handle.close()
    process = FakeProcess()
    process.returncode = returncode
    manager._managed[key] = ManagedProcess(
        strategy_instance_id=key,
        run_id=run_id,
        run_dir=run_dir,
        process=process,
        command=[],
        started_at_ms=ended_at_ms or 0,
        log_path=log_path,
        log_handle=log_handle,
        ended_at_ms=ended_at_ms,
        registry_retired_at_ms=ended_at_ms,
    )


@pytest.fixture
def daemon_context(tmp_path: Path) -> tuple[RunnerProcessManager, Path]:
    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    run_dir = live_runs_root / RUN_ID
    run_dir.mkdir(parents=True)
    return RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root), run_dir


async def test_health_reports_idle_process(daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["process"]["state"] == "idle"
    # Non-git tmp repo_root degrades to None/not-stale rather than raising.
    assert body["git_sha"] is None
    assert body["repo_head_sha"] is None
    assert body["code_stale"] is False
    assert body["commits_behind"] is None


def test_instances_prunes_exited_records_by_ttl_and_count(tmp_path: Path) -> None:
    now_ms = 10_000
    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    manager = RunnerProcessManager(
        repo_root=repo_root,
        live_runs_root=live_runs_root,
        exited_record_retention_count=2,
        exited_record_retention_ttl_ms=5_000,
        now_ms=lambda: now_ms,
    )
    _add_managed_process(manager, key="active", ended_at_ms=None, returncode=None)
    _add_managed_process(manager, key="old-exit", ended_at_ms=4_000, returncode=0)
    _add_managed_process(manager, key="new-exit-a", ended_at_ms=9_000, returncode=0)
    _add_managed_process(manager, key="new-exit-b", ended_at_ms=8_000, returncode=0)
    _add_managed_process(manager, key="new-exit-c", ended_at_ms=7_000, returncode=0)

    status = manager.instances()

    assert {instance.strategy_instance_id for instance in status.instances} == {
        "active",
        "new-exit-a",
        "new-exit-b",
    }
    assert status.exited_record_retention_count == 2
    assert status.exited_record_retention_ttl_ms == 5_000
    assert status.exited_record_count == 2
    assert status.exited_records_pruned_total == 2


async def test_broker_sockets_endpoint_returns_gateway_socket_rows(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    from app.engine.live import broker_socket_probe

    def fake_enumerate(self: object, gateway_port: int) -> list[GatewaySocketRow]:
        assert gateway_port == 4002
        return [
            GatewaySocketRow(
                pid=21760,
                command="python",
                argv=["python", "-m", "app.engine.live.run", "start"],
                run_dir=str(manager.live_runs_root / RUN_ID),
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ]

    monkeypatch.setattr(broker_socket_probe.LsofSocketEnumerator, "enumerate", fake_enumerate)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/broker/sockets?gateway_port=4002")

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_port"] == 4002
    assert body["sockets"][0]["pid"] == 21760
    assert body["sockets"][0]["run_dir"].endswith(RUN_ID)


@requires_git
async def test_health_reports_git_sha_of_executing_code() -> None:
    """The daemon surfaces the SHA of the code it is RUNNING (captured at launch)
    so an operator can confirm it is running the merged fixes — the daemon is
    long-lived and does NOT reload on `git pull`. With no pull since launch the
    running SHA equals the on-disk HEAD, so code is fresh.
    """
    import re

    repo_root = Path(__file__).resolve().parents[4]
    manager = RunnerProcessManager(
        repo_root=repo_root,
        live_runs_root=repo_root / "PythonDataService" / "artifacts" / "live_runs",
    )

    health = manager.health()

    assert health.git_sha is not None
    assert re.fullmatch(r"[0-9a-f]{40}", health.git_sha)
    # Running == on-disk: fresh, nothing behind.
    assert health.repo_head_sha == health.git_sha
    assert health.code_stale is False
    assert health.commits_behind is None


@requires_git
def test_health_flags_stale_code_when_launch_sha_behind_head(tmp_path: Path) -> None:
    """When the running (launch) SHA differs from the on-disk HEAD — the operator
    git-pulled but didn't restart the daemon — health flags code_stale and counts
    how far behind, so the UI can say 'restart to apply fixes'. This is the core
    of the freshness verdict; computing the SHA live (the old behavior) would
    have masked it by always reporting the on-disk HEAD.

    Uses a hermetic 4-commit tmp repo so commits_behind is deterministic and the
    test does not depend on the real repo's history depth (CI checks out a
    shallow clone, so HEAD~3 would not exist there).
    """
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        _sp.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t.local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    shas: list[str] = []
    for i in range(4):  # 4 linear commits → c0..c3, no merges
        (repo / "f.txt").write_text(str(i), encoding="utf-8")
        git("add", "f.txt")
        git("commit", "-q", "-m", f"c{i}", "--no-gpg-sign")
        shas.append(
            _sp.run(
                ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
            ).stdout.strip()
        )

    manager = RunnerProcessManager(repo_root=repo, live_runs_root=repo / "live_runs")
    # Daemon launched at the first commit; HEAD is now 3 commits ahead.
    manager._launch_git_sha = shas[0]

    health = manager.health()

    assert health.git_sha == shas[0]  # running = the (older) launch SHA
    assert health.repo_head_sha == shas[3]  # on-disk = current HEAD
    assert health.code_stale is True
    assert health.commits_behind == 3  # linear history → exact


def test_emergency_flatten_runs_cli_and_reports_success(
    daemon_context: tuple[RunnerProcessManager, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account-wide flatten reuses the one-shot emergency-flatten CLI: the
    manager spawns it (fixed argv, --confirm + --account), and exit 0 → accepted."""
    from app.engine.live import host_daemon as hd

    manager, run_dir = daemon_context
    captured: dict[str, list[str]] = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> _Result:
        captured["command"] = command
        return _Result()

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    resp = manager.emergency_flatten(RUN_ID, "DU123")

    assert resp.accepted is True
    cmd = captured["command"]
    assert cmd[1:4] == ["-m", "app.engine.live.run", "emergency-flatten"]
    assert "--confirm" in cmd
    assert "DU123" in cmd
    assert str(run_dir.resolve()) in cmd


def test_emergency_flatten_account_mismatch_maps_to_http_400(
    daemon_context: tuple[RunnerProcessManager, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI exit 2 (operator precondition — account mismatch / no --confirm) → 400."""
    from app.engine.live import host_daemon as hd
    from app.engine.live.host_daemon import HostRunnerError

    manager, _ = daemon_context

    class _Result:
        returncode = 2
        stdout = ""
        stderr = "account mismatch"

    monkeypatch.setattr(hd.subprocess, "run", lambda command, **_kwargs: _Result())

    with pytest.raises(HostRunnerError) as exc_info:
        manager.emergency_flatten(RUN_ID, "DU999")
    assert exc_info.value.status_code == 400


def test_emergency_flatten_broker_error_maps_to_http_502(
    daemon_context: tuple[RunnerProcessManager, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI exit 3 (broker/runtime) → 502, with the CLI stderr surfaced."""
    from app.engine.live import host_daemon as hd
    from app.engine.live.host_daemon import HostRunnerError

    manager, _ = daemon_context

    class _Result:
        returncode = 3
        stdout = ""
        stderr = "broker boom"

    monkeypatch.setattr(hd.subprocess, "run", lambda command, **_kwargs: _Result())

    with pytest.raises(HostRunnerError) as exc_info:
        manager.emergency_flatten(RUN_ID, "DU123")
    assert exc_info.value.status_code == 502
    assert "broker boom" in exc_info.value.detail


def test_emergency_flatten_rejects_concurrent_same_account(
    daemon_context: tuple[RunnerProcessManager, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second concurrent flatten for the same account is rejected (409), not run
    against the same pre-fill snapshot — preventing double-liquidation (Codex P1
    on #451). The account is released after the run so a later flatten proceeds.
    """
    from app.engine.live import host_daemon as hd
    from app.engine.live.host_daemon import HostRunnerError

    manager, _ = daemon_context
    reentrant: dict[str, int] = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> _Result:
        # While the first flatten holds the account, a re-entrant one is rejected.
        with pytest.raises(HostRunnerError) as exc_info:
            manager.emergency_flatten(RUN_ID, "DU123")
        reentrant["status"] = exc_info.value.status_code
        return _Result()

    monkeypatch.setattr(hd.subprocess, "run", fake_run)

    resp = manager.emergency_flatten(RUN_ID, "DU123")

    assert resp.accepted is True
    assert reentrant["status"] == 409
    # Released after completion — the in-flight set does not leak the account.
    assert "DU123" not in manager._flatten_in_flight


async def test_start_launches_existing_run_with_host_env(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    fake_process = FakeProcess()
    captured: dict[str, Any] = {}
    monkeypatch.setenv("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", "70-71")

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
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
    assert body["process"]["ibkr_client_id"] == 70
    assert "--readonly" in captured["command"]
    assert "--hydrate-policy" in captured["command"]
    assert "optional" in captured["command"]
    assert str(run_dir) in captured["command"]
    assert captured["kwargs"]["cwd"] == str(manager.repo_root)
    assert captured["kwargs"]["env"]["IBKR_HOST"] == "127.0.0.1"
    assert captured["kwargs"]["env"]["IBKR_CLIENT_ID"] == "70"
    assert "PythonDataService" in captured["kwargs"]["env"]["PYTHONPATH"]


def test_start_refuses_stopped_desired_state_before_spawn(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    DesiredStateRepo(
        stable_desired_state_path(manager.artifacts_root, "spy_ema_paper")
    ).set(
        DesiredState.STOPPED,
        updated_by="test",
        reason="regression",
        now_ms=1_700_000_000_000,
    )
    popen_called = False

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal popen_called
        popen_called = True
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert exc_info.value.status_code == 409
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail["reason_code"] == "STOPPED_REQUIRES_RESUME"
    assert exc_info.value.detail["gate_id"] == "desired_state.start"
    assert popen_called is False


def test_start_refuses_invalid_strategy_instance_id_before_desired_state_lookup(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "../bad",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    popen_called = False

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal popen_called
        popen_called = True
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert exc_info.value.status_code == 409
    assert "desired_state sidecar is unreadable" in str(exc_info.value.detail)
    assert popen_called is False


def test_start_refuses_unreadable_desired_state_sidecar(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    sidecar_path = stable_desired_state_path(manager.artifacts_root, "spy_ema_paper")
    sidecar_path.mkdir(parents=True)
    popen_called = False

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        nonlocal popen_called
        popen_called = True
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert exc_info.value.status_code == 409
    assert "desired_state sidecar is unreadable" in str(exc_info.value.detail)
    assert popen_called is False


async def test_start_allocates_distinct_ibkr_client_ids_for_sibling_instances(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    second_run_id = "run-daemon-" + "b" * 53
    second_run_dir = manager.live_runs_root / second_run_id
    second_run_dir.mkdir(parents=True)
    monkeypatch.setenv("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", "80-81")
    captured_ids: list[str] = []

    def _write_ledger(path: Path, strategy_instance_id: str, run_id: str, account_id: str) -> None:
        (path / "run_ledger.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "strategy_instance_id": strategy_instance_id,
                    "account_id": account_id,
                }
            ),
            encoding="utf-8",
        )

    _write_ledger(run_dir, "first-bot", RUN_ID, "DU111")
    _write_ledger(second_run_dir, "second-bot", second_run_id, "DU112")

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured_ids.append(kwargs["env"]["IBKR_CLIENT_ID"])
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    first = manager.start(RUN_ID, request=HostRunnerStartRequest())
    second = manager.start(second_run_id, request=HostRunnerStartRequest())

    assert first.accepted is True
    assert second.accepted is True
    assert captured_ids == ["80", "81"]


def test_parse_ibkr_client_id_pool_rejects_zero_and_huge_ranges() -> None:
    assert _parse_ibkr_client_id_pool("50,52-53,52") == (50, 52, 53)

    with pytest.raises(ValueError, match="outside"):
        _parse_ibkr_client_id_pool("0")
    with pytest.raises(ValueError, match="outside"):
        _parse_ibkr_client_id_pool("0-2")
    with pytest.raises(ValueError, match="maximum"):
        _parse_ibkr_client_id_pool("1-5000")


def test_start_does_not_write_active_binding_when_client_id_pool_invalid(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live.host_daemon import HostRunnerError

    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", "1-5000")

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert exc_info.value.status_code == 503
    assert read_account_instance_registry(manager.artifacts_root, "DU111") == []


def test_concurrent_starts_cannot_allocate_same_ibkr_client_id(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, first_run_dir = daemon_context
    first_run_id = RUN_ID
    second_run_id = "run-daemon-" + "b" * 53
    second_run_dir = manager.live_runs_root / second_run_id
    second_run_dir.mkdir(parents=True)
    monkeypatch.setenv("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", "90-91")

    def _write_ledger(path: Path, strategy_instance_id: str, run_id: str) -> None:
        (path / "run_ledger.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "strategy_instance_id": strategy_instance_id,
                    "account_id": "DU111",
                }
            ),
            encoding="utf-8",
        )

    _write_ledger(first_run_dir, "first-bot", first_run_id)
    _write_ledger(second_run_dir, "second-bot", second_run_id)

    captured_ids: list[str] = []
    captured_lock = threading.Lock()
    first_popen_entered = threading.Event()
    allow_first_popen_to_finish = threading.Event()

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        del command
        with captured_lock:
            captured_ids.append(kwargs["env"]["IBKR_CLIENT_ID"])
            call_number = len(captured_ids)
        if call_number == 1:
            first_popen_entered.set()
            assert allow_first_popen_to_finish.wait(timeout=2.0)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    errors: list[BaseException] = []

    def _start(run_id: str) -> None:
        try:
            manager.start(run_id, request=HostRunnerStartRequest())
        except BaseException as exc:  # pragma: no cover - reported after join
            errors.append(exc)

    first = threading.Thread(target=_start, args=(first_run_id,))
    second = threading.Thread(target=_start, args=(second_run_id,))
    first.start()
    assert first_popen_entered.wait(timeout=2.0)
    second.start()
    allow_first_popen_to_finish.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert captured_ids == ["90", "91"]


def test_retry_skips_ibkr_client_id_rejected_by_gateway(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    monkeypatch.setenv("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", "50-51")
    processes = [FakeProcess(), FakeProcess()]
    captured_ids: list[str] = []

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        del command
        captured_ids.append(kwargs["env"]["IBKR_CLIENT_ID"])
        return processes[len(captured_ids) - 1]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    manager.start(RUN_ID, request=HostRunnerStartRequest())
    processes[0].returncode = 3
    (run_dir / "run_status.json").write_text(
        json.dumps({"exit_error_code": "IBKR_CLIENT_ID_IN_USE"}),
        encoding="utf-8",
    )
    manager.process_status(RUN_ID)

    manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert captured_ids == ["50", "51"]


async def test_start_writes_account_registry_before_spawn(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
        assert bindings
        assert bindings[-1].strategy_instance_id == "spy_ema_paper"
        assert bindings[-1].run_id == RUN_ID
        assert bindings[-1].bot_order_namespace == "learn-ai/spy_ema_paper/v1"
        assert bindings[-1].lifecycle_state == "ACTIVE"
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post(f"/runs/{RUN_ID}/start", json={})

    assert response.status_code == 200


async def test_start_retires_account_registry_binding_when_spawn_fails(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live.host_daemon import HostRunnerError

    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        raise OSError("spawn failed")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(HostRunnerError):
        manager.start(RUN_ID, request=HostRunnerStartRequest())

    bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
    assert [binding.lifecycle_state for binding in bindings[-2:]] == ["ACTIVE", "RETIRED"]
    assert bindings[-1].source == "host_daemon.start_failed"
    events = BotEventRawWal(run_bot_event_wal_path(run_dir)).read_all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type is BotEventRawType.LAUNCH_FAILED
    assert event.source_authority is SourceAuthority.DAEMON_LAUNCHER
    assert event.strategy_instance_id == "spy_ema_paper"
    assert event.run_id == RUN_ID
    assert event.identity.evaluation_id == f"launch:{RUN_ID}"
    assert event.terminal_error is not None
    assert event.terminal_error.code is TerminalErrorCode.LAUNCH_FAILED
    assert event.terminal_error.source is TerminalErrorSource.OS
    assert event.terminal_error.gate_id == "daemon.spawn"
    assert event.terminal_error.external_message == "spawn failed"
    assert event.facts["failure_stage"] == "spawn"
    incidents = IncidentStore(run_dir).list_unresolved()
    assert [incident.notice.code for incident in incidents] == ["submit.launch_failed"]


async def test_child_without_status_retires_account_registry_binding_as_ended_without_status(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "host_daemon.log").write_text(
        "Traceback (most recent call last):\nRuntimeError: boot boom\n",
        encoding="utf-8",
    )
    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)

    manager.start(RUN_ID, request=HostRunnerStartRequest())
    fake_process.returncode = -9

    status = manager.process_status(RUN_ID)

    assert status.state == HostRunnerProcessState.exited
    assert status.exit_reason == "exited(-9)"
    bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
    assert [binding.lifecycle_state for binding in bindings[-2:]] == ["ACTIVE", "RETIRED"]
    assert bindings[-1].source == "host_daemon.ended_without_status"
    events = BotEventRawWal(run_bot_event_wal_path(run_dir)).read_all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type is BotEventRawType.LAUNCH_FAILED
    assert event.source_authority is SourceAuthority.DAEMON_LAUNCHER
    assert event.strategy_instance_id == "spy_ema_paper"
    assert event.identity.evaluation_id == f"launch:{RUN_ID}"
    assert event.terminal_error is not None
    assert event.terminal_error.code is TerminalErrorCode.LAUNCH_FAILED
    assert event.terminal_error.source is TerminalErrorSource.DAEMON
    assert event.terminal_error.gate_id == "daemon.child_process"
    assert event.terminal_error.external_code == -9
    assert "RuntimeError: boot boom" in str(event.terminal_error.external_message)
    assert event.facts["failure_stage"] == "child_process"
    # A second status refresh must not duplicate the visible terminal story.
    manager.process_status(RUN_ID)
    assert len(BotEventRawWal(run_bot_event_wal_path(run_dir)).read_all()) == 1
    incidents = IncidentStore(run_dir).list_unresolved()
    assert [incident.notice.code for incident in incidents] == ["submit.launch_failed"]
    _owned, siblings = compute_reconcile_namespaces(
        artifacts_root=manager.artifacts_root,
        account_id="DU111",
        current_namespace="learn-ai/other_bot/v1",
    )
    assert "learn-ai/spy_ema_paper/v1" not in siblings


async def test_child_controlled_halt_does_not_emit_launch_failed_event(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id=RUN_ID,
            started_at_ms=1_700_000_000_000,
            last_update_ms=1_700_000_010_000,
            ended_at_ms=1_700_000_010_000,
            exit_code=1,
            exit_reason=ExitReason.fatal_halt,
            host_pid=4242,
        ),
    )
    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)

    manager.start(RUN_ID, request=HostRunnerStartRequest())
    fake_process.returncode = 1

    status = manager.process_status(RUN_ID)

    assert status.state == HostRunnerProcessState.exited
    bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
    assert [binding.lifecycle_state for binding in bindings[-2:]] == ["ACTIVE", "RETIRED"]
    assert bindings[-1].source == "host_daemon.process_halted"
    assert BotEventRawWal(run_bot_event_wal_path(run_dir)).read_all() == []


async def test_child_unclassified_exit_does_not_write_clean_retirement_source(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "started_at_ms": 1_700_000_000_000,
                "last_update_ms": 1_700_000_010_000,
                "ended_at_ms": 1_700_000_010_000,
                "exit_code": 0,
                "exit_reason": "legacy_unknown",
                "host_pid": 4242,
            }
        ),
        encoding="utf-8",
    )
    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)

    manager.start(RUN_ID, request=HostRunnerStartRequest())
    fake_process.returncode = 0

    status = manager.process_status(RUN_ID)

    assert status.state == HostRunnerProcessState.exited
    bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
    assert [binding.lifecycle_state for binding in bindings[-2:]] == ["ACTIVE", "RETIRED"]
    assert bindings[-1].source == "host_daemon.ended_without_status"


async def test_start_blocks_after_crash_retire_until_later_recovery_proof(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live.host_daemon import HostRunnerError

    manager, run_dir = daemon_context
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    fixed_ms = 1_700_000_020_000
    from app.engine.live import host_daemon as hd

    monkeypatch.setattr(hd, "_now_ms", lambda: fixed_ms)
    first_process = FakeProcess()
    second_process = FakeProcess()
    popen_results = iter([first_process, second_process])
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: next(popen_results))

    manager.start(RUN_ID, request=HostRunnerStartRequest())
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id=RUN_ID,
            started_at_ms=1_700_000_000_000,
            last_update_ms=1_700_000_010_000,
            ended_at_ms=1_700_000_010_000,
            exit_code=3,
            exit_reason=ExitReason.exception,
            host_pid=4242,
        ),
    )
    first_process.returncode = -9
    manager.process_status(RUN_ID)

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(RUN_ID, request=HostRunnerStartRequest())
    assert exc_info.value.status_code == 409
    assert "recovery proof" in exc_info.value.detail

    append_account_event(
        manager.artifacts_root,
        "DU111",
        {
            "event_type": "account_recovery_proof_recorded",
            "recorded_at_ms": fixed_ms + 1,
            "recovery_id": "proof-1",
            "reconciliation_result": "clean",
        },
    )

    response = manager.start(RUN_ID, request=HostRunnerStartRequest())

    assert response.accepted is True
    assert response.process.pid == second_process.pid


async def test_start_blocks_when_restart_intensity_freezes_account(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live import host_daemon as hd

    manager, run_dir = daemon_context
    fixed_now_ms = 1_700_000_020_000
    monkeypatch.setattr(hd, "_now_ms", lambda: fixed_now_ms)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "strategy_instance_id": "spy_ema_paper",
                "account_id": "DU111",
            }
        ),
        encoding="utf-8",
    )
    for index, recorded_at_ms in enumerate((fixed_now_ms - 20_000, fixed_now_ms - 10_000), start=1):
        write_account_instance_binding(
            manager.artifacts_root,
            AccountInstanceBinding(
                account_id="DU111",
                strategy_instance_id=f"prior-{index}",
                run_id=f"prior-run-{index}",
                bot_order_namespace=bot_order_namespace_for_instance(f"prior-{index}"),
                lifecycle_state="ACTIVE",
                recorded_at_ms=recorded_at_ms,
                source="test",
            ),
        )

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        raise AssertionError("restart intensity freeze should block before spawn")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post(f"/runs/{RUN_ID}/start", json={})

    assert response.status_code == 409
    freeze = read_account_freeze(manager.artifacts_root, "DU111")
    assert freeze is not None
    assert freeze.source == "account_restart_intensity"


async def test_start_rejects_second_active_run(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = daemon_context
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess())
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        first = await client.post(f"/runs/{RUN_ID}/start", json={})
        second = await client.post(f"/runs/{RUN_ID}/start", json={})

    assert first.status_code == 200
    assert second.status_code == 409


async def test_start_rejects_missing_run(daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/runs/missing-run/start", json={})

    assert response.status_code == 404


async def test_stop_force_kills_when_graceful_signal_does_not_exit(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = daemon_context
    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        start = await client.post(f"/runs/{RUN_ID}/start", json={})
        stop = await client.post(f"/runs/{RUN_ID}/stop", json={"force": True})

    assert start.status_code == 200
    assert stop.status_code == 200
    assert fake_process.signals
    assert fake_process.killed is True
    assert stop.json()["process"]["state"] == "exited"
    assert stop.json()["process"]["exit_code"] == -9


async def test_stop_handles_process_exiting_between_poll_and_signal(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process exits in the TOCTOU window between poll() and send_signal()."""
    manager, _ = daemon_context

    class RacingProcess(FakeProcess):
        def send_signal(self, sig: int) -> None:
            self.returncode = 0
            raise OSError("process already exited")

    fake_process = RacingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: fake_process)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        start = await client.post(f"/runs/{RUN_ID}/start", json={})
        stop = await client.post(f"/runs/{RUN_ID}/stop", json={"force": False})

    assert start.status_code == 200
    assert stop.status_code == 200
    assert stop.json()["accepted"] is False


async def test_instances_lists_each_managed_strategy_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry keys by strategy_instance_id, so an executing and a shadow
    instance coexist as separate processes and both surface on /instances."""
    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    runs = {
        "run-exec-" + "a" * 52: "spy_ema_paper",
        "run-shadow-" + "b" * 50: "spy_vwap_shadow",
    }
    for run_id, sid in runs.items():
        run_dir = live_runs_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run_ledger.json").write_text(json.dumps({"strategy_instance_id": sid}), encoding="utf-8")

    manager = RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess())
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        for run_id in runs:
            started = await client.post(f"/runs/{run_id}/start", json={})
            assert started.status_code == 200  # different instances coexist
        listing = await client.get("/instances")
        exec_process = await client.get("/instances/spy_ema_paper/process")
        missing = await client.get("/instances/no_such_instance/process")

    assert listing.status_code == 200
    body = listing.json()
    by_sid = {inst["strategy_instance_id"]: inst for inst in body["instances"]}
    assert set(by_sid) == {"spy_ema_paper", "spy_vwap_shadow"}
    for sid, inst in by_sid.items():
        assert inst["run_id"] in runs
        assert inst["process"]["state"] == "running"
        assert inst["process"]["strategy_instance_id"] == sid

    assert exec_process.status_code == 200
    assert exec_process.json()["state"] == "running"
    assert exec_process.json()["strategy_instance_id"] == "spy_ema_paper"

    assert missing.status_code == 200
    assert missing.json()["state"] == "idle"


async def test_start_falls_back_to_run_id_key_without_ledger_binding(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy run with no strategy_instance_id keys by run_id and still
    surfaces on /instances with an empty instance id."""
    manager, _ = daemon_context
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: FakeProcess())
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        started = await client.post(f"/runs/{RUN_ID}/start", json={})
        listing = await client.get("/instances")

    assert started.status_code == 200
    instances = listing.json()["instances"]
    assert len(instances) == 1
    assert instances[0]["run_id"] == RUN_ID
    assert instances[0]["strategy_instance_id"] == ""


async def test_start_injects_sibling_managed_symbols(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting a second instance injects the running sibling's symbol via
    --managed-symbols so the unexpected-position gate excludes it (#395/#398)."""
    from app.engine.strategy.spec import schema as spec_schema

    fixture = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"
    expected_symbol = json.loads(fixture.read_text(encoding="utf-8"))["symbols"][0]

    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    ema_run = "run-ema-" + "a" * 54
    vwap_run = "run-vwap-" + "b" * 52
    for run_id, sid in ((ema_run, "spy_ema"), (vwap_run, "spy_vwap")):
        run_dir = live_runs_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run_ledger.json").write_text(
            json.dumps({"strategy_instance_id": sid, "strategy_spec_path": str(fixture)}),
            encoding="utf-8",
        )

    manager = RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)
    captured: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured.append(command)
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        await client.post(f"/runs/{ema_run}/start", json={})
        await client.post(f"/runs/{vwap_run}/start", json={})

    # First start has no running sibling -> no --managed-symbols.
    assert "--managed-symbols" not in captured[0]
    # Second start carries the running EMA instance's symbol.
    assert "--managed-symbols" in captured[1]
    idx = captured[1].index("--managed-symbols")
    assert captured[1][idx + 1] == expected_symbol


async def test_sibling_symbols_resolves_relative_spec_paths_from_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledgers store repo-relative spec paths; daemon cwd may be PythonDataService."""
    from app.engine.strategy.spec import schema as spec_schema

    source_fixture = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"
    expected_symbol = json.loads(source_fixture.read_text(encoding="utf-8"))["symbols"][0]

    repo_root = tmp_path / "repo"
    live_runs_root = repo_root / "PythonDataService" / "artifacts" / "live_runs"
    fixture = (
        repo_root
        / "PythonDataService"
        / "app"
        / "engine"
        / "strategy"
        / "spec"
        / "fixtures"
        / "spy_ema_crossover.spec.json"
    )
    fixture.parent.mkdir(parents=True)
    fixture.write_text(source_fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (repo_root / "PythonDataService").mkdir(exist_ok=True)

    ema_run = "run-ema-" + "a" * 54
    vwap_run = "run-vwap-" + "b" * 52
    for run_id, sid in ((ema_run, "spy_ema"), (vwap_run, "spy_vwap")):
        run_dir = live_runs_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run_ledger.json").write_text(
            json.dumps(
                {
                    "strategy_instance_id": sid,
                    "strategy_spec_path": "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json",
                }
            ),
            encoding="utf-8",
        )

    manager = RunnerProcessManager(repo_root=repo_root, live_runs_root=live_runs_root)
    captured: list[list[str]] = []

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured.append(command)
        return FakeProcess()

    monkeypatch.chdir(repo_root / "PythonDataService")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        await client.post(f"/runs/{ema_run}/start", json={})
        await client.post(f"/runs/{vwap_run}/start", json={})

    assert "--managed-symbols" in captured[1]
    idx = captured[1].index("--managed-symbols")
    assert captured[1][idx + 1] == expected_symbol


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "8.8.8.8"])
def test_build_parser_accepts_non_loopback_host(host: str) -> None:
    # Non-loopback binds are allowed now that auth is mandatory (ADR 0007) — the
    # data-plane container reaches the daemon on a non-loopback interface.
    parser = build_parser()
    args = parser.parse_args(["--host", host])
    assert args.host == host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_build_parser_accepts_loopback_host(host: str) -> None:
    parser = build_parser()
    args = parser.parse_args(["--host", host])
    assert args.host == host


def test_build_parser_rejects_garbage_host() -> None:
    parser = build_parser()
    with pytest.raises((SystemExit, argparse.ArgumentTypeError)):
        parser.parse_args(["--host", "not-a-host"])


async def test_protected_route_rejects_missing_token(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/instances")

    assert response.status_code == 401
    assert TOKEN_HEADER in response.json()["detail"]


async def test_protected_route_rejects_wrong_token(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={TOKEN_HEADER: "wrong"}
    ) as client:
        response = await client.get("/instances")

    assert response.status_code == 401


async def test_protected_route_accepts_correct_token(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/instances")

    assert response.status_code == 200


async def test_protected_route_rejects_prefix_of_token_vcr_0011(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    # VCR-0011: token compare must be constant-time (hmac.compare_digest).
    # A token that is a strict prefix of the real one must still be rejected
    # with no length-dependent fast path. Asserts the equality semantics the
    # constant-time compare preserves; timing-stability assertions are out
    # of scope for unit tests.
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)
    prefix = _TEST_TOKEN[: max(1, len(_TEST_TOKEN) // 2)]
    assert prefix != _TEST_TOKEN

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={TOKEN_HEADER: prefix},
    ) as client:
        response = await client.get("/instances")

    assert response.status_code == 401


async def test_health_requires_token(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    # PRD #619-C followup (Codex P2): /health is now auth-gated so the
    # connectivity monitor's AUTH_FAILED classification is reachable.
    # Without a token: 401. The data plane probe holds the token via
    # the artifacts bind mount.
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 401


def test_importing_host_daemon_does_not_generate_token(tmp_path: Path) -> None:
    # Regression (ADR 0007 P1): importing the module must NOT generate a token.
    # The old module-level ``app = create_app()`` ran at import and, via the
    # default path, generated one from the cwd — under systemd's
    # WorkingDirectory=PythonDataService that wrote a doubly-nested,
    # un-ignored token file that tripped the deploy dirty-tree gate.
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    env.pop("LIVE_RUNNER_DAEMON_TOKEN", None)
    subprocess.run(
        [sys.executable, "-c", "import app.engine.live.host_daemon"],
        cwd=str(tmp_path),
        env=env,
        check=True,
        capture_output=True,
    )

    assert list(tmp_path.rglob(".host-daemon-token")) == []


# ── deploy (ADR 0006) ────────────────────────────────────────────────

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in this environment",
)


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


@pytest.fixture
def git_daemon_context(tmp_path: Path) -> tuple[RunnerProcessManager, Path]:
    """Daemon manager whose repo_root is a clean git repo holding a spec + QC
    audit copy. Returns ``(manager, repo_root)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "PythonDataService").mkdir()
    (repo / "PythonDataService" / "spec.json").write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    (repo / "references" / "qc-shadow").mkdir(parents=True)
    (repo / "references" / "qc-shadow" / "SpyEmaCrossoverAlgorithm.py").write_text(
        "# QC audit copy\n", encoding="utf-8"
    )
    (repo / "references" / "qc-shadow" / "DeploymentValidationAlgorithm.py").write_text(
        "# Deployment validation QC audit copy\n", encoding="utf-8"
    )
    # Mirror the real repo: live-run artifacts are gitignored, so writing a
    # run_ledger under PythonDataService/artifacts does NOT dirty the tree (the
    # clean-tree scope includes PythonDataService). Without this, a second
    # deploy would see the first deploy's output as an uncommitted change.
    (repo / ".gitignore").write_text("PythonDataService/artifacts/\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            ".gitignore",
            "PythonDataService/spec.json",
            "references/qc-shadow/DeploymentValidationAlgorithm.py",
            "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "seed", "--no-gpg-sign"], cwd=repo, check=True)

    live_runs_root = repo / "PythonDataService" / "artifacts" / "live_runs"
    manager = RunnerProcessManager(repo_root=repo, live_runs_root=live_runs_root)
    return manager, repo


def _deploy_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "strategy_spec_path": "PythonDataService/spec.json",
        "qc_audit_copy_path": "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1700000000000,
        "strategy_instance_id": "spy-ema-paper-1",
        "live_config": {
            "symbol": "SPY",
            "sizing": {"kind": "FixedShares", "value": 1},
        },
    }
    body.update(overrides)
    return body


def _add_deployment_validation_spec(repo: Path) -> str:
    relative_path = Path("PythonDataService") / "deployment_validation.spec.json"
    (repo / relative_path).write_text('{"strategy": "deployment_validation"}', encoding="utf-8")
    subprocess.run(["git", "add", str(relative_path)], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add deployment validation spec", "--no-gpg-sign"],
        cwd=repo,
        check=True,
    )
    return relative_path.as_posix()


@requires_git
async def test_deploy_creates_run(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, repo = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body())

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["start"] is None
    run_dir = repo / "PythonDataService" / "artifacts" / "live_runs" / body["run_id"]
    assert (run_dir / "run_ledger.json").is_file()


@requires_git
async def test_deploy_rejects_empty_deployment_validation_plan_with_gate_detail(
    git_daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, repo = git_daemon_context
    spec_path = _add_deployment_validation_spec(repo)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)
    body = _deploy_body(
        strategy_spec_path=spec_path,
        qc_audit_copy_path="references/qc-shadow/DeploymentValidationAlgorithm.py",
        strategy_key="deployment_validation",
        strategy_instance_id="dep-val-empty",
        live_config={
            "symbol": "SPY",
            "sizing": {"kind": "FixedShares", "value": 1},
            "action": {"on_enter": [], "on_exit": []},
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=body)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "ACTION_PLAN_EMPTY"
    assert detail["gate_id"] == "deploy.action_plan"
    assert "Action plan" in detail["remediation"]
    assert not manager.live_runs_root.exists()


@requires_git
async def test_deploy_writes_account_registry_binding(
    git_daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body())

    assert response.status_code == 200
    body = response.json()
    bindings = read_account_instance_registry(manager.artifacts_root, "DU111")
    assert bindings[-1].strategy_instance_id == "spy-ema-paper-1"
    assert bindings[-1].run_id == body["run_id"]
    assert bindings[-1].bot_order_namespace == "learn-ai/spy-ema-paper-1/v1"
    assert bindings[-1].lifecycle_state == "DEPLOYED"


@requires_git
async def test_deploy_is_idempotent(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        first = await client.post("/deploy", json=_deploy_body())
        second = await client.post("/deploy", json=_deploy_body())

    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["run_id"] == first.json()["run_id"]


@requires_git
async def test_deploy_rejects_dirty_tree(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, repo = git_daemon_context
    (repo / "PythonDataService" / "spec.json").write_text('{"strategy": "x"}', encoding="utf-8")
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body())

    assert response.status_code == 409


@requires_git
async def test_deploy_rejects_missing_spec(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body(strategy_spec_path="PythonDataService/nope.json"))

    assert response.status_code == 400


@requires_git
async def test_deploy_rejects_path_escape(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body(strategy_spec_path="../../../etc/passwd"))

    assert response.status_code == 400


@requires_git
async def test_deploy_with_start_chains_a_launch(
    git_daemon_context: tuple[RunnerProcessManager, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = git_daemon_context
    # Patch the manager's start (not subprocess.Popen): deploy_run still needs a
    # real `git rev-parse HEAD` via subprocess, so a global Popen fake would
    # break the deploy itself. This asserts the chaining wiring directly.
    calls: dict[str, Any] = {}
    canned = HostRunnerActionResponse(
        accepted=True,
        process=HostRunnerProcessStatus(state=HostRunnerProcessState.running, pid=4242),
    )

    def fake_start(run_id: str, request: Any) -> HostRunnerActionResponse:
        calls["run_id"] = run_id
        return canned

    monkeypatch.setattr(manager, "start", fake_start)
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post("/deploy", json=_deploy_body(start=True))

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["start"]["accepted"] is True
    assert calls["run_id"] == body["run_id"]


@requires_git
async def test_qc_audit_copies_lists_committed_files(
    git_daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/qc-audit-copies")

    assert response.status_code == 200
    body = response.json()
    assert body["scope_root"] == "references/qc-shadow"
    assert "references/qc-shadow/SpyEmaCrossoverAlgorithm.py" in body["entries"]
    assert "references/qc-shadow/DeploymentValidationAlgorithm.py" in body["entries"]


@requires_git
async def test_qc_audit_copies_excludes_untracked_files(
    git_daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    """ADR 0006 provenance: only committed copies are deploy-eligible. An
    untracked file under references/qc-shadow must NOT be listed — otherwise the
    UI offers a deploy option not backed by committed source."""
    manager, repo = git_daemon_context
    untracked = repo / "references" / "qc-shadow" / "UncommittedAlgorithm.py"
    untracked.write_text("# not committed\n", encoding="utf-8")
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/qc-audit-copies")

    entries = response.json()["entries"]
    assert "references/qc-shadow/SpyEmaCrossoverAlgorithm.py" in entries  # committed
    assert "references/qc-shadow/DeploymentValidationAlgorithm.py" in entries  # committed
    assert "references/qc-shadow/UncommittedAlgorithm.py" not in entries  # untracked


def test_build_start_command_passes_sibling_all_in_symbols(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    """ADR 0009 PR5 reviewer fix — the host-runner start command now carries
    --sibling-all-in-symbols so cmd_start's coexistence guard can detect a
    sibling SetHoldings(1.0) instance even before the broker observes its
    exposure."""
    from app.schemas.live_runs import HostRunnerStartRequest

    manager, run_dir = daemon_context
    request = HostRunnerStartRequest(
        readonly=True,
        hydrate_policy="optional",
        strategy="spy_ema_crossover",
        max_orders_per_day=3,
        ibkr_host="127.0.0.1",
    )
    command = manager._build_start_command(
        run_dir,
        request,
        managed_symbols={"AAPL"},
        sibling_all_in_symbols={"SPY"},
    )
    assert "--sibling-all-in-symbols" in command
    idx = command.index("--sibling-all-in-symbols")
    assert command[idx + 1] == "SPY"
    # The unrelated managed-symbols arg still rides through too.
    assert "--managed-symbols" in command


def test_build_start_command_omits_sibling_all_in_when_empty(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    from app.schemas.live_runs import HostRunnerStartRequest

    manager, run_dir = daemon_context
    request = HostRunnerStartRequest(strategy="spy_ema_crossover")
    command = manager._build_start_command(
        run_dir,
        request,
        managed_symbols=set(),
        sibling_all_in_symbols=set(),
    )
    assert "--sibling-all-in-symbols" not in command


def test_start_rejects_unallowlisted_ibkr_host_at_daemon_boundary(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live.host_daemon import HostRunnerError
    from app.schemas.live_runs import HostRunnerStartRequest

    monkeypatch.delenv("IBKR_HOST", raising=False)
    monkeypatch.delenv("IBKR_HOST_ALLOWLIST", raising=False)
    manager, _ = daemon_context

    with pytest.raises(HostRunnerError) as exc_info:
        manager.start(
            RUN_ID,
            request=HostRunnerStartRequest(ibkr_host="192.168.1.50"),
        )

    assert exc_info.value.status_code == 400
    assert "host-daemon allow-list" in exc_info.value.detail


def test_main_env_file_feeds_daemon_host_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.engine.live import host_daemon

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("IBKR_HOST_ALLOWLIST=192.168.1.50\n", encoding="utf-8")
    monkeypatch.delenv("IBKR_HOST", raising=False)
    monkeypatch.delenv("IBKR_HOST_ALLOWLIST", raising=False)
    monkeypatch.setattr(host_daemon, "ensure_daemon_token", lambda _artifacts_root: _TEST_TOKEN)

    import uvicorn

    served: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        served["app"] = app
        served["host"] = host
        served["port"] = port
        assert validate_ibkr_host_allowed("192.168.1.50") == "192.168.1.50"

    monkeypatch.setattr(uvicorn, "run", fake_run)

    try:
        assert host_daemon.main(["--repo-root", str(repo_root), "--env-file", str(env_file)]) == 0
    finally:
        monkeypatch.delenv("IBKR_HOST_ALLOWLIST", raising=False)

    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8765


def test_sibling_all_in_symbols_detects_set_holdings_full(
    daemon_context: tuple[RunnerProcessManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling whose ledger pins SetHoldings(1.0) contributes its symbol;
    a sibling with FixedShares contributes nothing.
    """
    from app.engine.live.host_daemon import ManagedProcess

    manager, _ = daemon_context

    # Bypass the real Pydantic spec loader: the test only exercises the
    # sizing-shape filter; symbol resolution is a separate concern verified
    # elsewhere.
    monkeypatch.setattr(
        manager,
        "_resolve_symbol",
        lambda run_dir: json.loads((run_dir / "run_ledger.json").read_text()).get("live_config", {}).get("symbol"),
    )

    def _write_sibling(key: str, sizing: dict | None, symbol: str) -> ManagedProcess:
        run_id = "sib-" + key + "a" * (60 - len(key))
        rdir = manager.live_runs_root / run_id
        rdir.mkdir(parents=True)
        ledger = {
            "schema_version": "1.3",
            "run_id": run_id,
            "code_sha": "abc",
            "strategy_spec_path": "PythonDataService/spec.json",
            "strategy_spec_sha256": "x" * 64,
            "qc_audit_copy_path": "references/qc-shadow/X.py",
            "qc_audit_copy_sha256": "y" * 64,
            "qc_cloud_backtest_id": "bt-1",
            "account_id": "DU111",
            "start_date_ms": 0,
            "live_config": {"symbol": symbol, **({"sizing": sizing} if sizing else {})},
            "created_at_ms": 0,
        }
        (rdir / "run_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        log_path = rdir / "host_daemon.log"
        managed = ManagedProcess(
            strategy_instance_id=key,
            run_id=run_id,
            run_dir=rdir,
            process=FakeProcess(),
            command=[],
            started_at_ms=0,
            log_path=log_path,
            log_handle=log_path.open("a", encoding="utf-8"),
        )
        manager._managed[key] = managed
        return managed

    _write_sibling("sib_full", {"kind": "SetHoldings", "fraction": "1.0"}, "SPY")
    _write_sibling("sib_canary", {"kind": "FixedShares", "value": 1}, "QQQ")
    _write_sibling("sib_legacy", None, "TSLA")

    # `_sibling_all_in_symbols(key)` excludes the key from its own scan; pass a
    # synthetic excluded key so all three siblings are surveyed.
    all_in = manager._sibling_all_in_symbols("self")
    assert all_in == {"SPY"}


async def test_qc_audit_copies_empty_when_dir_absent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "PythonDataService").mkdir(parents=True)
    manager = RunnerProcessManager(
        repo_root=repo, live_runs_root=repo / "PythonDataService" / "artifacts" / "live_runs"
    )
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.get("/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == []
