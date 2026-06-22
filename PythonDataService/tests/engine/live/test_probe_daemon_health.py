"""PRD #619-C followup (Codex review P2) — ``probe_daemon_health``
classifies auth-sensitive transport outcomes via the daemon's
``/health`` endpoint, which is now token-gated.

Before the followup: ``/health`` was unauthenticated, so a missing or
rotated daemon token would never produce ``AUTH_FAILED`` from the
connectivity monitor — the monitor reported ``CONNECTED`` while every
protected daemon call failed closed. Now the probe goes through the
same auth ladder as every other daemon request, so AUTH_FAILED is
reachable.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.engine.live.host_daemon_client import probe_daemon_health

BASE = "http://daemon-host:8765"


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_401_as_auth_failed() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(401, json={"detail": "missing token"})
    )

    result = await probe_daemon_health(BASE)

    assert result.kind == "AUTH_FAILED"
    assert result.response_status == 401
    assert result.detail == "missing token"


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_403_as_auth_failed() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(403, json={"detail": "rotated token"})
    )

    result = await probe_daemon_health(BASE)

    assert result.kind == "AUTH_FAILED"
    assert result.response_status == 403


@pytest.mark.asyncio
@respx.mock
async def test_probe_happy_path_extracts_daemon_boot_id() -> None:
    """A valid token + 2xx ``HostRunnerHealth`` body yields CONNECTED
    with the daemon's declared ``boot_id`` forwarded."""
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "repo_root": "/r",
                "live_runs_root": "/r/runs",
                "fetched_at_ms": 1_700_000_000_000,
                "process": {"state": "idle"},
                "daemon_boot_id": "boot-deadbeef",
            },
        )
    )

    result = await probe_daemon_health(BASE)

    assert result.kind == "CONNECTED"
    assert result.observed_daemon_boot_id == "boot-deadbeef"


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_5xx_as_protocol_error() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(503, json={"detail": "lifespan failed"})
    )

    result = await probe_daemon_health(BASE)

    assert result.kind == "PROTOCOL_ERROR"
    assert result.response_status == 503


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_connect_error_as_unreachable() -> None:
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))

    result = await probe_daemon_health(BASE)

    assert result.kind == "UNREACHABLE"
    assert result.outcome_ambiguous is False
