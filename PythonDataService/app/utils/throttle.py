"""In-process TTL cache used by Slice 1F's broker search endpoints.

``TtlCache`` short-circuits a repeated option-contract drill-down within
its TTL so the router never re-qualifies the same contract.

The companion ``TokenBucket`` retired with ``/api/broker/symbols/search``
(PR-B of #1813, 2026-08-27) — the option-contract path it guarded is not
rate-limited upstream, so nothing was left to pace.

Accepts a ``now`` callable so the suite can drive it with a fake clock;
defaults to ``time.monotonic``.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable


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
