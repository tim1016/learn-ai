"""Security guard tests for mutating data-plane control routes."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app

_PROBE_PATH = "/api/broker/__security_probe__"


@pytest.mark.asyncio
async def test_control_mutation_rejects_missing_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_PROBE_PATH)

    assert response.status_code == 403
    assert settings.DATA_PLANE_CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_rejects_wrong_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PROBE_PATH,
            headers={settings.DATA_PLANE_CONTROL_SECRET_HEADER: "wrong"},
        )

    assert response.status_code == 403
    assert settings.DATA_PLANE_CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_accepts_valid_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _PROBE_PATH,
            headers={settings.DATA_PLANE_CONTROL_SECRET_HEADER: "test-control-secret"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_control_get_does_not_require_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_PROBE_PATH)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_disallowed_host_header_is_rejected() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"host": "evil.example"})

    assert response.status_code == 400
