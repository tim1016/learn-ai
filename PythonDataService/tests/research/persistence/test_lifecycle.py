"""``lifecycle.job_state`` reads the transport's Redis job record; the redelivery and delete decisions above it depend on this mapping."""

from __future__ import annotations

import pytest
import redis

from app.research.persistence import lifecycle


class _FakeRedis:
    def __init__(self, status: str | None, *, unreachable: bool = False) -> None:
        self._status = status
        self._unreachable = unreachable
        self.active: list[str] = []
        self.expires: list[tuple[str, int]] = []

    def hget(self, key: str, field: str) -> str | None:
        if self._unreachable:
            raise redis.RedisError("down")
        return self._status

    def sadd(self, key: str, member: str) -> None:
        self.active.append(member)

    def expire(self, key: str, seconds: int) -> None:
        self.expires.append((key, seconds))

    def pipeline(self) -> _FakeRedis:
        return self

    def execute(self) -> None:
        return None


@pytest.mark.parametrize(
    ("status", "expected"),
    [("queued", "live"), ("running", "live"), ("completed", "finished"), ("failed", "finished"), ("cancelled", "cancelled"), (None, "absent")],
)
def test_job_state_maps_the_record_status(monkeypatch: pytest.MonkeyPatch, status: str | None, expected: str) -> None:
    monkeypatch.setattr(lifecycle, "get_redis", lambda: _FakeRedis(status))
    assert lifecycle.job_state("job-1") == expected
    assert lifecycle.job_is_live("job-1") is (expected == "live")


def test_job_state_is_unknown_when_redis_cannot_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "get_redis", lambda: _FakeRedis(None, unreachable=True))
    assert lifecycle.job_state("job-1") is None
    assert lifecycle.job_is_live("job-1") is None


def test_a_missing_job_id_is_absent_without_asking_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_connect() -> _FakeRedis:
        raise AssertionError("Redis must not be asked about a missing job id")

    monkeypatch.setattr(lifecycle, "get_redis", must_not_connect)
    assert lifecycle.job_state(None) == "absent"
    assert lifecycle.job_is_live(None) is False


def test_reclaim_job_returns_the_job_to_the_active_set_and_renews_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis("completed")
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)
    lifecycle.reclaim_job("job-1")
    assert fake.active == ["job-1"]
    assert fake.expires == [(lifecycle._state_key("job-1"), lifecycle.JOB_TTL_SECONDS)]
