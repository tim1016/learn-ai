"""PRD #619-B B5 — child-side watchdog for daemon lease loss.

The watchdog is a small async task that runs alongside the bar loop +
command poll. On every cadence tick it reads
``<artifacts_root>/control_plane/daemon_lease.json`` and asserts both:

1. The lease is **fresh** — ``now - written_at_ms <= lease_threshold_ms``.
2. The daemon's ``boot_id`` matches the ``expected_daemon_boot_id``
   captured at child spawn (the daemon sets it via the
   ``LIVE_RUNNER_DAEMON_BOOT_ID`` env var).

Either check failing triggers the **lease-lost handler**: an ordered
5-step shutdown the PRD defines as a contract. Order is asserted by
``tests/control_plane/test_child_watchdog.py``:

1. **Block submissions immediately.** No new order can leave the child
   while we're in the handling path.
2. **Persist durable PAUSED + write the incident.** A recovery / next
   start MUST observe PAUSED so reconciliation can pick up cleanly.
3. **Evidence-flush grace** (5–10s default). Active producers (bar
   loop, command poll, engine_runtime publisher) get bounded time to
   flush their state before the broker session is torn down.
4. **Disconnect the broker.** Stops streaming + drops the IBKR socket.
5. **Request bounded engine exit.** The watchdog sets the engine's
   shutdown event; the bar loop observes it on the next iteration
   and the outer ``cmd_start`` finally tears the process down.

Every step is independently callable so tests can assert ordering
without spinning up the real engine. Lease reading reuses the 619-B B4
``read_daemon_lease`` helper; control-plane block updates flow into
the 619-B B2 aggregator so the next publisher tick records the
observation. The watchdog never reads from or writes to the lease
file directly — it consults only the daemon's published state.

PRD authority principle #6: "No adoption, period — for now." The
watchdog only stops the child. Re-spawning, reclamation, or any
form of "the daemon should pick this up later" is out of scope here
and belongs in a separate ADR.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from app.engine.live.control_plane import (
    DEFAULT_LEASE_THRESHOLD_MS,
    read_daemon_lease,
)
from app.engine.live.engine_runtime import ControlPlaneBlock

logger = logging.getLogger(__name__)


DEFAULT_POLL_CADENCE_MS = 1_000
DEFAULT_EVIDENCE_FLUSH_GRACE_MS = 5_000
DEFAULT_EXIT_DEADLINE_MS = 15_000

INCIDENT_FILENAME = "control_plane_lease_lost.json"
INCIDENT_SCHEMA_VERSION = 1

LeaseLossReason = Literal["LEASE_EXPIRED", "BOOT_ID_CHANGED"]
WatchdogState = Literal["HEALTHY", "LEASE_LOST_HANDLING", "EXITED"]


def write_lease_lost_incident(
    run_dir: Path,
    *,
    reason: LeaseLossReason,
    observed_at_ms: int,
    expected_daemon_boot_id: str | None,
    observed_daemon_boot_id: str | None,
    lease_written_at_ms: int | None,
) -> None:
    """Persist the lease-loss incident to ``<run_dir>/control_plane_lease_lost.json``.

    Atomic via tmp + fsync + rename. The reader (next-start
    reconciliation, cockpit incident pane) treats this file as the
    authoritative explanation of why the child exited under
    ``CONTROL_PLANE_LEASE_LOST`` semantics.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / INCIDENT_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "schema_version": INCIDENT_SCHEMA_VERSION,
        "reason": reason,
        "observed_at_ms": observed_at_ms,
        "expected_daemon_boot_id": expected_daemon_boot_id,
        "observed_daemon_boot_id": observed_daemon_boot_id,
        "lease_written_at_ms": lease_written_at_ms,
    }
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


class ChildWatchdog:
    """Polls the daemon lease and runs the ordered lease-loss handler on failure.

    The watchdog is constructed with explicit callbacks so the engine
    can wire it without circular imports and tests can pass fakes that
    record ordering. ``aggregator`` is optional — when present, every
    poll updates ``ControlPlaneBlock`` so the engine_runtime publisher
    has fresh evidence.

    Lifecycle mirrors ``EngineRuntimePublisher`` / ``DaemonLeaseWriter``:
    idempotent ``start()``, bounded ``stop()``, exception-tolerant
    inner loop.
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        run_dir: Path,
        expected_daemon_boot_id: str | None,
        block_submissions: Callable[[], None],
        persist_paused: Callable[[LeaseLossReason], None],
        disconnect_broker: Callable[[], Awaitable[None]],
        request_engine_exit: Callable[[], None],
        now_ms: Callable[[], int],
        aggregator: object | None = None,
        poll_cadence_ms: int = DEFAULT_POLL_CADENCE_MS,
        lease_threshold_ms: int = DEFAULT_LEASE_THRESHOLD_MS,
        evidence_flush_grace_ms: int = DEFAULT_EVIDENCE_FLUSH_GRACE_MS,
        exit_deadline_ms: int = DEFAULT_EXIT_DEADLINE_MS,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        incident_writer: Callable[..., None] | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._run_dir = run_dir
        self._expected_daemon_boot_id = expected_daemon_boot_id
        self._block_submissions = block_submissions
        self._persist_paused = persist_paused
        self._disconnect_broker = disconnect_broker
        self._request_engine_exit = request_engine_exit
        self._now_ms = now_ms
        self._aggregator = aggregator
        self._poll_cadence_ms = poll_cadence_ms
        self._lease_threshold_ms = lease_threshold_ms
        self._evidence_flush_grace_ms = evidence_flush_grace_ms
        self._exit_deadline_ms = exit_deadline_ms
        self._sleep = sleep_fn or asyncio.sleep
        self._incident_writer = incident_writer or write_lease_lost_incident

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._state: WatchdogState = "HEALTHY"

    @property
    def state(self) -> WatchdogState:
        return self._state

    async def start(self) -> None:
        """Spawn the watchdog task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="child_watchdog")

    async def stop(self) -> None:
        """Cancel + drain. Bounded by ``2 * poll_cadence``."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(
                self._task, timeout=max(self._poll_cadence_ms * 2.0 / 1000.0, 0.5)
            )
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        finally:
            self._task = None

    async def _run(self) -> None:
        cadence_s = self._poll_cadence_ms / 1000.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=cadence_s)
                return
            except TimeoutError:
                pass
            try:
                await self.poll_once()
            except Exception:
                logger.exception("child watchdog poll failed")

    async def poll_once(self) -> None:
        """One poll iteration — read lease, update control_plane, react.

        Exposed publicly so tests can drive the watchdog one step at a
        time without spinning the cadence task.
        """
        if self._state != "HEALTHY":
            return
        lease = read_daemon_lease(self._artifacts_root)
        now = self._now_ms()
        await self._update_control_plane_block(lease, now)
        if lease is None or (now - lease.written_at_ms) > self._lease_threshold_ms:
            await self._handle_lease_lost(
                reason="LEASE_EXPIRED",
                observed_at_ms=now,
                observed_daemon_boot_id=lease.boot_id if lease is not None else None,
                lease_written_at_ms=lease.written_at_ms if lease is not None else None,
            )
            return
        if (
            self._expected_daemon_boot_id is not None
            and lease.boot_id != self._expected_daemon_boot_id
        ):
            await self._handle_lease_lost(
                reason="BOOT_ID_CHANGED",
                observed_at_ms=now,
                observed_daemon_boot_id=lease.boot_id,
                lease_written_at_ms=lease.written_at_ms,
            )

    async def _update_control_plane_block(
        self, lease: object, now_ms: int
    ) -> None:
        """Publish the latest lease observation onto the runtime aggregator.

        Runs on every poll iteration regardless of lease state — a
        missing lease still emits a block with ``observed_daemon_boot_id=None``
        so the backend freshness evaluator can act on the absence.
        """
        if self._aggregator is None:
            return
        observed_boot_id = getattr(lease, "boot_id", None)
        block = ControlPlaneBlock(
            lease_observed_at_ms=now_ms,
            observed_daemon_boot_id=observed_boot_id,
        )
        await self._aggregator.update_control_plane(block)

    async def _handle_lease_lost(
        self,
        *,
        reason: LeaseLossReason,
        observed_at_ms: int,
        observed_daemon_boot_id: str | None,
        lease_written_at_ms: int | None,
    ) -> None:
        """The PRD-defined 5-step ordered shutdown.

        Order is the contract; tests assert it. Each step is bounded
        and side-effecting — the watchdog itself never raises out of
        this method (the engine must exit cleanly even if a step
        fails).
        """
        self._state = "LEASE_LOST_HANDLING"
        logger.warning(
            "[CONTROL_PLANE_LEASE_LOST] reason=%s observed_boot=%s expected_boot=%s lease_written_at=%s",
            reason,
            observed_daemon_boot_id,
            self._expected_daemon_boot_id,
            lease_written_at_ms,
        )

        # Step 1 — block submissions IMMEDIATELY.
        try:
            self._block_submissions()
        except Exception:
            logger.exception("step 1 (block_submissions) failed")

        # Step 2 — durable PAUSED + incident file.
        try:
            self._persist_paused(reason)
        except Exception:
            logger.exception("step 2a (persist_paused) failed")
        try:
            self._incident_writer(
                self._run_dir,
                reason=reason,
                observed_at_ms=observed_at_ms,
                expected_daemon_boot_id=self._expected_daemon_boot_id,
                observed_daemon_boot_id=observed_daemon_boot_id,
                lease_written_at_ms=lease_written_at_ms,
            )
        except Exception:
            logger.exception("step 2b (write_lease_lost_incident) failed")

        # Step 3 — evidence-flush grace.
        try:
            await self._sleep(self._evidence_flush_grace_ms / 1000.0)
        except Exception:
            logger.exception("step 3 (evidence-flush grace) failed")

        # Step 4 — broker disconnect.
        try:
            await self._disconnect_broker()
        except Exception:
            logger.exception("step 4 (disconnect_broker) failed")

        # Step 5 — request bounded engine exit.
        try:
            self._request_engine_exit()
        except Exception:
            logger.exception("step 5 (request_engine_exit) failed")

        self._state = "EXITED"
