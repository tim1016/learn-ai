"""Per-data-plane registry of running broker-activity publishers.

Split out of ``broker_activity_publisher`` in slice 7 to keep the
publisher module below the 1k-line threshold. The publisher itself is
the per-instance lifecycle owner (event consumer + reconnect sweep +
pending-intent tick); this module is the process-wide singleton that
manages the per-instance fleet and routes recovery-chain orchestration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.services.broker_activity_publisher import BrokerActivityPublisher

logger = logging.getLogger(__name__)


class BrokerActivityPublisherRegistry:
    """Per-data-plane registry of running publishers, keyed by
    ``strategy_instance_id``.

    Lifecycle hook: when an instance is deployed, ``register`` creates
    and starts a publisher. When the instance stops or the data plane
    shuts down, ``unregister`` (or ``stop_all``) shuts it down.
    """

    def __init__(self) -> None:
        self._by_instance: dict[str, BrokerActivityPublisher] = {}
        self._lock = asyncio.Lock()
        # Slice 3 follow-up — process-wide reconnect-halt flag that
        # covers the *entire* post-reconnect recovery window, not just
        # the executions-sweep slice. The per-publisher
        # ``_reconnect_recovery_active`` only flips inside
        # ``sweep_reconnect_recovery`` (under that publisher's lock),
        # so any recovery callback that runs before the sweep — notably
        # the bar aggregator's ``resubscribe_all`` — would leave
        # submissions enabled. Without this outer flag a slow bar
        # resubscribe lets a new order land mid-recovery and the
        # subsequent sweep authors it as a ``reconnect_recovery`` row,
        # which is the race the slice is meant to prevent.
        # ``run_recovery_chain`` sets this True before the first
        # callback fires and clears it in ``finally`` after the last
        # completes, so callback order on the
        # ``AutoReconnectMonitor.recovery_callbacks`` chain no longer
        # affects halt coverage.
        self._reconnect_in_progress: bool = False

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
        publisher.start()
        return publisher

    def get(self, strategy_instance_id: str) -> BrokerActivityPublisher | None:
        return self._by_instance.get(strategy_instance_id)

    async def unregister(self, strategy_instance_id: str) -> None:
        async with self._lock:
            publisher = self._by_instance.pop(strategy_instance_id, None)
        if publisher is not None:
            await publisher.stop()

    async def stop_all(self) -> None:
        """Shutdown hook — stop every running publisher. The registry is
        left empty; the data plane's FastAPI lifespan calls this from
        the shutdown handler."""
        async with self._lock:
            publishers = list(self._by_instance.values())
            self._by_instance.clear()
        for p in publishers:
            await p.stop()

    def instances(self) -> tuple[str, ...]:
        return tuple(self._by_instance.keys())

    # ── reconnect recovery (slice 3 / ADR 0011 amendment) ─────────────

    def any_recovery_active(self) -> bool:
        """True iff the process is mid reconnect-recovery.

        ``place_paper_order`` consults this before forwarding the
        submission to IBKR — a positive answer means the broker
        connection is in the post-reconnect recovery window and a new
        order would race either the bar resubscribe or the executions
        sweep. Pure read, no locking required: callers only need
        eventual consistency between "recovery started" and "next submit
        attempt".

        Two contributors are OR-ed:

        - ``_reconnect_in_progress`` — set by ``run_recovery_chain`` for
          the entire post-reconnect window (covers every callback in the
          chain, including pre-sweep work like bar resubscribe).
        - Any publisher's ``is_reconnect_recovery_active`` — set inside
          ``sweep_reconnect_recovery`` so a sweep invoked outside the
          chain (e.g. tests, manual triggers) still gates submissions.
        """
        if self._reconnect_in_progress:
            return True
        return any(p.is_reconnect_recovery_active for p in self._by_instance.values())

    async def run_recovery_chain(
        self,
        callbacks: list[Callable[[], Awaitable[None]]],
    ) -> None:
        """Run every post-reconnect callback under a process-wide
        submission halt.

        Wraps the ``AutoReconnectMonitor.recovery_callbacks`` chain so
        ``any_recovery_active()`` is True for the *entire* recovery
        window, not just the executions-sweep slice. Without this, a
        callback that runs before the sweep (e.g. the bar aggregator's
        ``resubscribe_all``) would leave submissions enabled and a new
        order placed during the resubscribe could be picked up by the
        subsequent sweep and authored as a ``reconnect_recovery`` row.

        Callback exceptions propagate after the halt is cleared — the
        monitor decides whether to retry the chain. The ``finally``
        guarantees the halt lifts even when a callback raises.
        """
        self._reconnect_in_progress = True
        try:
            for callback in callbacks:
                await callback()
        finally:
            self._reconnect_in_progress = False

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
