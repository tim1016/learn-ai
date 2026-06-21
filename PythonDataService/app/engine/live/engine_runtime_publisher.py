"""PRD #619-B B2 — engine_runtime.json aggregator + serialized publisher.

The publisher is split into three components per the PRD: an in-memory
state aggregator (updated by domain producers), a single serialized
publisher task, and a tiny atomic file writer. The writer lives in
``engine_runtime.py``; this module owns the other two.

Splitting them lets the concurrent-producer race tests exercise the
aggregator under contention without standing up the file-writer side,
and lets the publisher's monotonic-``snapshot_seq`` + immediate-flush
contract be exercised against a fake aggregator. The atomic-write
contract has its own seam in ``test_engine_runtime_writer.py``.

Concurrency model: producers are all asyncio coroutines running on the
same event loop as the publisher. The publisher reads the aggregator
under an ``asyncio.Lock`` so an update-in-flight cannot leave one
block half-applied at the moment ``snapshot()`` is called.

All timestamps are ``int64`` ms UTC. Steady-state cadence default is
1Hz; safety-transition flushes are immediate (the next loop iteration
writes).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    write_engine_runtime_snapshot,
)

logger = logging.getLogger(__name__)


DEFAULT_STEADY_STATE_INTERVAL_S: float = 1.0


class EngineRuntimeAggregator:
    """In-memory state for the four domain blocks + envelope identity.

    Producers update each slot independently. ``snapshot()`` returns a
    coherent ``EngineRuntimeSnapshot`` once *every* block has been
    populated at least once; before that it returns ``None`` so the
    publisher cannot emit a half-formed artifact.

    Thread/asyncio safety: ``asyncio.Lock`` guards every update +
    read. Producers from different asyncio tasks are safe; producers
    from genuine OS threads would need a separate threading lock —
    not in the 619-B contract since both producers (bar loop, command
    poll, broker probe ticks) run on the engine's single event loop.

    Strictly speaking the current producers are single-assignment
    coroutines that never yield between read start and finish, so the
    lock is *defensive* — its job is to keep this invariant if a
    future producer needs to ``await some_io()`` mid-update. Removing
    the lock would be a perf win measured in nanoseconds; keeping it
    is a small future-proofing tax against that subtle bug.
    """

    def __init__(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        pid: int,
        process_start_identity: str,
        expected_daemon_boot_id: str | None,
    ) -> None:
        self._strategy_instance_id = strategy_instance_id
        self._run_id = run_id
        self._pid = pid
        self._process_start_identity = process_start_identity
        self._expected_daemon_boot_id = expected_daemon_boot_id

        self._command_loop: CommandLoopBlock | None = None
        self._broker: BrokerBlock | None = None
        self._bar_loop: BarLoopBlock | None = None
        self._control_plane: ControlPlaneBlock | None = None

        self._lock = asyncio.Lock()

    async def update_command_loop(self, block: CommandLoopBlock) -> None:
        async with self._lock:
            self._command_loop = block

    async def update_broker(self, block: BrokerBlock) -> None:
        async with self._lock:
            self._broker = block

    async def update_bar_loop(self, block: BarLoopBlock) -> None:
        async with self._lock:
            self._bar_loop = block

    async def update_control_plane(self, block: ControlPlaneBlock) -> None:
        async with self._lock:
            self._control_plane = block

    async def snapshot(
        self, *, snapshot_seq: int, written_at_ms: int
    ) -> EngineRuntimeSnapshot | None:
        """Return a coherent snapshot, or ``None`` if any block is missing.

        A read under the same lock as updates means the four blocks
        observed are mutually consistent — no update can land halfway
        through ``snapshot()``.
        """
        async with self._lock:
            if (
                self._command_loop is None
                or self._broker is None
                or self._bar_loop is None
                or self._control_plane is None
            ):
                return None
            return EngineRuntimeSnapshot(
                strategy_instance_id=self._strategy_instance_id,
                run_id=self._run_id,
                pid=self._pid,
                process_start_identity=self._process_start_identity,
                expected_daemon_boot_id=self._expected_daemon_boot_id,
                snapshot_seq=snapshot_seq,
                written_at_ms=written_at_ms,
                command_loop=self._command_loop,
                broker=self._broker,
                bar_loop=self._bar_loop,
                control_plane=self._control_plane,
            )


class EngineRuntimePublisher:
    """Single serialized publisher task.

    Steady-state cadence is 1Hz (configurable for tests via
    ``steady_state_interval_s``). Safety transitions trigger an
    immediate flush via ``request_immediate_flush()`` — the next loop
    iteration writes without waiting for the next cadence tick.

    ``snapshot_seq`` is monotonic across the lifetime of one publisher;
    a fresh publisher starts at ``seq=0`` (the first emitted snapshot
    has ``seq=0``). The backend freshness evaluator uses ``seq`` +
    ``written_at_ms`` to detect torn / out-of-order reads.
    """

    def __init__(
        self,
        aggregator: EngineRuntimeAggregator,
        *,
        run_dir: Path,
        now_ms: Callable[[], int],
        steady_state_interval_s: float = DEFAULT_STEADY_STATE_INTERVAL_S,
        writer: Callable[[Path, EngineRuntimeSnapshot], None] = write_engine_runtime_snapshot,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._aggregator = aggregator
        self._run_dir = run_dir
        self._now_ms = now_ms
        self._steady_state_interval_s = steady_state_interval_s
        self._writer = writer
        self._sleep = sleep_fn or asyncio.sleep

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._seq = 0
        self._last_written_seq: int | None = None
        self._last_written_at_ms: int | None = None

    @property
    def last_written_seq(self) -> int | None:
        """The highest ``snapshot_seq`` actually persisted to disk."""
        return self._last_written_seq

    @property
    def last_written_at_ms(self) -> int | None:
        return self._last_written_at_ms

    def request_immediate_flush(self) -> None:
        """Schedule a write on the next loop iteration.

        Safe to call from any producer — the publisher event-loops
        atomically on the asyncio loop, so this is just a flag set.
        """
        self._wake.set()

    async def start(self) -> None:
        """Spawn the publisher task. Idempotent (no-op if already running)."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self._run(), name="engine_runtime_publisher")

    async def stop(self) -> None:
        """Stop the publisher task and flush one final snapshot if available.

        Bounded shutdown: waits up to ``2 * steady_state_interval_s``
        for the loop to exit cleanly; falls back to cancel if the
        task is still running.
        """
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.wait_for(
                self._task, timeout=max(self._steady_state_interval_s * 2.0, 0.5)
            )
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._task = None

    async def _run(self) -> None:
        """Main publisher loop.

        Each iteration: try to publish (if a coherent snapshot is
        available), then wait for either the steady-state interval to
        elapse OR ``_wake`` to fire (whichever comes first). On
        ``_stop``, perform one final publish attempt and exit.
        """
        while not self._stop.is_set():
            await self._publish_once()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._steady_state_interval_s
                )
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
        # One final flush on shutdown so any post-loop state change is
        # captured. Best-effort — failures are logged, not raised.
        await self._publish_once()

    async def _publish_once(self) -> None:
        seq_candidate = self._seq
        snapshot = await self._aggregator.snapshot(
            snapshot_seq=seq_candidate, written_at_ms=self._now_ms()
        )
        if snapshot is None:
            return
        try:
            self._writer(self._run_dir, snapshot)
        except OSError:
            logger.exception(
                "engine_runtime.json write failed",
                extra={"run_dir": str(self._run_dir), "snapshot_seq": seq_candidate},
            )
            return
        self._seq = seq_candidate + 1
        self._last_written_seq = seq_candidate
        self._last_written_at_ms = snapshot.written_at_ms
