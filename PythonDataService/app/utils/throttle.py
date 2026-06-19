"""In-process token bucket + TTL cache used by Slice 1F's broker search
endpoints (``/api/broker/symbols/search`` and friends).

The two pieces compose to honor IBKR's ``reqMatchingSymbols`` ceiling
(~1 request per 5s) without paging tickets onto a remote queue:

* ``TtlCache`` short-circuits a repeated pattern within 60s — the
  request never even consults the bucket.
* ``TokenBucket`` rate-limits the slow path. On exhaustion it returns
  the retry-after (seconds) so the router can render a clean
  ``Retry-After`` header instead of slamming IBKR.

Both accept a ``now`` callable so the suite can drive them with a fake
clock; defaults to ``time.monotonic``.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable


class TokenBucket:
    """Classic token bucket, keyed.

    A separate bucket is maintained per ``key`` so independent search
    patterns don't throttle each other (one operator typing ``SPY``
    must not block another typing ``QQQ``).

    ``rate_per_second`` is the steady-state issuance rate; ``capacity``
    is the burst. ``try_acquire`` returns ``0.0`` on success and the
    seconds-until-one-token on denial.
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._rate = rate_per_second
        self._capacity = capacity
        self._now = now
        self._state: dict[Hashable, tuple[float, float]] = {}

    def try_acquire(self, key: Hashable) -> float:
        now = self._now()
        tokens, last = self._state.get(key, (float(self._capacity), now))
        elapsed = max(0.0, now - last)
        tokens = min(float(self._capacity), tokens + elapsed * self._rate)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return 0.0
        self._state[key] = (tokens, now)
        return (1.0 - tokens) / self._rate


class TtlCache[K: Hashable, V]:
    """Insertion-ordered, fixed-size, per-entry TTL cache.

    Eviction policy is "drop the oldest insertion" once ``max_size`` is
    reached; expired entries are pruned lazily on ``get``. Designed for
    the symbol-search response shape (≤ a few hundred patterns, each
    holding a small DTO list); for hotter paths consider an LRU with an
    explicit recency move on read.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_size: int,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._ttl = ttl_seconds
        self._max = max_size
        self._now = now
        self._store: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._now() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V) -> None:
        now = self._now()
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (now + self._ttl, value)
        while len(self._store) > self._max:
            self._store.popitem(last=False)
