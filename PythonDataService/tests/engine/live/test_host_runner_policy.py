"""Tests for host-runner IBKR host allow-list policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.host_runner_policy import (
    allowed_ibkr_hosts,
    host_process_ibkr_host,
    load_policy_env_file,
    validate_ibkr_host_allowed,
)


def test_policy_defaults_allow_local_gateway_hosts() -> None:
    allowed = allowed_ibkr_hosts({})

    assert "127.0.0.1" in allowed
    assert "localhost" in allowed
    assert "host.containers.internal" in allowed
    assert "host.docker.internal" in allowed


def test_policy_accepts_documented_env_allowlist() -> None:
    env = {"IBKR_HOST_ALLOWLIST": "192.168.1.50,gateway.example.com"}

    assert validate_ibkr_host_allowed("192.168.1.50", environ=env) == "192.168.1.50"
    assert validate_ibkr_host_allowed("gateway.example.com", environ=env) == "gateway.example.com"


def test_policy_accepts_configured_ibkr_host() -> None:
    assert validate_ibkr_host_allowed("auto", environ={"IBKR_HOST": "auto"}) == "auto"


@pytest.mark.parametrize("alias", ["host.containers.internal", "HOST.DOCKER.INTERNAL"])
def test_host_process_ibkr_host_translates_container_gateway_alias_to_loopback(alias: str) -> None:
    assert host_process_ibkr_host(alias) == "127.0.0.1"


def test_host_process_ibkr_host_translates_auto_to_loopback() -> None:
    assert host_process_ibkr_host("auto") == "127.0.0.1"


def test_host_process_ibkr_host_preserves_remote_gateway_host() -> None:
    assert host_process_ibkr_host("gateway.example.com") == "gateway.example.com"


def test_policy_rejects_unallowlisted_host() -> None:
    with pytest.raises(ValueError, match="host-daemon allow-list"):
        validate_ibkr_host_allowed("192.168.1.50", environ={})


def test_policy_env_file_loads_only_documented_daemon_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "POLYGON_API_KEY=not-for-daemon",
                "IBKR_HOST_ALLOWLIST=192.168.1.50,gateway.example.com",
                "IBKR_HOST=192.168.1.50",
                "LIVE_RUNNER_IBKR_CLIENT_ID_POOL=70-80",
            ]
        ),
        encoding="utf-8",
    )
    env: dict[str, str] = {}

    loaded = load_policy_env_file(env_file, environ=env)

    assert loaded == ("IBKR_HOST_ALLOWLIST", "IBKR_HOST", "LIVE_RUNNER_IBKR_CLIENT_ID_POOL")
    assert env == {
        "IBKR_HOST_ALLOWLIST": "192.168.1.50,gateway.example.com",
        "IBKR_HOST": "192.168.1.50",
        "LIVE_RUNNER_IBKR_CLIENT_ID_POOL": "70-80",
    }
    assert validate_ibkr_host_allowed("192.168.1.50", environ=env) == "192.168.1.50"


def test_policy_env_file_does_not_override_process_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "IBKR_HOST_ALLOWLIST=file-host.example.com",
                "IBKR_HOST=file-host.example.com",
                "LIVE_RUNNER_IBKR_CLIENT_ID_POOL=70-80",
            ]
        ),
        encoding="utf-8",
    )
    env = {
        "IBKR_HOST_ALLOWLIST": "process-host.example.com",
        "LIVE_RUNNER_IBKR_CLIENT_ID_POOL": "90-99",
    }

    loaded = load_policy_env_file(env_file, environ=env)

    assert loaded == ("IBKR_HOST",)
    assert env["IBKR_HOST_ALLOWLIST"] == "process-host.example.com"
    assert env["IBKR_HOST"] == "file-host.example.com"
    assert env["LIVE_RUNNER_IBKR_CLIENT_ID_POOL"] == "90-99"
    assert validate_ibkr_host_allowed("process-host.example.com", environ=env) == "process-host.example.com"
