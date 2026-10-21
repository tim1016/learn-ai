"""A benchmark ends after the real save, never at its pre-transaction timestamp."""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.data_lake import catalog_client
from app.options_backtest import saved_runs
from app.options_backtest.benchmark import benchmark_cases, summarize_measurements
from app.research.persistence.db import writer_loop_call
from app.routers.options_backtest_benchmark import router
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest.test_benchmark import observations
from tests.research.conftest import conn as conn

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("evidence", ["complete", "missing", "duplicate", "invalid_order"])
async def test_saved_result_http_requires_unambiguous_post_commit_time(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, evidence: str,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME", "1")
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "benchmark-timing-test")
    result = a_busy_run()
    run_id = await saved_runs.create_run(conn, saved_runs.NewRun(
        job_id="benchmark-timing-" + uuid4().hex, created_at_ms=1_000_000,
        setup=result.setup, setup_name=None, first_session_open_ms=result.sessions[0].session_open_ms,
        last_session_open_ms=result.sessions[-1].session_open_ms, sessions_requested=len(result.sessions),
    ))
    try:
        await saved_runs.complete_run(conn, run_id, result, finished_at_ms=1_059_000)
        save = dict(phase="save", started_at_ms=1_059_000, finished_at_ms=1_062_000,
                    duration_ms=3000, scope="run", chain_key=None)
        spans = [] if evidence == "missing" else [save, save] if evidence == "duplicate" else [save]
        if evidence == "invalid_order":
            spans = [{**save, "finished_at_ms": 1_058_000}]
        await saved_runs.record_timings(conn, run_id, spans, {"measurement_test": True})
        app = FastAPI()
        app.include_router(router, prefix="/api/jobs-internal")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={CONTROL_SECRET_HEADER: "benchmark-timing-test"}) as client:
            response = await client.get(f"/api/jobs-internal/options-backtest/benchmark-result/{run_id}")
        if evidence != "complete":
            assert response.status_code == 409
            return
        assert response.status_code == 200
        measured = response.json()
        assert measured["saved_at_ms"] - measured["accepted_at_ms"] == 62_000
        typical = benchmark_cases()[0]
        samples = [sample.model_copy(update={"accepted_at_ms": measured["accepted_at_ms"],
                   "saved_at_ms": measured["saved_at_ms"]})
                   if sample.case_digest == typical.digest and sample.cache_mode == "cold" else sample
                   for sample in observations()]
        cold = next(group for group in summarize_measurements(samples)
                    if group["case"] == typical.name and group["cache_mode"] == "cold")
        assert cold["median_end_to_end_ms"] == 62_000
        assert cold["target_met"] is False
    finally:
        await conn.execute("DELETE FROM research_options_backtest_runs WHERE id=$1", run_id)
        await writer_loop_call(catalog_client.close_pool())
