from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveState
from app.routers.option_archive import router
from app.utils.error_handlers import install_fleet_control_error_handler


async def test_unavailable_status_is_never_reported_green() -> None:
    app = FastAPI()
    app.include_router(router)
    install_fleet_control_error_handler(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get("/api/option-archive/status")
    assert response.status_code == 503


async def test_status_read_is_cheap_and_manual_trigger_requires_control_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=False), state=ArchiveState(tmp_path / "state.json"), clock=lambda: 1792558800000)
    app.state.option_archive_service = service
    app.include_router(router)
    install_fleet_control_error_handler(app)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "local-private-test-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        status = await client.get("/api/option-archive/status")
        refused = await client.post("/api/option-archive/trigger/capture")
        disabled = await client.post("/api/option-archive/trigger/capture", headers={"X-Data-Plane-Control-Secret": "local-private-test-secret"})
    assert status.status_code == 200 and status.json()["enabled"] is False
    assert len(status.json()["underlyings"]) == 3
    assert refused.status_code == 403 and disabled.status_code == 409
    assert not service.state.path.exists()


async def test_manual_recovery_names_a_canonical_session_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.state.option_archive_service = OptionArchiveService(config=ArchiveSettings(ENABLED=True),
        state=ArchiveState(tmp_path / "state.json"), clock=lambda: 1792558800000)
    app.include_router(router)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "local-private-test-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/option-archive/capture-session", json={"underlying": "SPY", "session_ms": 1729517400001},
                                     headers={"X-Data-Plane-Control-Secret": "local-private-test-secret"})
    assert response.status_code == 422 and "canonical session open" in response.text
