"""The worker lease is liveness (#1938).

A job is live exactly while its Redis record says queued/running **and** a
worker still holds it — proven by the ``job:{id}:lease`` key the worker
acquires on dispatch and renews with every emit and cancellation check. A
worker that dies or hangs silently stops renewing, so the record reads not
live within ``JOB_LEASE_TTL_SECONDS`` instead of claiming ``running`` until
the 24 h TTL.
"""

from __future__ import annotations

import pytest
import redis

from app.jobs.progress import (
    JOB_LEASE_TTL_SECONDS,
    JOB_TTL_SECONDS,
    CancellationCheck,
    ProgressEmitter,
    acquire_lease,
    release_lease,
    renew_lease,
)
from app.jobs.runner import run_in_thread
from app.research.persistence import lifecycle
from tests.jobs.conftest import _FakeRedis

LEASE = "job:lease-1:lease"


def test_a_running_job_with_a_held_lease_is_live(fake: _FakeRedis) -> None:
    fake.job("lease-1", "running")
    fake.lease("lease-1")

    assert lifecycle.job_is_live("lease-1") is True


def test_a_running_job_without_a_lease_is_not_live(fake: _FakeRedis) -> None:
    """The dead-worker case the status fields could not see: the record still says running."""
    fake.job("lease-1", "running")

    assert lifecycle.job_is_live("lease-1") is False


def test_a_terminal_job_is_not_live_even_while_its_lease_lingers(fake: _FakeRedis) -> None:
    fake.job("lease-1", "completed")
    fake.lease("lease-1")

    assert lifecycle.job_is_live("lease-1") is False


def test_an_expired_lease_stays_expired_when_the_worker_emits_again(fake: _FakeRedis) -> None:
    """A worker that lost its lease may not resurrect liveness: the record read
    as interrupted, and only a terminal event may close it for good."""
    fake.job("lease-1", "running")
    fake.lease("lease-1")
    fake.strings.pop(LEASE)  # the TTL passed

    ProgressEmitter("lease-1").log("late emit")
    renew_lease("lease-1")

    assert lifecycle.job_is_live("lease-1") is False


def test_an_unknown_job_and_an_unanswerable_store(fake: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    assert lifecycle.job_is_live(None) is False

    class _Unreachable:
        def pipeline(self) -> None:
            raise redis.ConnectionError("connection refused")

    monkeypatch.setattr(lifecycle, "get_redis", lambda: _Unreachable())
    assert lifecycle.job_is_live("lease-1") is None


def test_acquire_and_release_the_lease(fake: _FakeRedis) -> None:
    acquire_lease("lease-1")
    assert fake.exists(LEASE)

    release_lease("lease-1")
    assert not fake.exists(LEASE)


def test_every_emit_renews_the_lease_and_the_stream_ttl(fake: _FakeRedis) -> None:
    fake.job("lease-1", "running")
    acquire_lease("lease-1")

    ProgressEmitter("lease-1").phase("run")

    assert (LEASE, JOB_LEASE_TTL_SECONDS) in fake.expires
    assert ("job:lease-1:events", JOB_TTL_SECONDS) in fake.expires


def test_state_patches_refresh_the_state_hash_ttl(fake: _FakeRedis) -> None:
    """Hazard (c): EXPIRE was set only at create, so a late patch after expiry
    recreated a partial record with no TTL at all."""
    fake.job("lease-1", "running")

    ProgressEmitter("lease-1").phase("run")

    assert ("job:lease-1:state", JOB_TTL_SECONDS) in fake.expires


def test_the_cancellation_check_renews_the_lease(fake: _FakeRedis) -> None:
    fake.job("lease-1", "running")
    acquire_lease("lease-1")
    check = CancellationCheck("lease-1", check_every_n=1)

    assert check.should_cancel() is False

    assert (LEASE, JOB_LEASE_TTL_SECONDS) in fake.expires


def test_run_in_thread_holds_the_lease_for_the_work_and_releases_it(fake: _FakeRedis) -> None:
    seen: dict[str, bool] = {}

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, bool]:
        seen["held"] = fake.exists(LEASE)
        return {"ok": True}

    run_in_thread("lease-1", work, thread_name="lease-test").join(timeout=5)

    assert seen["held"] is True
    assert not fake.exists(LEASE)


def test_run_in_thread_releases_the_lease_when_the_work_raises(fake: _FakeRedis) -> None:
    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> None:
        raise RuntimeError("boom")

    run_in_thread("lease-1", work, thread_name="lease-test").join(timeout=5)

    assert fake.hashes["job:lease-1:state"]["status"] == "failed"
    assert not fake.exists(LEASE)
