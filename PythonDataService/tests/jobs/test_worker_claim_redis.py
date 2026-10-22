"""The actual Redis claim scripts admit one worker and release only its ownership."""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import redis

from app.jobs import progress, runner
from app.jobs.runner import prepared_worker, run_in_thread
from tests.options_backtest.test_runtime_http_boundary import isolated_redis as isolated_redis

pytestmark = pytest.mark.slow


def test_concurrent_claims_have_one_winner_and_preserve_its_token(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    gate = threading.Barrier(2)
    def claim() -> str | None:
        gate.wait(timeout=5)
        try:
            return progress.acquire_lease(job_id)
        except progress.JobLeaseOccupied:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        tokens = [future.result(timeout=10) for future in futures]
    [winner] = [token for token in tokens if token is not None]
    assert client.get(progress.lease_key(job_id)) == winner
    assert 0 < client.ttl(progress.lease_key(job_id)) <= progress.JOB_LEASE_TTL_SECONDS
    progress.release_lease(job_id, "another-owner")
    assert client.get(progress.lease_key(job_id)) == winner
    progress.release_lease(job_id, winner)
    assert client.get(progress.lease_key(job_id)) is None


@pytest.mark.parametrize("prior", ["completed", "failed", "cancelled", "started"])
def test_a_started_or_terminal_job_cannot_be_claimed_after_its_lease_expires(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch, prior: str,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    state = {"status": "running", "started_at": "1000"} if prior == "started" else {"status": prior}
    client.hset(progress.state_key(job_id), mapping=state)
    with pytest.raises(progress.JobAlreadyStarted):
        progress.acquire_lease(job_id)
    assert client.get(progress.lease_key(job_id)) is None
    assert client.hgetall(progress.state_key(job_id)) == state


def test_a_finished_worker_cannot_release_a_replacement_lease(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    def work(emit: progress.ProgressEmitter, cancel: progress.CancellationCheck) -> None:
        client.set(progress.lease_key(job_id), "replacement", ex=30)
    run_in_thread(job_id, work).join(timeout=5)
    assert client.get(progress.lease_key(job_id)) == "replacement"


def test_a_binding_failure_releases_only_the_prepared_workers_lease(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    with pytest.raises(ValueError, match="binding failed"), prepared_worker(job_id):
        raise ValueError("binding failed")
    assert client.get(progress.lease_key(job_id)) is None
    with prepared_worker(job_id) as launch:
        launch.start(lambda emit, cancel: None).join(timeout=5)


def test_a_lost_thread_start_ack_keeps_the_running_workers_lease_until_it_exits(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    entered, finish = threading.Event(), threading.Event()
    launched: list[threading.Thread] = []
    actual_start = threading.Thread.start

    def start_then_lose_ack(thread: threading.Thread) -> None:
        actual_start(thread)
        if thread.name == "uncertain-launch-test":
            launched.append(thread)
            raise OSError("thread start acknowledgement lost")

    def work(emit: progress.ProgressEmitter, cancel: progress.CancellationCheck) -> dict[str, bool]:
        entered.set()
        assert finish.wait(timeout=5)
        return {"ok": True}

    monkeypatch.setattr(threading.Thread, "start", start_then_lose_ack)
    try:
        with pytest.raises(OSError, match="acknowledgement lost"):
            run_in_thread(job_id, work, thread_name="uncertain-launch-test")
        assert entered.wait(timeout=5)
        assert client.get(progress.lease_key(job_id))
        assert client.hget(progress.state_key(job_id), "status") == "running"
    finally:
        finish.set()
        for thread in launched:
            thread.join(timeout=5)
    assert client.get(progress.lease_key(job_id)) is None
    assert client.hget(progress.state_key(job_id), "status") == "completed"


@pytest.mark.parametrize("blocked_operation", ["acquire", "release"])
async def test_repeated_cancellation_drains_claim_and_cleanup_without_blocking_the_coordinator(
    isolated_redis: str, monkeypatch: pytest.MonkeyPatch, blocked_operation: str,
) -> None:
    client = redis.Redis.from_url(isolated_redis, decode_responses=True)
    monkeypatch.setattr(progress, "get_redis", lambda: client)
    job_id = uuid4().hex
    loop, coordinator_thread = asyncio.get_running_loop(), threading.get_ident()
    io_started, allow_io, io_finished = asyncio.Event(), threading.Event(), threading.Event()
    acquire, release = runner.acquire_lease, runner.release_lease

    def held_io(operation: str) -> None:
        assert threading.get_ident() != coordinator_thread
        if blocked_operation == operation:
            loop.call_soon_threadsafe(io_started.set)
            assert allow_io.wait(timeout=5)

    def checked_acquire(job_id: str) -> str:
        held_io("acquire")
        token = acquire(job_id)
        if blocked_operation == "acquire":
            io_finished.set()
        return token

    def checked_release(job_id: str, token: str) -> None:
        held_io("release")
        release(job_id, token)
        if blocked_operation == "release":
            io_finished.set()

    async def bind() -> None:
        async with runner.async_prepared_worker(job_id):
            assert blocked_operation == "release", "a cancelled acquisition cannot bind or launch"

    monkeypatch.setattr(runner, "acquire_lease", checked_acquire)
    monkeypatch.setattr(runner, "release_lease", checked_release)
    dispatch = asyncio.create_task(bind())
    try:
        await asyncio.wait_for(io_started.wait(), timeout=5)
        for _ in range(3):
            dispatch.cancel()
            await asyncio.sleep(0)
        still_draining = not dispatch.done()
        allow_io.set()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
        assert await asyncio.to_thread(io_finished.wait, 5)
        assert client.get(progress.lease_key(job_id)) is None
        assert still_draining, "cancellation must not release ownership before its thread has ended"
    finally:
        allow_io.set()
        await asyncio.gather(dispatch, return_exceptions=True)
        await asyncio.to_thread(io_finished.wait, 5)
        client.delete(progress.lease_key(job_id), progress.state_key(job_id))
