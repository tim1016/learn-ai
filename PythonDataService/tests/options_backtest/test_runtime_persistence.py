"""Real disposable-Postgres fences and lost save acknowledgements (#3423)."""
from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from app.options_backtest import saved_runs, service
from app.options_backtest.saved_runs import DbRunStore, NewRun, RunFailure
from app.options_backtest.timings import RunTimings
from app.research.persistence.ownership import COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID, GenerationFencedError
from app.research.persistence.schema import ensure_schema
from app.utils.background_loop import CallerStoppedWaitingError
from tests.options_backtest._a_run import a_busy_run
from tests.research.conftest import _ephemeral_url
from tests.research.conftest import scratch_db as scratch_db

pytestmark = pytest.mark.slow


async def new_run(conn: asyncpg.Connection) -> tuple[int, Any]:
    await ensure_schema(conn)
    result = a_busy_run()
    run_id = await saved_runs.create_run(conn, NewRun("runtime-test", 1791800000000, result.setup, None,
        result.sessions[0].session_open_ms, result.sessions[-1].session_open_ms, len(result.sessions)))
    return run_id, result


async def test_stale_generation_cannot_publish_fail_cancel_or_observations(scratch_db: asyncpg.Connection) -> None:
    run_id, result = await new_run(scratch_db)
    await scratch_db.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await scratch_db.execute("INSERT INTO research_job_owners VALUES('runtime-test','options_backtest',$1,NULL,'old',1,1)", str(run_id))
    for change in (
        lambda: saved_runs.complete_run(scratch_db, run_id, result, finished_at_ms=1791828000000, generation=1),
        lambda: saved_runs.fail_run(scratch_db, run_id, RunFailure("FAIL", "failed"), finished_at_ms=1791828000000, generation=1),
        lambda: saved_runs.cancel_run(scratch_db, run_id, finished_at_ms=1791828000000, generation=1),
        lambda: saved_runs.record_timings(scratch_db, run_id, [], {}, generation=1),
    ):
        with pytest.raises(GenerationFencedError):
            await change()
    assert await scratch_db.fetchval("SELECT status FROM research_options_backtest_runs WHERE id=$1", run_id) == "running"
    assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == 0
    await scratch_db.execute("UPDATE research_job_owners SET boot_generation=2 WHERE job_id='runtime-test'")
    with pytest.raises(GenerationFencedError):
        await saved_runs.complete_run(scratch_db, run_id, result, finished_at_ms=1791828000000, generation=2)
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
    await saved_runs.complete_run(scratch_db, run_id, result, finished_at_ms=1791828000000, generation=2)
    assert await saved_runs.completion_matches(scratch_db, run_id, result)
    with pytest.raises(saved_runs.RunNotRunning):
        await saved_runs.complete_run(scratch_db, run_id, result, finished_at_ms=1791828000000, generation=2)
    assert await scratch_db.fetchval("SELECT count(*) FROM research_options_backtest_run_sessions WHERE run_id=$1", run_id) == len(result.sessions)


async def test_lost_ack_after_real_commit_joins_once_and_telemetry_failure_keeps_success(
    scratch_db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    run_id, result = await new_run(scratch_db)
    database = await scratch_db.fetchval("SELECT current_database()")

    async def disposable_connection(fn, *args, **kwargs):
        conn = await asyncpg.connect(_ephemeral_url(), database=database)
        try:
            return await fn(conn, *args, **kwargs)
        finally:
            await conn.close()

    actual = saved_runs.run_on_background_loop
    calls = 0

    def lose_first_ack(coroutine, *, timeout):
        nonlocal calls
        calls += 1
        answer = actual(coroutine, timeout=timeout)
        if calls == 1:
            raise CallerStoppedWaitingError("lost acknowledgement after commit")
        return answer

    monkeypatch.setattr(saved_runs, "with_connection", disposable_connection)
    monkeypatch.setattr(saved_runs, "run_on_background_loop", lose_first_ack)
    store = DbRunStore()
    await asyncio.to_thread(store.complete, run_id, result, finished_at_ms=1791828000000)
    assert calls == 2
    assert await saved_runs.completion_matches(scratch_db, run_id, result)

    def unavailable_metadata(*args, **kwargs) -> None:
        raise OSError("execution metadata unavailable")

    monkeypatch.setattr(store, "record_timings", unavailable_metadata)
    ledger = RunTimings()
    ledger.start()
    service.record_observations(store, run_id, ledger, {})
    assert "execution observations" in caplog.text
    assert await saved_runs.completion_matches(scratch_db, run_id, result)
