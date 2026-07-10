"""Per-bot ownership of versioned Bot Cockpit state snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel

logger = logging.getLogger(__name__)

SnapshotT = TypeVar("SnapshotT", bound=BaseModel)
SnapshotAssembler = Callable[[], Awaitable[SnapshotT]]
SnapshotObserver = Callable[[SnapshotT], Awaitable[None]]

_PROCESS_EPOCH = uuid4().hex
_TRANSPORT_ONLY_PATHS = frozenset(
    {
        ("stream_epoch",),
        ("surface_version",),
        ("fetched_at_ms",),
        ("daemon_fetched_at_ms",),
        ("readiness", "as_of_ms"),
        ("operator_surface", "trading_session", "as_of_ms"),
        (
            "operator_surface",
            "broker_observation_consistency",
            "compared_at_ms",
        ),
    }
)
_DERIVED_AGE_FIELDS = frozenset({"age_ms", "age_ms_at_generation", "now_ms"})
_EVALUATION_RECEIPT_LABELS = frozenset(
    {
        "account_identity.verdict",
        "readiness.desired_state",
        "readiness_gate.next_step",
        "readiness_gate.status",
        "trading_session.phase",
    }
)
_CLIENT_QUEUE_MAXSIZE = 1


class SnapshotUnavailableError(RuntimeError):
    """The producer has not completed a successful assembly yet."""


class SurfaceHubResourceLimits(BaseModel):
    """Bounded resources owned by one SurfaceHub producer."""

    producer_task_limit: int = 1
    refresh_task_limit: int = 1
    client_queue_maxsize: int = 1
    watcher_count: int = 0


def _semantic_value(value: object, *, path: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        return {
            key: _semantic_value(item, path=(*path, key))
            for key, item in value.items()
            if not _is_transport_only_entry(value, path=path, key=key)
        }
    if isinstance(value, list):
        return [_semantic_value(item, path=path) for item in value]
    return value


def _is_transport_only_entry(
    container: dict,
    *,
    path: tuple[str, ...],
    key: str,
) -> bool:
    full_path = (*path, key)
    if full_path in _TRANSPORT_ONLY_PATHS or key in _DERIVED_AGE_FIELDS:
        return True
    if key == "evidence_at_ms" and ("gate_result" in path or "gate_results" in path):
        return True
    return key == "ts_ms" and container.get("label") in _EVALUATION_RECEIPT_LABELS


def semantic_surface_fingerprint(snapshot: BaseModel) -> str:
    """Hash the operator document while excluding transport-only motion."""

    semantic = _semantic_value(snapshot.model_dump(mode="json"))
    encoded = json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SurfaceHub(Generic[SnapshotT]):  # noqa: UP046 - Python 3.11 runtime; PEP 695 needs 3.12.
    """Own one bot's assembly cadence, latest snapshot, epoch, and version."""

    def __init__(
        self,
        *,
        strategy_instance_id: str,
        assemble: SnapshotAssembler[SnapshotT],
        on_snapshot: SnapshotObserver[SnapshotT] | None = None,
        refresh_interval_seconds: float = 1.0,
        process_epoch: str = _PROCESS_EPOCH,
    ) -> None:
        self.strategy_instance_id = strategy_instance_id
        self._process_epoch = process_epoch
        self.stream_epoch = self._new_stream_epoch()
        self._assemble = assemble
        self._on_snapshot = on_snapshot
        self._refresh_interval_seconds = refresh_interval_seconds
        self._latest: SnapshotT | None = None
        self._surface_version = 0
        self._fingerprint: str | None = None
        self._refresh_guard = asyncio.Lock()
        self._refresh_task: asyncio.Task[SnapshotT] | None = None
        self._lifecycle_guard = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._producer_task: asyncio.Task[None] | None = None
        self._producer_started_once = False
        self._generation = 0
        self._initial_cycle_done = asyncio.Event()
        self._client_queue_maxsize = _CLIENT_QUEUE_MAXSIZE
        self._watchers: set[asyncio.Queue[SnapshotT | None]] = set()

    @property
    def latest(self) -> SnapshotT | None:
        return self._latest

    @property
    def surface_version(self) -> int:
        return self._surface_version

    @property
    def is_running(self) -> bool:
        return self._producer_task is not None and not self._producer_task.done()

    @property
    def resource_limits(self) -> SurfaceHubResourceLimits:
        return SurfaceHubResourceLimits(
            client_queue_maxsize=self._client_queue_maxsize,
            watcher_count=len(self._watchers),
        )

    async def start(self) -> None:
        """Start one producer lifecycle, even when initial assembly fails.

        The producer owns retries. Startup waits only for the first attempt so
        callers can observe a ready snapshot when the sources are available;
        an unavailable source never prevents the producer task from existing.
        """

        async with self._lifecycle_guard:
            if self._producer_task is not None and not self._producer_task.done():
                return
            if self._producer_started_once:
                self.stream_epoch = self._new_stream_epoch()
                self._surface_version = 0
                self._fingerprint = None
                self._latest = None
            self._producer_started_once = True
            self._generation += 1
            self._stop_event = asyncio.Event()
            self._initial_cycle_done = asyncio.Event()
            self._producer_task = asyncio.create_task(
                self._producer_loop(self._generation),
                name=f"surface-hub:{self.strategy_instance_id}",
            )
            initial_cycle_done = self._initial_cycle_done
        await initial_cycle_done.wait()

    async def snapshot(self, *, refresh: bool = False) -> SnapshotT:
        """Return the stored snapshot, optionally after one coalesced cycle."""

        if refresh:
            return await self.refresh()
        if self._latest is None:
            raise SnapshotUnavailableError(self.strategy_instance_id)
        return self._latest

    async def refresh(self) -> SnapshotT:
        """Coalesce concurrent callers onto one assembly task."""

        async with self._refresh_guard:
            if self._refresh_task is None or self._refresh_task.done():
                generation = self._generation
                epoch = self.stream_epoch
                self._refresh_task = asyncio.create_task(
                    self._assemble_and_store(generation=generation, epoch=epoch),
                    name=f"surface-hub-refresh:{self.strategy_instance_id}",
                )
            task = self._refresh_task
        return await asyncio.shield(task)

    def subscribe(self) -> asyncio.Queue[SnapshotT | None]:
        """Subscribe to latest-wins snapshots with a bounded queue of one."""

        queue: asyncio.Queue[SnapshotT | None] = asyncio.Queue(
            maxsize=self._client_queue_maxsize
        )
        if self._stop_event.is_set():
            queue.put_nowait(None)
            return queue
        if self._latest is not None:
            queue.put_nowait(self._latest)
        self._watchers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SnapshotT | None]) -> None:
        self._watchers.discard(queue)

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        """Stop the producer loop within the data-plane shutdown budget."""

        async with self._lifecycle_guard:
            producer_task = self._producer_task
            refresh_task = self._refresh_task
            self._stop_event.set()
            self._initial_cycle_done.set()
            self._generation += 1
            pending = [task for task in (producer_task, refresh_task) if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    logger.error(
                        "surface hub did not stop within shutdown budget",
                        extra={"strategy_instance_id": self.strategy_instance_id},
                    )
            self._producer_task = None
            self._refresh_task = None
            self._close_watchers()

    def _new_stream_epoch(self) -> str:
        return f"{self._process_epoch}:{uuid4().hex}"

    async def _assemble_and_store(self, *, generation: int, epoch: str) -> SnapshotT:
        candidate = await self._assemble()
        if generation != self._generation or epoch != self.stream_epoch:
            raise asyncio.CancelledError
        fingerprint = semantic_surface_fingerprint(candidate)
        semantic_changed = fingerprint != self._fingerprint
        if semantic_changed:
            self._surface_version += 1
            self._fingerprint = fingerprint
        versioned = candidate.model_copy(
            update={
                "stream_epoch": self.stream_epoch,
                "surface_version": self._surface_version,
            }
        )
        self._latest = versioned
        if semantic_changed:
            self._publish_to_watchers(versioned)
        return versioned

    def _publish_to_watchers(self, snapshot: SnapshotT) -> None:
        for queue in tuple(self._watchers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(snapshot)

    def _close_watchers(self) -> None:
        for queue in tuple(self._watchers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        self._watchers.clear()

    async def _producer_loop(self, generation: int) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = await self.refresh()
                if self._on_snapshot is not None:
                    await self._on_snapshot(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "surface hub refresh failed",
                    extra={"strategy_instance_id": self.strategy_instance_id},
                )
            finally:
                if generation == self._generation:
                    self._initial_cycle_done.set()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._refresh_interval_seconds,
                )
            except TimeoutError:
                continue


class SurfaceHubRegistry(Generic[SnapshotT]):  # noqa: UP046 - Python 3.11 runtime; PEP 695 needs 3.12.
    """Process-local registry for independently owned per-bot hubs."""

    def __init__(self) -> None:
        self._hubs: dict[str, SurfaceHub[SnapshotT]] = {}

    def get(self, strategy_instance_id: str) -> SurfaceHub[SnapshotT] | None:
        return self._hubs.get(strategy_instance_id)

    def get_or_create(
        self,
        strategy_instance_id: str,
        *,
        assemble: SnapshotAssembler[SnapshotT],
        on_snapshot: SnapshotObserver[SnapshotT] | None = None,
        refresh_interval_seconds: float = 1.0,
    ) -> SurfaceHub[SnapshotT]:
        hub = self._hubs.get(strategy_instance_id)
        if hub is None:
            hub = SurfaceHub(
                strategy_instance_id=strategy_instance_id,
                assemble=assemble,
                on_snapshot=on_snapshot,
                refresh_interval_seconds=refresh_interval_seconds,
            )
            self._hubs[strategy_instance_id] = hub
        return hub

    async def start_all(self, hubs: Iterable[SurfaceHub[SnapshotT]]) -> None:
        owned_hubs = tuple(hubs)
        results = await asyncio.gather(
            *(hub.start() for hub in owned_hubs),
            return_exceptions=True,
        )
        for hub, result in zip(owned_hubs, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "surface hub failed to start",
                    extra={
                        "strategy_instance_id": hub.strategy_instance_id,
                        "exception": repr(result),
                    },
                )

    async def stop_all(self) -> None:
        hubs = tuple(self._hubs.values())
        await asyncio.gather(*(hub.stop() for hub in hubs))
        self._hubs.clear()

    async def remove(self, strategy_instance_id: str) -> None:
        """Remove one hub and drain every task it owns."""

        hub = self._hubs.pop(strategy_instance_id, None)
        if hub is not None:
            await hub.stop()


__all__ = [
    "SnapshotUnavailableError",
    "SurfaceHub",
    "SurfaceHubRegistry",
    "SurfaceHubResourceLimits",
    "semantic_surface_fingerprint",
]
