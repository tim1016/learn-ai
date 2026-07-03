"""Tests for host-runner IBKR host allow-list policy."""

from __future__ import annotations

import pytest

from app.engine.live.host_runner_policy import (
    allowed_ibkr_hosts,
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


def test_policy_rejects_unallowlisted_host() -> None:
    with pytest.raises(ValueError, match="host-daemon allow-list"):
        validate_ibkr_host_allowed("192.168.1.50", environ={})
