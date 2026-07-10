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
ProducerStartHook = Callable[[], Awaitable[None]]

_PROCESS_EPOCH = uuid4().hex
_TRANSPORT_ONLY_FIELDS = frozenset(
    {
        "stream_epoch",
        "surface_version",
        "fetched_at_ms",
        "generated_at_ms",
        "age_ms",
        "age_ms_at_generation",
        "now_ms",
    }
)


def _semantic_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _semantic_value(item)
            for key, item in value.items()
            if key not in _TRANSPORT_ONLY_FIELDS
        }
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    return value


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
        on_start: ProducerStartHook | None = None,
        refresh_interval_seconds: float = 1.0,
        process_epoch: str = _PROCESS_EPOCH,
    ) -> None:
        self.strategy_instance_id = strategy_instance_id
        self._process_epoch = process_epoch
        self.stream_epoch = self._new_stream_epoch()
        self._assemble = assemble
        self._on_start = on_start
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

    @property
    def latest(self) -> SnapshotT | None:
        return self._latest

    @property
    def surface_version(self) -> int:
        return self._surface_version

    @property
    def is_running(self) -> bool:
        return self._producer_task is not None and not self._producer_task.done()

    async def start(self, *, invoke_start_hook: bool = True) -> None:
        """Start one producer lifecycle and assemble its initial snapshot."""

        async with self._lifecycle_guard:
            if self._producer_task is not None and not self._producer_task.done():
                return
            if self._producer_started_once:
                self.stream_epoch = self._new_stream_epoch()
                self._surface_version = 0
                self._fingerprint = None
                self._latest = None
            self._producer_started_once = True
            self._stop_event = asyncio.Event()
            if invoke_start_hook and self._on_start is not None:
                await self._on_start()
            await self.refresh()
            self._producer_task = asyncio.create_task(
                self._producer_loop(),
                name=f"surface-hub:{self.strategy_instance_id}",
            )

    async def snapshot(self, *, refresh: bool = False) -> SnapshotT:
        """Return the stored snapshot, optionally after one coalesced cycle."""

        if refresh or self._latest is None:
            return await self.refresh()
        return self._latest

    async def refresh(self) -> SnapshotT:
        """Coalesce concurrent callers onto one assembly task."""

        async with self._refresh_guard:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._assemble_and_store(),
                    name=f"surface-hub-refresh:{self.strategy_instance_id}",
                )
            task = self._refresh_task
        return await asyncio.shield(task)

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        """Stop the producer loop within the data-plane shutdown budget."""

        async with self._lifecycle_guard:
            task = self._producer_task
            if task is None:
                return
            self._stop_event.set()
            try:
                await asyncio.wait_for(task, timeout=timeout_seconds)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                logger.error(
                    "surface hub did not stop within shutdown budget",
                    extra={"strategy_instance_id": self.strategy_instance_id},
                )
            finally:
                self._producer_task = None

    def _new_stream_epoch(self) -> str:
        return f"{self._process_epoch}:{uuid4().hex}"

    async def _assemble_and_store(self) -> SnapshotT:
        candidate = await self._assemble()
        fingerprint = semantic_surface_fingerprint(candidate)
        if fingerprint != self._fingerprint:
            self._surface_version += 1
            self._fingerprint = fingerprint
        versioned = candidate.model_copy(
            update={
                "stream_epoch": self.stream_epoch,
                "surface_version": self._surface_version,
            }
        )
        self._latest = versioned
        return versioned

    async def _producer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._refresh_interval_seconds,
                )
            except TimeoutError:
                try:
                    await self.refresh()
                except Exception:
                    logger.exception(
                        "surface hub refresh failed",
                        extra={"strategy_instance_id": self.strategy_instance_id},
                    )


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
        on_start: ProducerStartHook | None = None,
        refresh_interval_seconds: float = 1.0,
    ) -> SurfaceHub[SnapshotT]:
        hub = self._hubs.get(strategy_instance_id)
        if hub is None:
            hub = SurfaceHub(
                strategy_instance_id=strategy_instance_id,
                assemble=assemble,
                on_start=on_start,
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


__all__ = [
    "SurfaceHub",
    "SurfaceHubRegistry",
    "semantic_surface_fingerprint",
]
