"""A restarted data service closes the jobs it was running.

Jobs run on threads of the service process; a crash, an out-of-memory kill or
a restart takes every worker with it, and nothing else ever closes their
records — the job reads as live until its TTL and the research record behind
it presents as running and refuses Finish (the 2026-09-05 grid-search stall).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import redis

from app.jobs import progress
from app.jobs.progress import JOB_STORE_TIMEOUT_SECONDS, ORPHANED_JOB_CODE, fail_jobs_without_a_worker
from app.research.persistence import lifecycle
from tests.jobs.conftest import _FakeRedis

ACTIVE = "jobs:active"


class _UnreachableRedis:
    def smembers(self, key: str) -> set[str]:
        raise redis.ConnectionError("connection refused")


def test_queued_and_running_jobs_are_failed_and_their_streams_closed(fake: _FakeRedis) -> None:
    fake.job("running-1", "running")
    fake.lease("running-1")
    fake.job("queued-1", "queued")

    assert fail_jobs_without_a_worker() == ["queued-1", "running-1"]

    for job_id in ("running-1", "queued-1"):
        state = fake.hashes[f"job:{job_id}:state"]
        assert (state["status"], state["error_code"]) == ("failed", ORPHANED_JOB_CODE)
        assert "did not survive the restart" in state["error_message"]
        assert fake.events(job_id)[-1]["type"] == "job.failed"
        # The worker is gone either way: its lease must not outlive the sweep (#1938).
        assert f"job:{job_id}:lease" not in fake.strings
    assert fake.sets[ACTIVE] == set()


def test_a_finished_job_still_in_the_active_set_is_only_dropped_from_it(fake: _FakeRedis) -> None:
    fake.job("done-1", "completed")

    assert fail_jobs_without_a_worker() == []

    assert fake.hashes["job:done-1:state"]["status"] == "completed"
    assert fake.events("done-1") == []
    assert fake.sets[ACTIVE] == set()


def test_an_id_whose_state_expired_is_dropped_from_the_active_set(fake: _FakeRedis) -> None:
    fake.sadd(ACTIVE, "expired-1")

    assert fail_jobs_without_a_worker() == []

    assert fake.sets[ACTIVE] == set()


def test_an_unreachable_job_store_is_logged_not_fatal(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(progress, "get_redis", lambda: _UnreachableRedis())

    with caplog.at_level("WARNING", logger="app.jobs.progress"):
        assert fail_jobs_without_a_worker() == []

    assert "left by the previous process" in caplog.text


@dataclass(frozen=True)
class _Record:
    status: str = "running"
    job_id: str | None = "running-1"
    incomplete: bool = False
    receipt: dict[str, Any] = field(default_factory=dict)


def test_the_research_record_behind_a_failed_job_reads_as_interrupted(fake: _FakeRedis) -> None:
    """The user-visible half: a search stranded by the restart stops presenting as running."""
    fake.job("running-1", "running")
    fake.lease("running-1")
    row = _Record()
    assert lifecycle.presented_status(row, live=lifecycle.job_is_live(row.job_id)) == "running"

    fail_jobs_without_a_worker()

    assert lifecycle.presented_status(row, live=lifecycle.job_is_live(row.job_id)) == "interrupted"


def test_every_job_store_wait_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup sweep runs before the listener opens: a Redis that drops packets, or accepts the connection and then goes silent, must not hold boot."""
    monkeypatch.setattr(progress, "_pool", None)

    kwargs = progress.get_redis().connection_pool.connection_kwargs

    assert (kwargs["socket_connect_timeout"], kwargs["socket_timeout"]) == (JOB_STORE_TIMEOUT_SECONDS, JOB_STORE_TIMEOUT_SECONDS) == (5.0, 5.0)
