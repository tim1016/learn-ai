from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.option_archive.scheduler import JobOwnership
from app.option_archive.service import OptionArchiveService
from app.option_archive.settings import ArchiveSettings
from app.option_archive.state import ArchiveState
from app.routers import option_archive as archive_routes
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
        self.released = threading.Event()

    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        if name in self.held:
            yield False
            return
        self.held.add(name)
        try:
            yield True
        finally:
            self.held.discard(name)
            self.released.set()


class _UnreachableOwnership:
    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        raise ConnectionError("the catalog database is unreachable")
        yield True


class _SlowOwnership(_SharedOwnership):
    """Answers the lock question only once ``answer`` is set."""

    def __init__(self) -> None:
        super().__init__(held=set())
        self.answer = threading.Event()

    @asynccontextmanager
    async def hold(self, name: str) -> AsyncIterator[bool]:
        await asyncio.to_thread(self.answer.wait, 5)
        async with super().hold(name) as owned:
            yield owned


REQUEST = {"underlying": "SPY", "session_ms": session_open_ms_utc(date(2026, 10, 9))}
HEADERS = {"X-Data-Plane-Control-Secret": "local-private-test-secret"}


def _recovery_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ownership: JobOwnership,
                     provider: Callable[..., Awaitable[None]]) -> tuple[httpx.AsyncClient, OptionArchiveService]:
    service = OptionArchiveService(config=ArchiveSettings(ENABLED=True), state=ArchiveState(tmp_path / "state.json"),
                                   clock=lambda: 1792558800000, ownership=ownership, capture=provider)
    app = FastAPI()
    app.state.option_archive_service = service
    app.include_router(router)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "local-private-test-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test"), service


async def test_manual_recovery_is_refused_while_another_job_holds_the_capture_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = _SharedOwnership(held={"capture"})
    provider_calls: list[set[str]] = []

    async def provider(underlying: str, day: date, **options: object) -> None:
        provider_calls.append(set(ownership.held))
        raise ValueError("stop at the provider boundary")
    client, service = _recovery_client(tmp_path, monkeypatch, ownership, provider)
    async with client:
        beside_nightly = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
        assert beside_nightly.status_code == 409 and "capture lock" in beside_nightly.json()["detail"]
        assert provider_calls == [] and not service.state.path.exists()
        ownership.held.clear()
        alone = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
        assert await asyncio.to_thread(ownership.released.wait, 5)
    assert alone.status_code == 202 and provider_calls == [{"capture"}]


async def test_back_to_back_recoveries_exclude_each_other_until_the_first_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = _SharedOwnership(held=set())
    in_provider, finish = threading.Event(), threading.Event()
    provider_calls: list[str] = []

    async def provider(underlying: str, day: date, **options: object) -> None:
        provider_calls.append(underlying)
        in_provider.set()
        await asyncio.to_thread(finish.wait, 5)
        raise ValueError("stop at the provider boundary")
    client, _ = _recovery_client(tmp_path, monkeypatch, ownership, provider)
    async with client:
        first = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
        assert await asyncio.to_thread(in_provider.wait, 5)
        second = await client.post("/api/option-archive/capture-session", json={**REQUEST, "underlying": "QQQ"}, headers=HEADERS)
        assert provider_calls == ["SPY"]
        finish.set()
        assert await asyncio.to_thread(ownership.released.wait, 5)
        ownership.released.clear()
        third = await client.post("/api/option-archive/capture-session", json={**REQUEST, "underlying": "QQQ"}, headers=HEADERS)
        assert await asyncio.to_thread(ownership.released.wait, 5)
    assert (first.status_code, second.status_code, third.status_code) == (202, 409, 202)
    assert provider_calls == ["SPY", "QQQ"]


async def test_manual_recovery_that_cannot_ask_for_the_lock_is_reported_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []

    async def provider(underlying: str, day: date, **options: object) -> None:
        provider_calls.append(underlying)
    client, service = _recovery_client(tmp_path, monkeypatch, _UnreachableOwnership(), provider)
    async with client:
        response = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
    assert response.status_code == 503 and "capture lock" in response.json()["detail"]
    assert provider_calls == [] and not service.state.path.exists()


async def test_manual_recovery_that_waits_too_long_for_the_lock_never_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = _SlowOwnership()
    provider_calls: list[str] = []

    async def provider(underlying: str, day: date, **options: object) -> None:
        provider_calls.append(underlying)
    monkeypatch.setattr(archive_routes, "LOCK_DECISION_TIMEOUT_S", 0.05)
    client, service = _recovery_client(tmp_path, monkeypatch, ownership, provider)
    async with client:
        response = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
        ownership.answer.set()
        assert await asyncio.to_thread(ownership.released.wait, 5)
    assert response.status_code == 503 and "capture lock" in response.json()["detail"]
    assert provider_calls == [] and not service.state.path.exists()


async def test_a_lock_granted_as_the_wait_gives_up_still_answers_that_the_recovery_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the service has taken the decision the request cannot cancel it, so its answer stands (#3455)."""
    async def provider(underlying: str, day: date, **options: object) -> None:
        raise AssertionError("the stand-in recovery never reaches the provider")
    monkeypatch.setattr(archive_routes, "LOCK_DECISION_TIMEOUT_S", 0.5)
    client, service = _recovery_client(tmp_path, monkeypatch, _SharedOwnership(held=set()), provider)

    async def recover_session(underlying: str, session_ms: int, *, retry_exhausted: bool,
                              lock_decision: Future[bool]) -> None:
        assert lock_decision.set_running_or_notify_cancel()
        await asyncio.sleep(1.0)
        lock_decision.set_result(True)
    monkeypatch.setattr(service, "recover_session", recover_session)
    async with client:
        response = await client.post("/api/option-archive/capture-session", json=REQUEST, headers=HEADERS)
    assert response.status_code == 202 and response.json()["job"] == "capture"
