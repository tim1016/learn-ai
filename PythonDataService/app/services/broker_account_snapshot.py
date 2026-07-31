"""Shared short-lived cache for broker-authored account posture.

Account identity is stable for the life of a registered broker port, while the
Alpaca account read can take several seconds. Route resolution, account chrome,
and account-scoped products must therefore reuse one observation instead of
serially asking the vendor to prove the same account again.

The cache key includes the port object's identity. Re-registering a broker port
(reconnects and tests) naturally creates a fresh cache namespace.
"""

from __future__ import annotations

import asyncio
import time

from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.registry import get_broker_registry

_ACCOUNT_SNAPSHOT_TTL_S = 60.0

_snapshot_cache: dict[tuple[str, int], tuple[BrokerAccountSnapshot, float]] = {}
_inflight_reads: dict[tuple[str, int], asyncio.Task[BrokerAccountSnapshot]] = {}


async def resolve_broker_account_snapshot(broker: str) -> BrokerAccountSnapshot:
    """Return one cached or coalesced account observation for ``broker``."""
    port = get_broker_registry().resolve(broker)
    cache_key = (broker, id(port))
    cached = _snapshot_cache.get(cache_key)
    if cached is not None:
        snapshot, expires_at = cached
        if time.monotonic() < expires_at:
            return snapshot

    task = _inflight_reads.get(cache_key)
    if task is None:
        task = asyncio.create_task(port.get_account())
        _inflight_reads[cache_key] = task

    try:
        snapshot = await asyncio.shield(task)
    finally:
        if _inflight_reads.get(cache_key) is task and task.done():
            _inflight_reads.pop(cache_key, None)

    _snapshot_cache[cache_key] = (
        snapshot,
        time.monotonic() + _ACCOUNT_SNAPSHOT_TTL_S,
    )
    return snapshot


def clear_broker_account_snapshot_cache_for_testing() -> None:
    """Clear cached observations and cancel unfinished test reads."""
    _snapshot_cache.clear()
    for task in _inflight_reads.values():
        task.cancel()
    _inflight_reads.clear()
