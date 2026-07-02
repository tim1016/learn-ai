"""Host-runner start request safety validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.live_runs import HostRunnerStartRequest


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "localhost",
        "host.containers.internal",
        "host.docker.internal",
    ],
)
def test_start_request_accepts_local_gateway_hosts(host: str) -> None:
    assert HostRunnerStartRequest(ibkr_host=host).ibkr_host == host


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.50",
        "gateway.example.com",
        "http://127.0.0.1",
        "127.0.0.1 ",
    ],
)
def test_start_request_rejects_unconfigured_ibkr_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="ibkr_host"):
        HostRunnerStartRequest(ibkr_host=host)


def test_start_request_accepts_allowlisted_ibkr_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IBKR_HOST_ALLOWLIST", "192.168.1.50,gateway.example.com")

    assert HostRunnerStartRequest(ibkr_host="192.168.1.50").ibkr_host == "192.168.1.50"
    assert (
        HostRunnerStartRequest(ibkr_host="gateway.example.com").ibkr_host
        == "gateway.example.com"
    )


def test_start_request_accepts_configured_ibkr_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IBKR_HOST", "auto")

    assert HostRunnerStartRequest(ibkr_host="auto").ibkr_host == "auto"
