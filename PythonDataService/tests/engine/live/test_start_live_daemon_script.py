"""Contract tests for the local host-daemon launcher script."""

from __future__ import annotations

import json
import os
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
    allowed_hosts = set(payload["allowed_ibkr_hosts"])
    assert "192.168.1.53" in allowed_hosts
    assert "192.168.1.52" not in allowed_hosts
