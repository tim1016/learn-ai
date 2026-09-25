"""Synthetic job-boundary reproductions for review #2430 at 10b5f31b.

No Redis, database, launcher, container, broker or market-data access. The
real worker/cancellation and persistence timeout code runs with explicit
in-memory boundary doubles. Assertions describe the observed defects.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.jobs import progress
from app.jobs.runner import run_in_thread
from app.research.backtest_runs import repository, service
from app.routers import jobs
from app.services import lean_sidecar_service
from app.utils.background_loop import run_on_background_loop
from tests.jobs.conftest import _FakeRedis
from tests.research.backtest_runs.payloads import engine_payload


def _capture(monkeypatch: pytest.MonkeyPatch, route, request) -> dict:
    captured: dict = {}

    def capture(job_id, work, **kwargs):
        captured.update(job_id=job_id, work=work, kwargs=kwargs)

    monkeypatch.setattr(jobs, "run_in_thread", capture)
    asyncio.run(route(request))
    return captured


def test_lean_preexisting_cancel_is_ignored_but_engine_control_honors_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    reached: list[str] = []

    async def synthetic_lean(request, **kwargs):
        reached.append(request.run_id)
        return {"exit_code": 0, "strategy_execution_id": 42}

    monkeypatch.setattr(lean_sidecar_service, "run_trusted_sample", synthetic_lean)
    lean = _capture(
        monkeypatch,
        jobs.start_lean_engine_run_job,
        jobs.LeanEngineRunJobRequest(
            job_id="review-lean",
            request={
                "run_id": "review_2430_lean",
                "start_ms_utc": 1_736_778_600_000,
                "end_ms_utc": 1_736_865_000_000,
                "template": "ema_crossover",
                "data_policy": {
                    "source": "polygon",
                    "symbol": "SPY",
                    "adjusted": True,
                    "session": "regular",
                    "input_bars": {"timespan": "minute", "multiplier": 1},
                    "strategy_bars": {"timespan": "minute", "multiplier": 15},
                    "timestamp_policy": "bar_close_ms_utc",
                    "timezone": "America/New_York",
                },
            },
        ),
    )
    fake.job("review-lean", "queued")
    fake.hset("job:review-lean:state", mapping={"cancel_requested": "1"})
    worker = run_in_thread(lean["job_id"], lean["work"], **lean["kwargs"])
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert reached == ["review_2430_lean"]
    assert fake.hashes["job:review-lean:state"]["status"] == "completed"
    assert fake.events("review-lean")[-1]["type"] == "job.completed"
    assert "cancel_check_every_n" not in lean["kwargs"]

    engine = _capture(
        monkeypatch,
        jobs.start_engine_backtest_job,
        jobs.EngineBacktestJobRequest(
            job_id="review-engine",
            backtest={"strategy_name": "sma_crossover", "symbol": "SPY", "from_date": "2025-01-01", "to_date": "2025-01-31"},
        ),
    )
    fake.job("review-engine", "queued")
    fake.hset("job:review-engine:state", mapping={"cancel_requested": "1"})
    worker = run_in_thread(engine["job_id"], engine["work"], **engine["kwargs"])
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert fake.hashes["job:review-engine:state"]["status"] == "cancelled"
    assert engine["kwargs"]["cancel_check_every_n"] == 1


def test_unknown_save_returns_no_id_then_insert_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    committed = threading.Event()
    saved: list[int] = []

    async def in_memory_insert(function, record):
        assert function is repository.insert_run
        await asyncio.sleep(0.05)
        saved.append(2430)
        committed.set()
        return repository.InsertOutcome(run_id=2430, created=True)

    monkeypatch.setattr(service, "with_connection", in_memory_insert)
    monkeypatch.setattr(service, "run_sync", lambda coroutine: run_on_background_loop(coroutine, timeout=0.001))

    returned_id = service.persist_run_payload_sync(engine_payload())
    assert returned_id is None
    assert committed.wait(timeout=2)
    assert saved == [2430]
