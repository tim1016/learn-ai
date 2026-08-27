"""Tests for app.utils.throttle (Slice 1F).

``TtlCache`` short-circuits a repeated option-contract drill-down within
its TTL. Its former companion ``TokenBucket`` retired with
``/api/broker/symbols/search`` (PR-B of #1813, 2026-08-27); its tests
went with it. Driven by an injected ``now()`` clock so the suite does not
rely on wall-clock time.
"""

from __future__ import annotations

from app.utils.throttle import TtlCache


class _Clock:
    """Drop-in for ``time.monotonic`` whose value advances only when
    ``advance`` is called. Lets the test pin TTL expiry without sleeping."""

    def __init__(self, t0: float = 0.0) -> None:
        self._t = t0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


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
