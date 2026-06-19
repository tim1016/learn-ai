"""Tests for app.utils.throttle (Slice 1F).

``TokenBucket`` gates rapid-repeat calls to IBKR ``reqMatchingSymbols``
(the upstream limit is ~1 request per 5s per pattern).
``TtlCache`` softens the same surface — a cached pattern doesn't even
draw a token. Both are tested with an injected ``now()`` clock so the
suite does not rely on wall-clock time.
"""

from __future__ import annotations

import pytest

from app.utils.throttle import TokenBucket, TtlCache


class _Clock:
    """Drop-in for ``time.monotonic`` whose value advances only when
    ``advance`` is called. Lets the test pin tokenbucket fill timing
    without sleeping."""

    def __init__(self, t0: float = 0.0) -> None:
        self._t = t0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def test_token_bucket_admits_until_capacity_exhausted() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_second=0.2, capacity=2, now=clock)

    assert bucket.try_acquire("k") == 0.0
    assert bucket.try_acquire("k") == 0.0
    retry = bucket.try_acquire("k")
    assert retry > 0.0


def test_token_bucket_refills_over_time() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_second=0.2, capacity=1, now=clock)

    assert bucket.try_acquire("k") == 0.0  # drain
    assert bucket.try_acquire("k") > 0.0  # immediately denied

    clock.advance(5.0)  # one token / 5s @ 0.2 Hz
    assert bucket.try_acquire("k") == 0.0


def test_token_bucket_keys_are_independent() -> None:
    """Two operators searching different patterns should not throttle
    each other — the bucket keys by ``(pattern, sec_type)``."""

    clock = _Clock()
    bucket = TokenBucket(rate_per_second=0.2, capacity=1, now=clock)

    assert bucket.try_acquire("SPY") == 0.0
    assert bucket.try_acquire("QQQ") == 0.0
    assert bucket.try_acquire("SPY") > 0.0


def test_token_bucket_retry_after_is_seconds_until_one_token() -> None:
    clock = _Clock()
    bucket = TokenBucket(rate_per_second=0.5, capacity=1, now=clock)

    bucket.try_acquire("k")
    retry = bucket.try_acquire("k")

    # One token refills every 1/rate = 2s.
    assert retry == pytest.approx(2.0)


def test_ttl_cache_returns_value_within_ttl() -> None:
    clock = _Clock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=5.0, max_size=10, now=clock)

    cache.set("k", 42)
    assert cache.get("k") == 42

    clock.advance(4.9)
    assert cache.get("k") == 42


def test_ttl_cache_expires_value_after_ttl() -> None:
    clock = _Clock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=5.0, max_size=10, now=clock)

    cache.set("k", 42)
    clock.advance(5.1)
    assert cache.get("k") is None


def test_ttl_cache_evicts_oldest_when_full() -> None:
    clock = _Clock()
    cache: TtlCache[str, int] = TtlCache(ttl_seconds=60.0, max_size=2, now=clock)

    cache.set("a", 1)
    clock.advance(1.0)
    cache.set("b", 2)
    clock.advance(1.0)
    cache.set("c", 3)  # evicts "a"

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
