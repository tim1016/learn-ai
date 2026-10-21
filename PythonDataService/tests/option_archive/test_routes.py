from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
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


class _SharedOwnership:
    """The archive's job ownership as the nightly pass and a manual recovery contend for it."""

    def __init__(self, held: set[str]) -> None:
        self.held = held
        self.refused, self.released = threading.Event(), threading.Event()

    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        if name in self.held:
            self.refused.set()
            yield False
            return
        self.held.add(name)
        try:
            yield True
        finally:
            self.held.discard(name)
            self.released.set()


async def test_manual_recovery_leaves_the_provider_alone_while_the_nightly_capture_owns_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = _SharedOwnership(held={"capture"})
    provider_calls: list[set[str]] = []

    async def provider(underlying: str, day: date, **options: object) -> None:
        provider_calls.append(set(ownership.held))
        raise ValueError("stop at the provider boundary")
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=ArchiveState(tmp_path / "state.json"),
                                   clock=lambda: 1792558800000, ownership=ownership, capture=provider)
    app = FastAPI()
    app.state.option_archive_service = service
    app.include_router(router)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "local-private-test-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    request = {"underlying": "SPY", "session_ms": session_open_ms_utc(date(2026, 10, 9))}
    headers = {"X-Data-Plane-Control-Secret": "local-private-test-secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        beside_nightly = await client.post("/api/option-archive/capture-session", json=request, headers=headers)
        assert await asyncio.to_thread(ownership.refused.wait, 5), "manual recovery never asked for archive ownership"
        assert beside_nightly.status_code == 202 and provider_calls == [] and not service.state.path.exists()
        ownership.held.clear()
        alone = await client.post("/api/option-archive/capture-session", json=request, headers=headers)
        assert await asyncio.to_thread(ownership.released.wait, 5)
    assert alone.status_code == 202 and provider_calls == [{"capture"}]
