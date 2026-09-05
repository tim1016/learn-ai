"""A restarted data service closes the jobs it was running.

Jobs run on threads of the service process; a crash, an out-of-memory kill or
a restart takes every worker with it, and nothing else ever closes their
records — the job reads as live until its TTL and the research record behind
it presents as running and refuses Finish (the 2026-09-05 grid-search stall).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
import redis

from app.jobs import progress
from app.jobs.progress import ORPHANED_JOB_CODE, fail_jobs_without_a_worker
from app.research.persistence import lifecycle

ACTIVE = "jobs:active"


class _FakeRedis:
    """Enough of Redis for the active set, the state hashes and the event streams."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[dict[str, Any]]] = {}

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).discard(member)

    def hget(self, key: str, name: str) -> str | None:
        return self.hashes.get(key, {}).get(name)

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **_: Any) -> None:
        self.hashes.setdefault(key, {}).update(mapping or {})

    def xadd(self, key: str, fields: dict[str, Any], maxlen: int | None = None, approximate: bool = True) -> str:
        self.streams.setdefault(key, []).append(dict(fields))
        return f"0-{len(self.streams[key])}"

    def expire(self, key: str, seconds: int) -> None:
        return None

    def job(self, job_id: str, status: str) -> None:
        self.sadd(ACTIVE, job_id)
        self.hset(f"job:{job_id}:state", mapping={"id": job_id, "status": status})

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [json.loads(entry["event"]) for entry in self.streams.get(f"job:{job_id}:events", [])]


class _UnreachableRedis:
    def smembers(self, key: str) -> set[str]:
        raise redis.ConnectionError("connection refused")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)
    return fake


def test_queued_and_running_jobs_are_failed_and_their_streams_closed(fake: _FakeRedis) -> None:
    fake.job("running-1", "running")
    fake.job("queued-1", "queued")

    assert fail_jobs_without_a_worker() == ["queued-1", "running-1"]

    for job_id in ("running-1", "queued-1"):
        state = fake.hashes[f"job:{job_id}:state"]
        assert (state["status"], state["error_code"]) == ("failed", ORPHANED_JOB_CODE)
        assert "did not survive the restart" in state["error_message"]
        assert fake.events(job_id)[-1]["type"] == "job.failed"
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
    row = _Record()
    assert lifecycle.presented_status(row, live=lifecycle.job_is_live(row.job_id)) == "running"

    fail_jobs_without_a_worker()

    assert lifecycle.presented_status(row, live=lifecycle.job_is_live(row.job_id)) == "interrupted"
