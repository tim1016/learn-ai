"""PRD #619-C followup (Codex review P2) — ``fetch_health``
classifies auth-sensitive transport outcomes via the daemon's
``/health`` endpoint, which is now token-gated.

Before the followup: ``/health`` was unauthenticated, so a missing or
rotated daemon token would never produce ``AUTH_FAILED`` from the
connectivity monitor — the monitor reported ``CONNECTED`` while every
protected daemon call failed closed. Now the probe goes through the
same auth ladder as every other daemon request, so AUTH_FAILED is
reachable.

These tests pin the typed-classifier behavior at the
``fetch_health`` boundary; the data-plane ``/daemon-health`` route
test (``tests/routers/test_live_instances.py``) covers HTTP-status
mapping.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.engine.live.host_daemon_client import fetch_health, fetch_run_process

BASE = "http://daemon-host:8765"


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_401_as_auth_failed() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(401, json={"detail": "missing token"})
    )

    result, health = await fetch_health(BASE)

    assert result.kind == "AUTH_FAILED"
    assert result.response_status == 401
    assert result.detail == "missing token"
    assert health is None


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_403_as_auth_failed() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(403, json={"detail": "rotated token"})
    )

    result, health = await fetch_health(BASE)

    assert result.kind == "AUTH_FAILED"
    assert result.response_status == 403
    assert health is None


@pytest.mark.asyncio
@respx.mock
async def test_probe_happy_path_extracts_daemon_boot_id() -> None:
    """A valid token + 2xx ``HostRunnerHealth`` body yields CONNECTED
    with the daemon's declared ``boot_id`` forwarded, and the parsed
    envelope returned for callers that need it (e.g. ``/daemon-health``)."""
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

    result, health = await fetch_health(BASE)

    assert result.kind == "CONNECTED"
    assert result.observed_daemon_boot_id == "boot-deadbeef"
    assert health is not None
    assert health.daemon_boot_id == "boot-deadbeef"
    assert health.ok is True


@pytest.mark.asyncio
@respx.mock
async def test_probe_treats_a_200_with_an_invalid_health_contract_as_not_connected() -> None:
    """A partial 200 response must not surface as a usable live engine."""

    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "repo_root": "/r",
                "live_runs_root": "/r/runs",
                "fetched_at_ms": 1_700_000_000_000,
                # The required process contract is deliberately absent.
            },
        )
    )

    result, health = await fetch_health(BASE)

    assert result.kind == "INCOMPATIBLE_CONTRACT"
    assert result.response_status == 200
    assert health is None


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_5xx_as_protocol_error() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(503, json={"detail": "lifespan failed"})
    )

    result, health = await fetch_health(BASE)

    assert result.kind == "PROTOCOL_ERROR"
    assert result.response_status == 503
    assert health is None


@pytest.mark.asyncio
@respx.mock
async def test_probe_classifies_connect_error_as_unreachable() -> None:
    respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))

    result, health = await fetch_health(BASE)

    assert result.kind == "UNREACHABLE"
    assert result.outcome_ambiguous is False
    assert health is None


@pytest.mark.asyncio
@respx.mock
async def test_run_process_probe_requires_the_host_process_contract() -> None:
    respx.get(f"{BASE}/runs/run-a/process").mock(
        return_value=httpx.Response(200, json={"state": "exited", "run_id": "run-a"})
    )

    result, process = await fetch_run_process(BASE, "run-a")

    assert result.kind == "CONNECTED"
    assert process is not None
    assert process.state == "exited"


@pytest.mark.asyncio
@respx.mock
async def test_run_process_probe_rejects_an_invalid_host_process_contract() -> None:
    respx.get(f"{BASE}/runs/run-a/process").mock(
        return_value=httpx.Response(200, json={"state": "unrecognised"})
    )

    result, process = await fetch_run_process(BASE, "run-a")

    assert result.kind == "INCOMPATIBLE_CONTRACT"
    assert process is None
