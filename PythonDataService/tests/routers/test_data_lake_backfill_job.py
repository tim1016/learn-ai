"""POST /api/data-lake/backfill — the thin job-entry wrapper (#1836).

Mirrors tests/routers/test_lean_engine_run_job_phases.py's
run_in_thread-monkeypatch pattern: the underlying work runs synchronously
on a real thread with a fake ProgressEmitter/CancellationCheck double, and
app.data_lake.backfill.run_backfill is replaced with a small fake so this
file proves the HTTP boundary and the SSE-event wiring without touching
Postgres, Polygon, or a real Redis-backed thread. run_backfill's own
per-day orchestration is covered by tests/unit/data_lake/test_backfill.py;
the flag-off / 404 and 422 cases mirror
tests/integration/data_lake/test_ensure_data_route.py.
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.data_lake as data_lake_router
from app.data_lake.backfill import BackfillDayProgress, BackfillResult
from app.data_lake.types import ArtifactFailure
from app.routers.data_lake import router as data_lake_router_instance

pytestmark = pytest.mark.asyncio


def _make_app(*, include_data_lake: bool) -> FastAPI:
    app = FastAPI()
    if include_data_lake:
        app.include_router(data_lake_router_instance)
    return app


def _valid_body(job_id: str = "job-1", **spec_overrides: Any) -> dict:
    spec = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    spec.update(spec_overrides)
    return {"job_id": job_id, "spec": spec}


class _Cancel:
    def __init__(self) -> None:
        self.calls = 0

    def raise_if_cancelled(self) -> None:
        self.calls += 1


class _Emitter:
    def __init__(self) -> None:
        self.phases: list[str] = []
        self.logs: list[tuple[str, str]] = []
        self.progress_calls: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def phase(self, name: str) -> None:
        self.phases.append(name)

    def log(self, message: str, *, level: str = "info") -> None:
        self.logs.append((level, message))

    def progress(self, current: int, total: int, *, unit: str = "bars", message: str | None = None) -> None:
        self.progress_calls.append({"current": current, "total": total, "unit": unit, "message": message})

    def emit_event(self, event_type: str, payload: dict[str, Any]) -> str:
        self.events.append((event_type, payload))
        return "0-1"


def _run_sync_factory(captured: dict[str, Any]):
    def run_sync(job_id: str, work: Any, **_kwargs: Any) -> None:
        emitter = _Emitter()
        cancel = _Cancel()

        def target() -> None:
            captured["result"] = work(emitter, cancel)

        thread = threading.Thread(target=target, name=f"test-{job_id}")
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        captured["emitter"] = emitter
        captured["cancel"] = cancel

    return run_sync


async def test_route_404_when_flag_off() -> None:
    flag_off_app = _make_app(include_data_lake=False)
    async with AsyncClient(transport=ASGITransport(app=flag_off_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/backfill", json={})
    assert r.status_code == 404


async def test_missing_job_id_is_422() -> None:
    flag_on_app = _make_app(include_data_lake=True)
    body = _valid_body()
    del body["job_id"]
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/backfill", json=body)
    assert r.status_code == 422


async def test_invalid_spec_symbol_is_422() -> None:
    flag_on_app = _make_app(include_data_lake=True)
    body = _valid_body(symbols=["spy"])  # lowercase — rejected by DataRunSpec's validator
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/backfill", json=body)
    assert r.status_code == 422


async def test_start_backfill_job_returns_202_and_streams_per_day_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(data_lake_router, "run_in_thread", _run_sync_factory(captured))

    auth_failure = ArtifactFailure(
        artifact_kind="time_series_bars",
        symbol="SPY",
        trading_date=date(2024, 5, 21),
        data_type="trade",
        reason="provider_auth_error",
        detail="401 from Polygon",
        attempt_count=1,
    )

    async def fake_run_backfill(spec: Any, *, on_day_progress: Any = None, cancel_check: Any = None, ensure_fn: Any = None) -> BackfillResult:
        assert cancel_check is not None
        if on_day_progress is not None:
            on_day_progress(
                BackfillDayProgress(
                    day_index=1,
                    total_days=2,
                    trading_date=date(2024, 5, 20),
                    fetched_count=1,
                    reused_count=0,
                    failures=(),
                )
            )
            on_day_progress(
                BackfillDayProgress(
                    day_index=2,
                    total_days=2,
                    trading_date=date(2024, 5, 21),
                    fetched_count=0,
                    reused_count=0,
                    failures=(auth_failure,),
                )
            )
        return BackfillResult(
            request_id=spec.request_id,
            market=spec.market,
            symbols=list(spec.symbols),
            start_trading_date=spec.start_trading_date,
            end_trading_date=spec.end_trading_date,
            total_sessions=2,
            days_completed=1,
            days_with_failures=1,
            fetched_artifact_count=1,
            reused_artifact_count=0,
            failures=[auth_failure],
            overall_status="partial",
            completed_at_ms=123,
            duration_ms=1,
        )

    monkeypatch.setattr(data_lake_router, "run_backfill", fake_run_backfill)

    flag_on_app = _make_app(include_data_lake=True)
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        response = await client.post("/api/data-lake/backfill", json=_valid_body(job_id="job-1"))

    assert response.status_code == 202
    assert response.json() == {"job_id": "job-1", "status": "queued"}

    emitter: _Emitter = captured["emitter"]
    assert "backfilling" in emitter.phases

    # Two job.progress ticks, one per day, in order, unit="days".
    assert [p["current"] for p in emitter.progress_calls] == [1, 2]
    assert all(p["total"] == 2 and p["unit"] == "days" for p in emitter.progress_calls)

    # Structured per-day events carry the typed failure reason intact.
    day_events = [payload for (etype, payload) in emitter.events if etype == "data_lake.backfill_day"]
    assert len(day_events) == 2
    failing_day_event = next(e for e in day_events if e["trading_date"] == "2024-05-21")
    assert failing_day_event["failures"] == [
        {
            "artifact_kind": "time_series_bars",
            "symbol": "SPY",
            "trading_date": "2024-05-21",
            "data_type": "trade",
            "reason": "provider_auth_error",
            "detail": "401 from Polygon",
            "provider_status_code": None,
            "attempt_count": 1,
        }
    ]

    # The typed reason also appears in a warning-level log line, never
    # collapsed to an opaque string.
    assert any(level == "warning" and "provider_auth_error" in message for level, message in emitter.logs)

    # Final job result carries the same typed failure.
    final_result = captured["result"]
    assert final_result["overall_status"] == "partial"
    assert final_result["failures"][0]["reason"] == "provider_auth_error"
