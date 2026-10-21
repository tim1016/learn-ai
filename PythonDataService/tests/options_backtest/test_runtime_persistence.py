"""Real disposable-Postgres fences and lost save acknowledgements (#3423)."""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import asyncpg
import pytest
import redis

from app.jobs.runner import JobWork
from app.options_backtest import saved_runs, service
from app.options_backtest.saved_runs import DbRunStore, NewRun, RunFailure
from app.options_backtest.timings import RunTimings
from app.research.persistence.ownership import COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID, GenerationFencedError
from app.research.persistence.schema import ensure_schema
from app.utils.background_loop import CallerStoppedWaitingError
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest.test_runtime_http_boundary import isolated_redis as isolated_redis
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
    service.record_observations(store, run_id, ledger)
    assert "execution observations" in caplog.text
    assert await saved_runs.completion_matches(scratch_db, run_id, result)


async def test_runtime_create_and_owner_stamp_roll_back_together_and_retry_is_not_stranded(
    scratch_db: asyncpg.Connection, isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.jobs import progress
    from app.research.persistence import ownership

    await ensure_schema(scratch_db)
    await scratch_db.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
    authority = ownership.RuntimeAuthority("current", "research", 2, scratch_db)
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    result = a_busy_run()
    new = NewRun("atomic-create", 1791800000000, result.setup, None,
                 result.sessions[0].session_open_ms, result.sessions[-1].session_open_ms, len(result.sessions))
    stamp = ownership.record_dispatch

    async def interrupted_stamp(*args, **kwargs) -> None:
        await stamp(*args, **kwargs)
        raise OSError("stamp transaction interrupted before commit")

    monkeypatch.setattr(ownership, "record_dispatch", interrupted_stamp)
    with pytest.raises(OSError, match="interrupted"):
        await saved_runs._accept_runtime_run(new, 2, lambda run_id: lambda emit, cancel: None)
    assert await saved_runs.run_for_job(scratch_db, new.job_id) is None
    assert await scratch_db.fetchval("SELECT count(*) FROM research_job_owners WHERE job_id=$1", new.job_id) == 0
    assert await scratch_db.fetchval("SELECT count(*) FROM research_options_search_exposures WHERE owner_id=$1", f"backtest:{new.job_id}") == 0
    assert client.get(progress.lease_key(new.job_id)) is None
    monkeypatch.setattr(ownership, "record_dispatch", stamp)
    def after_commit(run_id: int) -> JobWork:
        assert not scratch_db.is_in_transaction()
        return lambda emit, cancel: None
    run_id = await saved_runs._accept_runtime_run(new, 2, after_commit)
    assert await saved_runs.run_for_job(scratch_db, new.job_id) == run_id
    owner = await scratch_db.fetchrow("SELECT * FROM research_job_owners WHERE job_id=$1", new.job_id)
    assert owner["record_id"] == str(run_id)
    assert owner["boot_generation"] == 2
    assert await scratch_db.fetchval("SELECT status FROM research_options_backtest_runs WHERE id=$1", run_id) == "running"
    with pytest.raises(GenerationFencedError):
        await saved_runs._accept_runtime_run(new, 1, after_commit)
    with pytest.raises(saved_runs.DuplicateJob):
        await saved_runs._accept_runtime_run(new, 2, after_commit)


@pytest.mark.parametrize("replace_lease", [False, True])
async def test_failed_activation_releases_only_its_own_initial_lease(
    scratch_db: asyncpg.Connection, isolated_redis: str, monkeypatch: pytest.MonkeyPatch, replace_lease: bool,
) -> None:
    from app.jobs import progress, runner
    from app.research.persistence import ownership

    await ensure_schema(scratch_db)
    await scratch_db.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
    authority = ownership.RuntimeAuthority("current", "research", 2, scratch_db)
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    result = a_busy_run()
    new = NewRun("failed-activation", 1791800000000, result.setup, None,
                 result.sessions[0].session_open_ms, result.sessions[-1].session_open_ms, len(result.sessions))
    def no_thread(*args: Any, **kwargs: Any) -> None:
        if replace_lease:
            client.set(progress.lease_key(new.job_id), "replacement", ex=30)
        raise OSError("thread did not start")

    monkeypatch.setattr(runner.PreparedWorker, "start", no_thread)
    try:
        with pytest.raises(OSError, match="thread did not start"):
            await saved_runs._accept_runtime_run(new, 2, lambda run_id: lambda emit, cancel: None)
        run_id = await saved_runs.run_for_job(scratch_db, new.job_id)
        assert client.get(progress.lease_key(new.job_id)) == ("replacement" if replace_lease else None)
        assert await scratch_db.fetchval("SELECT status FROM research_options_backtest_runs WHERE id=$1", run_id) == "failed"
        with pytest.raises(saved_runs.RunNotRunning):
            await saved_runs._accept_runtime_run(new, 2, lambda run_id: lambda emit, cancel: None)
    finally:
        client.delete(progress.lease_key(new.job_id), progress.state_key(new.job_id))


async def test_a_worker_thread_that_cannot_start_leaves_no_live_lease(
    scratch_db: asyncpg.Connection, isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure before any thread exists leaves the committed run failed and releases its own worker lease."""
    from app.jobs import progress
    from app.research.persistence import ownership

    await ensure_schema(scratch_db)
    await scratch_db.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
    authority = ownership.RuntimeAuthority("current", "research", 2, scratch_db)
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    result = a_busy_run()
    new = NewRun("unstartable-worker", 1791800000000, result.setup, None,
                 result.sessions[0].session_open_ms, result.sessions[-1].session_open_ms, len(result.sessions))
    start = threading.Thread.start

    def refuse_the_worker(self: threading.Thread) -> None:
        if self.name.startswith("options-backtest-"):
            raise RuntimeError("can't start new thread")
        start(self)

    monkeypatch.setattr(threading.Thread, "start", refuse_the_worker)
    try:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            await saved_runs._accept_runtime_run(new, 2, lambda run_id: lambda emit, cancel: None)
        run_id = await saved_runs.run_for_job(scratch_db, new.job_id)
        assert client.get(progress.lease_key(new.job_id)) is None
        assert await scratch_db.fetchval("SELECT status FROM research_options_backtest_runs WHERE id=$1", run_id) == "failed"
    finally:
        client.delete(progress.lease_key(new.job_id), progress.state_key(new.job_id))


@pytest.mark.parametrize("status", ["running", "complete"])
async def test_a_durable_redelivery_does_not_create_new_liveness_after_redis_expires(
    scratch_db: asyncpg.Connection, isolated_redis: str, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    from app.jobs import progress
    from app.research.persistence import ownership

    run_id, result = await new_run(scratch_db)
    if status == "complete":
        await saved_runs.complete_run(scratch_db, run_id, result, finished_at_ms=1791828000000)
    await scratch_db.execute("INSERT INTO research_runtime_owners VALUES('coordinator','current','research',2,1)")
    await scratch_db.execute("INSERT INTO research_job_owners VALUES('runtime-test','options_backtest',$1,NULL,'current',2,1)", str(run_id))
    await scratch_db.execute("SELECT pg_advisory_lock($1,$2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
    authority = ownership.RuntimeAuthority("current", "research", 2, scratch_db)
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    client.delete(progress.lease_key("runtime-test"), progress.state_key("runtime-test"))
    result = a_busy_run()
    new = NewRun("runtime-test", 1791800000000, result.setup, None,
                 result.sessions[0].session_open_ms, result.sessions[-1].session_open_ms, len(result.sessions))
    with pytest.raises(saved_runs.DuplicateJob if status == "running" else saved_runs.RunNotRunning):
        await saved_runs._accept_runtime_run(new, 2, lambda run_id: pytest.fail("redelivery cannot create a worker"))
    assert client.get(progress.lease_key(new.job_id)) is None
    assert client.hgetall(progress.state_key(new.job_id)) == {}
