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

from app.lean_sidecar.config import LEAN_IMAGE_REPO, PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL
from app.main import app

pytestmark = pytest.mark.asyncio


def _healthy_healthz() -> dict[str, object]:
    assert PINNED_LEAN_IMAGE_DIGEST is not None
    return {
        "status": "ok",
        "version": "test",
        "image": {
            "reference": f"{LEAN_IMAGE_REPO}@{PINNED_LEAN_IMAGE_DIGEST}",
            "available": True,
            "detail": "Pinned LEAN image is present in Podman's local image store.",
        },
    }


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
    monkeypatch.setenv("LEAN_LAUNCHER_URL", "http://host.containers.internal:abc")

    response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "fail"
    parse_check = next(c for c in body["checks"] if c["name"] == "launcher_url_parseable")
    assert parse_check["status"] == "fail"
    assert "invalid port" in parse_check["detail"]
    healthz_check = next(c for c in body["checks"] if c["name"] == "launcher_healthz")
    assert healthz_check["status"] == "skip"


async def test_diagnose_private_lan_launcher_url_warns(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Machine-specific LAN IPs may work today but are not a portable
    Podman topology. Keep them visible as a warning even when the
    launcher is reachable."""
    launcher_url = "http://192.168.86.106:8090"
    monkeypatch.setenv("LEAN_LAUNCHER_URL", launcher_url)
    async with respx.mock(base_url=launcher_url) as mock:
        mock.get("/healthz").mock(
            return_value=httpx.Response(200, json=_healthy_healthz())
        )

        response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "warn"
    url_check = next(c for c in body["checks"] if c["name"] == "launcher_url")
    assert url_check["status"] == "warn"
    assert "private LAN IP 192.168.86.106" in url_check["detail"]
    assert "host.containers.internal" in url_check["fix"]


async def test_diagnose_happy_path_returns_pass(client: AsyncClient) -> None:
    async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
        mock.get("/healthz").mock(
            return_value=httpx.Response(200, json=_healthy_healthz())
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
        "launcher_image",
    ]
    statuses = {c["name"]: c["status"] for c in body["checks"]}
    assert statuses == {
        "launcher_url": "pass",
        "launcher_url_parseable": "pass",
        "launcher_token": "pass",
        "launcher_healthz": "pass",
        "launcher_image": "pass",
    }
    assert isinstance(body["fetched_at_ms"], int)
    assert body["fetched_at_ms"] > 0


async def test_diagnose_missing_pinned_image_blocks_lean_run(client: AsyncClient) -> None:
    """Regression: an HTTP-reachable launcher must not pass readiness when
    its local Podman store lacks the immutable LEAN image it will launch."""
    assert PINNED_LEAN_IMAGE_DIGEST is not None
    healthz = _healthy_healthz()
    healthz["status"] = "degraded"
    image = healthz["image"]
    assert isinstance(image, dict)
    image["available"] = False
    image["failure_reason"] = "missing"
    image["detail"] = "Pinned LEAN image is not present in Podman's local image store."
    async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
        mock.get("/healthz").mock(return_value=httpx.Response(200, json=healthz))

        response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    body = response.json()
    assert body["overall_status"] == "fail"
    assert next(c for c in body["checks"] if c["name"] == "launcher_healthz")["status"] == "pass"
    image_check = next(c for c in body["checks"] if c["name"] == "launcher_image")
    assert image_check["status"] == "fail"
    assert "Build the configured local LEAN derivative" in image_check["fix"]


async def test_diagnose_podman_readiness_failure_does_not_recommend_an_image_build(
    client: AsyncClient,
) -> None:
    healthz = _healthy_healthz()
    healthz["status"] = "degraded"
    image = healthz["image"]
    assert isinstance(image, dict)
    image["available"] = False
    image["failure_reason"] = "check_failed"
    image["detail"] = "Podman could not determine pinned-image readiness (exit code 125)."
    async with respx.mock(base_url=DEFAULT_LAUNCHER_URL) as mock:
        mock.get("/healthz").mock(return_value=httpx.Response(200, json=healthz))

        response = await client.get("/api/lean-sidecar/diagnose")

    assert response.status_code == 200
    image_check = next(c for c in response.json()["checks"] if c["name"] == "launcher_image")
    assert image_check["status"] == "fail"
    assert "Podman could not determine" in image_check["detail"]
    assert "Build the configured local LEAN derivative" not in image_check["fix"]
