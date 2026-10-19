"""History capture uses the lake's existing underlying and listing download path."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.broker.fleet.errors import DataPlaneControlSecretRefused
from app.config import settings
from app.options_backtest import chain_download
from app.research.options_golden_search import service
from app.routers import options_golden_search as router
from app.schemas.options_golden_search import HistoryCaptureJobRequest, PlanRequest
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.session_anchors import et_midnight_ms


@pytest.mark.asyncio
async def test_history_job_fetches_underlying_and_listing_for_each_study_session(monkeypatch: pytest.MonkeyPatch) -> None:
    days = (date(2025, 1, 2), date(2025, 1, 3))
    asked: list[date] = []
    progress: list[tuple[int, int]] = []
    work = None

    class Api:
        async def history_capture_scope(self, request: PlanRequest) -> tuple[str, tuple[int, ...]]:
            return "SPY", tuple(et_midnight_ms(day) for day in days)

    async def capture(underlying: str, day: date, *, expiry: None, contracts: list) -> tuple:
        assert underlying == "SPY"
        assert expiry is None and contracts == []
        asked.append(day)
        return (
            chain_download.DownloadItem(chain_download.ItemKind.LISTING, underlying, chain_download.Outcome.FETCHED, None, None),
            chain_download.DownloadItem(chain_download.ItemKind.UNDERLYING, underlying, chain_download.Outcome.ALREADY_STORED, None, None),
        )

    def start(_id: str, task, **_kwargs) -> None:
        nonlocal work
        work = task

    monkeypatch.setattr(chain_download, "ensure_chain_data", capture)
    monkeypatch.setattr(router, "run_in_thread", start)
    app = FastAPI()
    app.include_router(router.jobs_router, prefix="/api/jobs-internal")
    app.dependency_overrides[router.get_service] = Api
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/jobs-internal/options-golden-search-history", headers={"X-Data-Plane-Control-Secret": "test-control-secret"}, json={
            "job_id": "history-job", "request": {"setup_name": "Saved setup", "expected_setup_hash": "a" * 64,
                "knobs": [], "history": {"start_ms": et_midnight_ms(days[0]), "end_ms": et_midnight_ms(days[-1])}},
        })
    assert response.status_code == 202
    assert response.json() == {"job_id": "history-job", "status": "queued"}
    assert work is not None
    emitter = SimpleNamespace(phase=lambda *_args, **_kwargs: None, progress=lambda current, total, **_kwargs: progress.append((current, total)))
    cancel = SimpleNamespace(raise_if_cancelled=lambda: None)
    result = await asyncio.to_thread(work, emitter, cancel)
    assert asked == list(days)
    assert progress == [(1, 2), (2, 2)]
    assert result == {"sessions_checked": 2, "artifacts_fetched": 2}


@pytest.mark.asyncio
async def test_history_job_requires_control_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    app = FastAPI()
    app.include_router(router.jobs_router, prefix="/api/jobs-internal")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(DataPlaneControlSecretRefused):
            await client.post("/api/jobs-internal/options-golden-search-history", json={
                "job_id": "history-job", "request": {"setup_name": "Saved setup", "expected_setup_hash": "a" * 64,
                    "knobs": [], "history": {"start_ms": et_midnight_ms(date(2025, 1, 2)),
                        "end_ms": et_midnight_ms(date(2025, 1, 3))}},
            })


@pytest.mark.asyncio
async def test_history_job_refuses_a_changed_saved_setup() -> None:
    class Api:
        async def history_capture_scope(self, _request: PlanRequest) -> tuple[str, tuple[int, ...]]:
            raise service.repo.Conflict("The saved setup changed.")

    app = FastAPI()
    app.include_router(router.jobs_router, prefix="/api/jobs-internal")
    app.dependency_overrides[router.get_service] = Api
    app.dependency_overrides[require_data_plane_control_secret] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/jobs-internal/options-golden-search-history", json={
            "job_id": "history-job", "request": {"setup_name": "Saved setup", "expected_setup_hash": "a" * 64,
                "knobs": [], "history": {"start_ms": et_midnight_ms(date(2025, 1, 2)),
                    "end_ms": et_midnight_ms(date(2025, 1, 3))}},
        })
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "OPTIONS_SEARCH_REFUSED"


@pytest.mark.asyncio
async def test_history_capture_rejects_a_changed_saved_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def saved(_name: str) -> object:
        return object()

    monkeypatch.setattr(service, "setup_named", saved)
    monkeypatch.setattr(service, "setup_sha256", lambda _setup: "b" * 64)
    request = PlanRequest.model_construct(setup_name="Saved setup", expected_setup_hash="a" * 64)
    with pytest.raises(service.repo.Conflict, match="saved setup changed"):
        await service.history_capture_scope(request)


@pytest.mark.asyncio
async def test_history_job_reports_a_listing_failure_instead_of_completing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scope(_request: PlanRequest) -> tuple[str, tuple[int, ...]]:
        return "SPY", (et_midnight_ms(date(2025, 1, 2)),)

    async def failed(_underlying: str, _day: date, *, expiry: None, contracts: list) -> tuple:
        return (chain_download.DownloadItem(chain_download.ItemKind.LISTING, "SPY", chain_download.Outcome.FAILED, "outside_data_plan", None),)

    scheduled = None

    def start(_id: str, task, **_kwargs) -> None:
        nonlocal scheduled
        scheduled = task

    monkeypatch.setattr(chain_download, "ensure_chain_data", failed)
    monkeypatch.setattr(router, "run_in_thread", start)
    body = HistoryCaptureJobRequest.model_construct(request=PlanRequest.model_construct(), job_id="history-job")
    await router.capture_history(body, api=SimpleNamespace(history_capture_scope=scope))
    assert scheduled is not None
    emitter = SimpleNamespace(phase=lambda *_args, **_kwargs: None, progress=lambda *_args, **_kwargs: pytest.fail("failed capture reported progress"))
    cancel = SimpleNamespace(raise_if_cancelled=lambda: None)
    with pytest.raises(RuntimeError, match="data plan does not reach that far back"):
        await asyncio.to_thread(scheduled, emitter, cancel)


@pytest.mark.asyncio
async def test_history_job_acknowledges_cancellation_during_last_download(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scope(_request: PlanRequest) -> tuple[str, tuple[int, ...]]:
        return "SPY", (et_midnight_ms(date(2025, 1, 2)),)

    cancelled = False
    scheduled = None

    async def capture(_underlying: str, _day: date, *, expiry: None, contracts: list) -> tuple:
        nonlocal cancelled
        cancelled = True
        return ()

    def start(_id: str, task, **_kwargs) -> None:
        nonlocal scheduled
        scheduled = task

    def check_cancelled() -> None:
        if cancelled:
            raise RuntimeError("cancelled")

    monkeypatch.setattr(chain_download, "ensure_chain_data", capture)
    monkeypatch.setattr(router, "run_in_thread", start)
    body = HistoryCaptureJobRequest.model_construct(request=PlanRequest.model_construct(), job_id="history-job")
    await router.capture_history(body, api=SimpleNamespace(history_capture_scope=scope))
    assert scheduled is not None
    emitter = SimpleNamespace(phase=lambda *_args, **_kwargs: None, progress=lambda *_args, **_kwargs: pytest.fail("cancelled capture reported progress"))
    with pytest.raises(RuntimeError, match="cancelled"):
        await asyncio.to_thread(scheduled, emitter, SimpleNamespace(raise_if_cancelled=check_cancelled))
