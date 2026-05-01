"""Tests for the result cache.

The cache module talks to Redis; we patch ``get_redis`` with a fake
in-memory store to keep these tests hermetic.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.jobs import cache


class FakeRedis:
    """Minimal Redis stand-in: get/set/expire/hset/srem/xadd/pipeline."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.hashes: dict[str, dict[str, Any]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._stream_seq = 0

    def get(self, key: str) -> Any:
        return self.store.get(key)

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.store[key] = value

    def expire(self, key: str, _seconds: int) -> None:
        return None

    def hset(self, key: str, mapping: dict[str, Any] | None = None, **_: Any) -> None:
        if mapping:
            self.hashes.setdefault(key, {}).update(mapping)

    def srem(self, _key: str, _member: str) -> None:
        return None

    def xadd(self, key: str, fields: dict[str, Any], maxlen: int | None = None, approximate: bool = True) -> str:
        self._stream_seq += 1
        entry_id = f"0-{self._stream_seq}"
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    def pipeline(self) -> FakeRedis:
        # Pipeline behaves like the same client for our purposes; tests
        # only care that calls land in the store, not that they're
        # batched atomically.
        return self

    def execute(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: fake)
    # The serve_cached_result function imports get_redis lazily from
    # progress; patch that path too.
    from app.jobs import progress
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    return fake


class TestParamsHash:
    def test_same_params_same_hash(self) -> None:
        a = cache.params_hash("feature_research", {"ticker": "AAPL", "from": "2024-01-01"})
        b = cache.params_hash("feature_research", {"from": "2024-01-01", "ticker": "AAPL"})
        assert a == b  # Order-insensitive

    def test_ticker_case_normalizes(self) -> None:
        a = cache.params_hash("cross_sectional", {"tickers": ["spy", "QQQ"]})
        b = cache.params_hash("cross_sectional", {"tickers": ["SPY", "qqq"]})
        assert a == b

    def test_different_job_type_different_hash(self) -> None:
        a = cache.params_hash("feature_research", {"ticker": "AAPL"})
        b = cache.params_hash("signal_engine", {"ticker": "AAPL"})
        assert a != b


class TestCacheRoundTrip:
    def test_miss_returns_none(self) -> None:
        assert cache.lookup("feature_research", {"ticker": "AAPL"}) is None

    def test_store_then_lookup_hits(self) -> None:
        params = {"ticker": "AAPL", "from": "2024-01-01"}
        cache.store("feature_research", params, {"verdict": "stage 2"})

        hit = cache.lookup("feature_research", params)
        assert hit is not None
        h, payload = hit
        assert payload["result"] == {"verdict": "stage 2"}
        assert "cached_at" in payload
        assert isinstance(h, str) and len(h) == 40  # sha1 hex


class TestServeCachedResult:
    def test_writes_result_and_emits_completed(self, fake_redis: FakeRedis) -> None:
        # Seed a cached payload directly.
        params = {"ticker": "AAPL"}
        cache.store("feature_research", params, {"score": 1})
        hit = cache.lookup("feature_research", params)
        assert hit is not None

        cache.serve_cached_result("job-123", "feature_research", hit[1])

        # Result should be written to the per-job result key.
        assert "job:job-123:result" in fake_redis.store
        # State hash should reflect completed + cached=1.
        state = fake_redis.hashes.get("job:job-123:state", {})
        assert state.get("status") == "completed"
        assert state.get("cached") == "1"
        # Two stream entries (started, completed) on the events key.
        events = fake_redis.streams.get("job:job-123:events", [])
        assert len(events) == 2
        bodies = [e[1] for e in events]
        assert any('"job.started"' in b.get("event", "") for b in bodies)
        assert any('"cached": true' in b.get("event", "") for b in bodies)
