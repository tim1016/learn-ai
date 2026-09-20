"""Shared job-store test doubles.

``_FakeRedis`` is enough of Redis for the active set, the state hashes, the
event streams and (since #1938) the worker leases — including the buffered
``pipeline`` shape the emitter, the cancellation check and ``job_is_live``
use. Expiry is not simulated by the clock: a test expires a key by deleting
it, which is the same thing liveness observes (the key is gone).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.jobs import progress
from app.research.persistence import lifecycle


class _FakePipeline:
    """The buffered-command shape of redis-py's plain (non-transactional) pipeline."""

    def __init__(self, fake: _FakeRedis) -> None:
        self._fake = fake
        self._ops: list[Callable[[], Any]] = []

    def hget(self, key: str, name: str) -> _FakePipeline:
        self._ops.append(lambda: self._fake.hget(key, name))
        return self

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **_: Any) -> _FakePipeline:
        self._ops.append(lambda: self._fake.hset(key, mapping=mapping))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self._ops.append(lambda: self._fake.expire(key, seconds))
        return self

    def exists(self, key: str) -> _FakePipeline:
        self._ops.append(lambda: self._fake.exists(key))
        return self

    def execute(self) -> list[Any]:
        results = [op() for op in self._ops]
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[dict[str, Any]]] = {}
        self.strings: dict[str, str] = {}
        self.expires: list[tuple[str, int]] = []

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

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

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.strings[key] = value

    def exists(self, key: str) -> bool:
        return key in self.strings

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.strings.pop(key, None)

    def expire(self, key: str, seconds: int) -> int:
        self.expires.append((key, seconds))
        return 1 if key in self.strings else 0

    def xadd(self, key: str, fields: dict[str, Any], maxlen: int | None = None, approximate: bool = True) -> str:
        self.streams.setdefault(key, []).append(dict(fields))
        return f"0-{len(self.streams[key])}"

    def job(self, job_id: str, status: str) -> None:
        self.sadd("jobs:active", job_id)
        self.hset(f"job:{job_id}:state", mapping={"id": job_id, "status": status})

    def lease(self, job_id: str) -> None:
        self.strings[f"job:{job_id}:lease"] = "1"

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [json.loads(entry["event"]) for entry in self.streams.get(f"job:{job_id}:events", [])]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """The fake store wired into both readers of the job contract."""
    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)
    return fake
