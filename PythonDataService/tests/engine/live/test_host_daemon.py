"""Tests for the host-side live-run daemon."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.live.daemon_auth import TOKEN_HEADER
from app.engine.live.host_daemon import RunnerProcessManager, build_parser, create_app
from app.schemas.live_runs import (
    HostRunnerActionResponse,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
)

RUN_ID = "run-daemon-" + "a" * 53
# Every protected route requires the shared secret (ADR 0007); tests pin a known
# token into create_app and send it on the client via the _AUTH header.
_TEST_TOKEN = "test-daemon-token"
_AUTH = {TOKEN_HEADER: _TEST_TOKEN}


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


async def test_health_flags_stale_code_when_launch_sha_behind_head() -> None:
    """When the running (launch) SHA differs from the on-disk HEAD — the operator
    git-pulled but didn't restart the daemon — health flags code_stale and counts
    how far behind, so the UI can say 'restart to apply fixes'. This is the core
    of the freshness verdict; computing the SHA live (the old behavior) would
    have masked it by always reporting the on-disk HEAD.
    """
    import subprocess as _sp

    repo_root = Path(__file__).resolve().parents[4]
    manager = RunnerProcessManager(
        repo_root=repo_root,
        live_runs_root=repo_root / "PythonDataService" / "artifacts" / "live_runs",
    )
    # Simulate the daemon having launched 3 commits ago, then a pull moved HEAD.
    older = _sp.run(
        ["git", "rev-parse", "HEAD~3"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manager._launch_git_sha = older

    health = manager.health()

    assert health.git_sha == older  # running = the (older) launch SHA
    assert health.repo_head_sha != older  # on-disk = current HEAD
    assert health.code_stale is True
    # rev-list count of older..HEAD: at least the 3 first-parent hops (more when
    # merge commits pulled in branch commits) — the contract is a positive count.
    assert health.commits_behind is not None and health.commits_behind >= 3


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


async def test_instances_lists_each_managed_strategy_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        (run_dir / "run_ledger.json").write_text(
            json.dumps({"strategy_instance_id": sid}), encoding="utf-8"
        )

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


async def test_start_injects_sibling_managed_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


async def test_health_is_open_without_token(
    daemon_context: tuple[RunnerProcessManager, Path],
) -> None:
    # /health must stay unauthenticated so the data plane's connectivity probe
    # works before any token is wired (ADR 0007).
    manager, _ = daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200


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
    (repo / "PythonDataService" / "spec.json").write_text(
        '{"strategy": "spy_ema_crossover"}', encoding="utf-8"
    )
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
        "live_config": {"symbol": "SPY"},
    }
    body.update(overrides)
    return body


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
        response = await client.post(
            "/deploy", json=_deploy_body(strategy_spec_path="PythonDataService/nope.json")
        )

    assert response.status_code == 400


@requires_git
async def test_deploy_rejects_path_escape(git_daemon_context: tuple[RunnerProcessManager, Path]) -> None:
    manager, _ = git_daemon_context
    app = create_app(manager, allowed_origins=["http://localhost:4200"], auth_token=_TEST_TOKEN)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=_AUTH) as client:
        response = await client.post(
            "/deploy", json=_deploy_body(strategy_spec_path="../../../etc/passwd")
        )

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
