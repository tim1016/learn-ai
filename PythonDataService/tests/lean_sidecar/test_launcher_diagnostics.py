"""Tests for the /api/lean-sidecar/diagnose endpoint.

The diagnostics endpoint never raises; every check captures its own
failure and surfaces it as one row of a structured report. The happy
path here exercises the URL → token → ``/healthz`` chain with respx
mocking the launcher's ``/healthz`` response.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL
from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_launcher_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin env to deterministic values so the test isn't affected by
    whatever the surrounding shell or compose layer leaked in.

    The token env is set explicitly so the token-resolution check has
    a known outcome without requiring a real launcher bind-mount on
    disk. The URL env is cleared so the probe targets
    ``DEFAULT_LAUNCHER_URL`` and respx intercepts it.
    """
    monkeypatch.delenv("LEAN_LAUNCHER_URL", raising=False)
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "diagnostic-test-token")


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_diagnose_malformed_port_returns_structured_fail(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a non-numeric port in ``LEAN_LAUNCHER_URL`` must
    surface as a structured ``launcher_url_parseable`` fail row, not
    a 500. ``ParseResult.port`` raises ``ValueError`` lazily on bad
    inputs; the check has to catch that and render the report."""
    monkeypatch.setenv("LEAN_LAUNCHER_URL", "http://host.docker.internal:abc")

    response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "fail"
    parse_check = next(c for c in body["checks"] if c["name"] == "launcher_url_parseable")
    assert parse_check["status"] == "fail"
    assert "invalid port" in parse_check["detail"]
    healthz_check = next(c for c in body["checks"] if c["name"] == "launcher_healthz")
    assert healthz_check["status"] == "skip"


async def test_diagnose_happy_path_returns_pass(client: AsyncClient) -> None:
    async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
        mock.get("/healthz").mock(
            return_value=httpx.Response(200, json={"status": "ok", "version": "test"})
        )

        response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "pass"
    names = [c["name"] for c in body["checks"]]
    assert names == [
        "launcher_url",
        "launcher_url_parseable",
        "launcher_token",
        "launcher_healthz",
    ]
    statuses = {c["name"]: c["status"] for c in body["checks"]}
    assert statuses == {
        "launcher_url": "pass",
        "launcher_url_parseable": "pass",
        "launcher_token": "pass",
        "launcher_healthz": "pass",
    }
    assert isinstance(body["fetched_at_ms"], int)
    assert body["fetched_at_ms"] > 0
