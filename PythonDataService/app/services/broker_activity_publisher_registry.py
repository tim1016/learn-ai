"""Per-data-plane registry of running broker-activity publishers.

Split out of ``broker_activity_publisher`` in slice 7 to keep the
publisher module below the 1k-line threshold. The publisher itself is
the per-instance lifecycle owner (event consumer + reconnect sweep +
pending-intent tick); this module is the process-wide singleton that
manages the per-instance fleet and reconnect evidence sweeps.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.services.broker_activity_publisher import BrokerActivityPublisher

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


class BrokerActivityPublisherRegistry:
    """Per-data-plane registry of running publishers, keyed by
    ``strategy_instance_id``.

    Lifecycle hook: when an instance is deployed, ``register`` creates
    and starts a publisher. When the instance stops or the data plane
    shuts down, ``unregister`` (or ``stop_all``) shuts it down.
    """

    def __init__(self) -> None:
        self._by_instance: dict[str, BrokerActivityPublisher] = {}
        # PR 5 — wall-clock ms when each instance was first registered.
        # Used by ``compose_broker_activity_health`` to classify the
        # ``starting`` vs ``unavailable`` states.  Preserved across
        # re-register calls (a superseding publisher for the same
        # instance keeps the original registration timestamp so the
        # health surface doesn't reset the clock).
        self._registered_at_by_instance: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        publisher: BrokerActivityPublisher,
        *,
        strategy_instance_id: str,
    ) -> BrokerActivityPublisher:
        """Add the publisher to the registry and start it. If an entry
        for ``strategy_instance_id`` already exists, the existing
        publisher is stopped first (the new one supersedes it).
        """
        async with self._lock:
            existing = self._by_instance.get(strategy_instance_id)
            if existing is not None and existing is not publisher:
                await existing.stop()
            self._by_instance[strategy_instance_id] = publisher
            # Record the first-registration timestamp; preserve it when
            # a superseding publisher replaces the prior one so the health
            # surface doesn't reset the starting-timeout clock.
            if strategy_instance_id not in self._registered_at_by_instance:
                self._registered_at_by_instance[strategy_instance_id] = _now_ms()
        publisher.start()
        return publisher

    def get(self, strategy_instance_id: str) -> BrokerActivityPublisher | None:
        return self._by_instance.get(strategy_instance_id)

    def registered_at_ms(self, strategy_instance_id: str) -> int | None:
        """Return the wall-clock ms when the publisher for ``strategy_instance_id``
        was first registered, or ``None`` if it has never been registered."""
        return self._registered_at_by_instance.get(strategy_instance_id)

    async def unregister(self, strategy_instance_id: str) -> None:
        async with self._lock:
            publisher = self._by_instance.pop(strategy_instance_id, None)
            self._registered_at_by_instance.pop(strategy_instance_id, None)
        if publisher is not None:
            await publisher.stop()

    async def stop_all(self) -> None:
        """Shutdown hook — stop every running publisher. The registry is
        left empty; the data plane's FastAPI lifespan calls this from
        the shutdown handler."""
        async with self._lock:
            publishers = list(self._by_instance.values())
            self._by_instance.clear()
            self._registered_at_by_instance.clear()
        await asyncio.gather(*(p.stop() for p in publishers))

    def instances(self) -> tuple[str, ...]:
        return tuple(self._by_instance.keys())

    # ── reconnect recovery (slice 3 / ADR 0011 amendment) ─────────────

    async def sweep_all_for_recovery(self) -> dict[str, int]:
        """Run ``sweep_reconnect_recovery`` on every registered publisher.

        Wired into the ``AutoReconnectMonitor.recovery_callbacks`` chain
        by the FastAPI lifespan so every per-instance publisher gets a
        chance to catch up on missed executions after a successful
        reconnect. Sweeps run sequentially — a single shared IBKR
        connection can only serve one ``reqExecutionsAsync`` at a time,
        and parallel sweeps would only contend for the same wire.

        Returns ``{strategy_instance_id: rows_authored}`` so the monitor
        can log it. A publisher whose sweep raises is logged and skipped
        — the monitor must NOT halt the recovery chain because one
        instance's broker-activity sweep failed (the engine still got
        its reconnect; downstream code that needs the missed rows can
        backfill from the WAL once the publisher recovers on its next
        sweep).
        """
        results: dict[str, int] = {}
        # Snapshot the dict under the lock; the sweep itself does not
        # need to hold the registry lock (the publishers own their own
        # serialisation via ``_recovery_lock``).
        async with self._lock:
            snapshot = list(self._by_instance.items())
        for sid, publisher in snapshot:
            try:
                results[sid] = await publisher.sweep_reconnect_recovery()
            except Exception:
                logger.exception(
                    "broker-activity reconnect sweep raised; continuing",
                    extra={"strategy_instance_id": sid},
                )
                results[sid] = 0
        return results


# Module-level singleton — one registry per data-plane process. Imported
# by the lifecycle wiring in ``live_instances`` and by the SSE/REST
# endpoint module. Tests construct fresh registries; production reads
# this one.
_REGISTRY = BrokerActivityPublisherRegistry()


def get_publisher_registry() -> BrokerActivityPublisherRegistry:
    return _REGISTRY


__all__ = [
    "BrokerActivityPublisherRegistry",
    "get_publisher_registry",
]
