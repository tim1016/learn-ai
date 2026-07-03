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
        "192.168.1.50",
        "gateway.example.com",
    ],
)
def test_start_request_accepts_bare_ibkr_hosts(host: str) -> None:
    assert HostRunnerStartRequest(ibkr_host=host).ibkr_host == host


@pytest.mark.parametrize(
    "host",
    [
        "http://127.0.0.1",
        "127.0.0.1/path",
        "user@127.0.0.1",
        "127.0.0.1 ",
    ],
)
def test_start_request_rejects_non_bare_ibkr_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="ibkr_host"):
        HostRunnerStartRequest(ibkr_host=host)
