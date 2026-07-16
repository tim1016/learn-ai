"""Contract tests for the local host-daemon launcher script."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def test_start_live_daemon_print_launch_env_passes_env_file_to_policy(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "IBKR_HOST_ALLOWLIST=192.168.1.50,192.168.1.51",
                "IBKR_HOST=192.168.1.50",
                "LIVE_RUNNER_IBKR_CLIENT_ID_POOL=70-80",
            ]
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHON_EXE": sys.executable,
    }
    env.pop("IBKR_HOST_ALLOWLIST", None)
    env.pop("IBKR_HOST", None)
    env.pop("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", None)
    env_file_arg = os.path.relpath(env_file, repo_root)

    result = subprocess.run(
        [
            "bash",
            str(repo_root / "start-live-daemon.sh"),
            "--env-file",
            env_file_arg,
            "--print-launch-env",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)

    assert payload["IBKR_HOST_ALLOWLIST"] == "192.168.1.50,192.168.1.51"
    assert payload["IBKR_HOST"] == "192.168.1.50"
    assert payload["LIVE_RUNNER_IBKR_CLIENT_ID_POOL"] == "70-80"
    allowed_hosts = set(payload["allowed_ibkr_hosts"])
    assert {"192.168.1.50", "192.168.1.51"}.issubset(allowed_hosts)
    assert payload["env_file"] == str(repo_root / env_file_arg)


def test_start_live_daemon_print_launch_env_preserves_process_override(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    env_file = tmp_path / ".env"
    env_file.write_text("IBKR_HOST_ALLOWLIST=192.168.1.52\n", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHON_EXE": sys.executable,
        "IBKR_HOST_ALLOWLIST": "192.168.1.53",
        "LIVE_RUNNER_IBKR_CLIENT_ID_POOL": "90-99",
    }
    env.pop("IBKR_HOST", None)

    result = subprocess.run(
        [
            "bash",
            str(repo_root / "start-live-daemon.sh"),
            "--env-file",
            str(env_file),
            "--print-launch-env",
        ],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)

    assert payload["IBKR_HOST_ALLOWLIST"] == "192.168.1.53"
    assert payload["LIVE_RUNNER_IBKR_CLIENT_ID_POOL"] == "90-99"
    allowed_hosts = set(payload["allowed_ibkr_hosts"])
    assert "192.168.1.53" in allowed_hosts
    assert "192.168.1.52" not in allowed_hosts


def test_bootstrap_daemon_match_accepts_optional_cli_arguments_before_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = (repo_root / "bootstrap-host-daemon.sh").read_text(encoding="utf-8")
    match_line = next(
        line for line in script.splitlines() if line.startswith('DAEMON_MATCH="')
    )
    daemon_match = match_line.removeprefix('DAEMON_MATCH="').removesuffix('"')
    daemon_match = daemon_match.replace("$ROOT_DIR", re.escape(str(repo_root)))

    standard_argv = (
        "python -m app.engine.live.host_daemon "
        f"--repo-root {repo_root} --port 8765"
    )
    host_first_argv = (
        "python -m app.engine.live.host_daemon --host 0.0.0.0 "
        f"--repo-root {repo_root} --env-file {repo_root / '.env'}"
    )

    assert re.search(daemon_match, standard_argv)
    assert re.search(daemon_match, host_first_argv)


def test_bootstrap_active_run_probe_authenticates_health_request() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = (repo_root / "bootstrap-host-daemon.sh").read_text(encoding="utf-8")
    active_run_ids = script.split("active_run_ids() {", maxsplit=1)[1].split(
        "\n}", maxsplit=1
    )[0]

    health_request = next(
        line for line in active_run_ids.splitlines() if '"$HEALTH_URL"' in line
    )

    assert 'X-Live-Runner-Token: $token' in health_request
