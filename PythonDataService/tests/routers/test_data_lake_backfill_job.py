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

import asyncio
import json
import re
import threading
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.data_lake as data_lake_router
from app.data_lake.backfill import BackfillDayProgress, BackfillResult, BackfillWaitProgress
from app.data_lake.types import ArtifactFailure, DataAvailabilityResult
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.routers.data_lake import router as data_lake_router_instance

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

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

    async def fake_run_backfill(
        spec: Any,
        *,
        on_day_progress: Any = None,
        on_wait: Any = None,
        cancel_check: Any = None,
        ensure_fn: Any = None,
        status_fn: Any = None,
    ) -> BackfillResult:
        assert cancel_check is not None
        if on_wait is not None:
            # Exercise the lease-wait relay too — a slow coalesce must keep
            # the SSE stream informative, not silent.
            on_wait(
                BackfillWaitProgress(
                    trading_date=date(2024, 5, 20),
                    symbol="SPY",
                    data_type="trade",
                    attempt=1,
                )
            )
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

    # Structured per-day events carry the typed failure reason intact,
    # with the trading date as canonical ET-session-open ms UTC
    # (temporal-rigor.md) — never an ISO date string on the wire.
    day_events = [payload for (etype, payload) in emitter.events if etype == "data_lake.backfill_day"]
    assert len(day_events) == 2
    expected_ms = session_open_ms_utc(date(2024, 5, 21))
    failing_day_event = next(e for e in day_events if e["trading_date_ms"] == expected_ms)
    assert isinstance(failing_day_event["trading_date_ms"], int)
    assert failing_day_event["failures"] == [
        {
            "artifact_kind": "time_series_bars",
            "symbol": "SPY",
            "trading_date_ms": expected_ms,
            "data_type": "trade",
            "reason": "provider_auth_error",
            "detail": "401 from Polygon",
            "provider_status_code": None,
            "attempt_count": 1,
        }
    ]
    assert not _ISO_DATE_RE.search(json.dumps(day_events))

    # The typed reason also appears in a warning-level log line, never
    # collapsed to an opaque string.
    assert any(level == "warning" and "provider_auth_error" in message for level, message in emitter.logs)

    # Final job result carries the same typed failure, with its own date
    # fields converted the same way — no ISO date string anywhere on the
    # wire, only the ms fields.
    final_result = captured["result"]
    assert final_result["overall_status"] == "partial"
    assert final_result["failures"][0]["reason"] == "provider_auth_error"
    assert final_result["failures"][0]["trading_date_ms"] == expected_ms
    assert final_result["start_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 20))
    assert final_result["end_trading_date_ms"] == session_open_ms_utc(date(2024, 5, 24))
    assert "start_trading_date" not in final_result
    assert "end_trading_date" not in final_result
    assert not _ISO_DATE_RE.search(json.dumps(final_result))

    # A lease-wait tick relays as an info-level log line, keeping the SSE
    # stream informative during a slow coalesce instead of going silent.
    assert any(level == "info" and "waiting on another worker" in message for level, message in emitter.logs)


async def test_ensure_data_calls_bridge_onto_the_requests_own_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-1 regression (review round 3).

    ensure_data's asyncpg pool (app.data_lake.catalog_client) is keyed by
    the calling event loop, so a loop with no pool of its own raises
    rather than reusing a foreign one. work()'s asyncio.run(_do()) spins
    up a fresh, throwaway loop per job; without _bridge_ensure_fn, any
    ensure_data() call inside that thread would run on THAT throwaway
    loop and pay for (and never close) a brand-new asyncpg pool every
    single backfill job, instead of reusing the one already pooled on
    this request's own loop (the common case, since /ensure-data is a
    plain async handler that always runs there).

    This uses a genuinely separate real background thread (mirroring
    run_in_thread's own fire-and-forget threading.Thread — the whole
    point is to exercise a truly different loop, so this test's own fake
    only skips the Redis-backed ProgressEmitter/CancellationCheck, not
    the real threading) with app.routers.data_lake.ensure_data replaced
    by a fake that records which loop it actually executed on. It must be
    the loop this test itself is running on (captured before the
    request, since httpx's ASGITransport dispatches the FastAPI handler
    on the caller's own loop) — never the worker thread's own throwaway
    one.

    Waiting for the background thread deliberately does NOT use a
    blocking thread.join() on this test's own coroutine: that would
    freeze this test's event loop, and run_coroutine_threadsafe's bridged
    ensure_data() call needs this exact loop to keep spinning (processing
    its scheduled-callback queue) to ever actually run — a synchronous
    join() here would self-deadlock the test, mirroring why
    run_in_thread's real implementation never joins its own thread either.
    """
    seen_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_ensure_data(spec: Any) -> DataAvailabilityResult:
        seen_loops.append(asyncio.get_running_loop())
        return DataAvailabilityResult(
            request_id=spec.request_id,
            overall_status="complete",
            lean_data_root_path="/tmp/lake",
            data_availability_hash="hash",
            artifacts=[],
            failures=[],
            skipped_non_sessions=[],
            fetched_artifact_count=0,
            reused_artifact_count=0,
            completed_at_ms=1,
            duration_ms=1,
        )

    monkeypatch.setattr(data_lake_router, "ensure_data", fake_ensure_data)

    this_loop = asyncio.get_running_loop()
    job_done = threading.Event()

    def run_on_a_real_thread(job_id: str, work: Any, **_kwargs: Any) -> None:
        def target() -> None:
            work(_Emitter(), _Cancel())
            job_done.set()

        threading.Thread(target=target, name=f"test-{job_id}").start()

    monkeypatch.setattr(data_lake_router, "run_in_thread", run_on_a_real_thread)

    flag_on_app = _make_app(include_data_lake=True)
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        response = await client.post(
            "/api/data-lake/backfill",
            json=_valid_body(job_id="job-loop", start_trading_date="2024-05-20", end_trading_date="2024-05-20"),
        )

    assert response.status_code == 202

    for _ in range(500):  # up to ~5s, yielding to this loop each time
        if job_done.is_set():
            break
        await asyncio.sleep(0.01)
    assert job_done.is_set(), "background job did not finish in time"

    assert len(seen_loops) == 1
    assert seen_loops[0] is this_loop
