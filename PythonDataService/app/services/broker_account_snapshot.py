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
import logging

from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.ports import BrokerReadPort
from app.broker.contract.registry import get_broker_registry
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_ACCOUNT_SNAPSHOT_TTL_MS = 60_000

_snapshot_cache: dict[
    str,
    tuple[BrokerReadPort, BrokerAccountSnapshot, int],
] = {}
_inflight_reads: dict[
    str,
    tuple[BrokerReadPort, asyncio.Task[BrokerAccountSnapshot]],
] = {}


async def resolve_broker_account_snapshot(broker: str) -> BrokerAccountSnapshot:
    """Return one cached or coalesced account observation for ``broker``."""
    port = get_broker_registry().resolve(broker)
    cached = _snapshot_cache.get(broker)
    if cached is not None:
        cached_port, snapshot, expires_at_ms = cached
        if cached_port is port and now_ms_utc() < expires_at_ms:
            return snapshot
        _snapshot_cache.pop(broker, None)

    in_flight = _inflight_reads.get(broker)
    task = in_flight[1] if in_flight is not None and in_flight[0] is port else None
    if task is None:
        task = asyncio.create_task(port.get_account())
        _inflight_reads[broker] = (port, task)

        def discard_completed_read(
            completed: asyncio.Task[BrokerAccountSnapshot],
        ) -> None:
            current = _inflight_reads.get(broker)
            if current is not None and current[1] is completed:
                _inflight_reads.pop(broker, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.warning(
                    "broker account snapshot read failed",
                    extra={"broker": broker},
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(discard_completed_read)

    snapshot = await asyncio.shield(task)
    _snapshot_cache[broker] = (
        port,
        snapshot,
        now_ms_utc() + _ACCOUNT_SNAPSHOT_TTL_MS,
    )
    return snapshot


def clear_broker_account_snapshot_cache_for_testing() -> None:
    """Clear cached observations and cancel unfinished test reads."""
    _snapshot_cache.clear()
    for _port, task in _inflight_reads.values():
        task.cancel()
    _inflight_reads.clear()
