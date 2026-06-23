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
WatchdogState = Literal["HEALTHY", "SUSPECTED_LOSS", "LEASE_LOST_HANDLING", "EXITED"]

# Grace period before a single flapping observation triggers the full halt.
# If the lease recovers within this window the watchdog returns to HEALTHY.
DEFAULT_LEASE_LOSS_GRACE_MS: int = 5_000


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
        lease_loss_grace_ms: int = DEFAULT_LEASE_LOSS_GRACE_MS,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        incident_writer: Callable[..., None] | None = None,
        executor: object | None = None,
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
        self._lease_loss_grace_ms = lease_loss_grace_ms
        self._sleep = sleep_fn or asyncio.sleep
        self._incident_writer = incident_writer or write_lease_lost_incident
        # When provided, _handle_lease_lost delegates to this executor instead
        # of the legacy 5-callback sequence.  The executor is responsible for
        # writing the typed OperatorIncident to the IncidentStore.
        self._executor = executor

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._state: WatchdogState = "HEALTHY"
        # Grace-window tracking: when did we first observe the loss?
        self._suspected_loss_at_ms: int | None = None
        self._suspected_loss_reason: LeaseLossReason | None = None
        self._suspected_observed_daemon_boot_id: str | None = None
        self._suspected_lease_written_at_ms: int | None = None

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

        State machine:
          HEALTHY          → SUSPECTED_LOSS on first bad observation.
          SUSPECTED_LOSS   → HEALTHY if lease recovers within grace window.
          SUSPECTED_LOSS   → LEASE_LOST_HANDLING if grace elapses with loss sustained.
          LEASE_LOST_HANDLING / EXITED → no-op (handler runs at most once).
        """
        if self._state not in ("HEALTHY", "SUSPECTED_LOSS"):
            return
        lease = read_daemon_lease(self._artifacts_root)
        now = self._now_ms()
        await self._update_control_plane_block(lease, now)

        loss_reason = self._detect_loss(lease, now)

        if loss_reason is None:
            # Lease is fresh and boot_id matches (or not configured).
            if self._state == "SUSPECTED_LOSS":
                logger.info(
                    "[WATCHDOG] suspected loss resolved; returning to HEALTHY "
                    "(grace_remaining_ms=%s)",
                    max(
                        0,
                        self._lease_loss_grace_ms
                        - (now - (self._suspected_loss_at_ms or now)),
                    ),
                )
                self._state = "HEALTHY"
                self._suspected_loss_at_ms = None
                self._suspected_loss_reason = None
                self._suspected_observed_daemon_boot_id = None
                self._suspected_lease_written_at_ms = None
            return

        # A loss was observed.
        if self._state == "HEALTHY":
            if self._lease_loss_grace_ms <= 0:
                # No grace window: trigger halt immediately.
                await self._handle_lease_lost(
                    reason=loss_reason[0],
                    observed_at_ms=now,
                    observed_daemon_boot_id=loss_reason[1],
                    lease_written_at_ms=loss_reason[2],
                )
                return
            # Enter grace window.
            self._state = "SUSPECTED_LOSS"
            self._suspected_loss_at_ms = now
            self._suspected_loss_reason = loss_reason[0]
            self._suspected_observed_daemon_boot_id = loss_reason[1]
            self._suspected_lease_written_at_ms = loss_reason[2]
            logger.warning(
                "[WATCHDOG] entering SUSPECTED_LOSS grace window reason=%s grace_ms=%s",
                loss_reason[0],
                self._lease_loss_grace_ms,
            )
            return

        # SUSPECTED_LOSS — check if grace has elapsed.
        assert self._suspected_loss_at_ms is not None
        if (now - self._suspected_loss_at_ms) < self._lease_loss_grace_ms:
            # Still within grace window.
            logger.warning(
                "[WATCHDOG] SUSPECTED_LOSS sustained; elapsed_ms=%s grace_ms=%s",
                now - self._suspected_loss_at_ms,
                self._lease_loss_grace_ms,
            )
            return

        # Grace elapsed with sustained loss → trigger halt.
        await self._handle_lease_lost(
            reason=self._suspected_loss_reason,  # type: ignore[arg-type]
            observed_at_ms=now,
            observed_daemon_boot_id=self._suspected_observed_daemon_boot_id,
            lease_written_at_ms=self._suspected_lease_written_at_ms,
        )

    def _detect_loss(
        self,
        lease: object,
        now: int,
    ) -> tuple[LeaseLossReason, str | None, int | None] | None:
        """Return ``(reason, observed_boot_id, lease_written_at_ms)`` if the lease
        is lost, or ``None`` if it is healthy."""
        if lease is None or (now - lease.written_at_ms) > self._lease_threshold_ms:  # type: ignore[union-attr]
            return (
                "LEASE_EXPIRED",
                getattr(lease, "boot_id", None),
                getattr(lease, "written_at_ms", None),
            )
        if (
            self._expected_daemon_boot_id is not None
            and lease.boot_id != self._expected_daemon_boot_id  # type: ignore[union-attr]
        ):
            return (
                "BOOT_ID_CHANGED",
                lease.boot_id,  # type: ignore[union-attr]
                lease.written_at_ms,  # type: ignore[union-attr]
            )
        return None

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

        When an ``executor`` (``WatchdogHaltExecutor``) is wired in (production
        path), delegates the entire sequence to it so typed ``OperatorIncident``
        records are written with per-step timeouts.  When no executor is
        provided (legacy / test path), falls back to the original 5-callback
        sequence.  The watchdog itself never raises out of this method — the
        engine must exit cleanly even if a step fails.
        """
        self._state = "LEASE_LOST_HANDLING"
        logger.warning(
            "[CONTROL_PLANE_LEASE_LOST] reason=%s observed_boot=%s expected_boot=%s lease_written_at=%s",
            reason,
            observed_daemon_boot_id,
            self._expected_daemon_boot_id,
            lease_written_at_ms,
        )

        if self._executor is not None:
            # Production path: delegate to WatchdogHaltExecutor.
            try:
                await self._executor.execute(reason)  # type: ignore[union-attr]
            except Exception:
                logger.exception("[WATCHDOG] executor.execute failed; state may be inconsistent")
            self._state = "EXITED"
            return

        # Legacy path: the original 5-callback sequence (backward compat for tests
        # that don't wire an executor).

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
