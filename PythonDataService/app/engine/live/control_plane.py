"""PRD #619-B B4 — daemon control-plane lease.

The host daemon (``host_daemon.py``) is now the explicit owner of the
live-runner control plane. Each daemon process generates a
``boot_id`` at startup and renews a lease file at
``artifacts/control_plane/daemon_lease.json`` on a 1Hz cadence. The
child watchdog (619-B B5) reads the lease to detect daemon restart
(``boot_id`` change) and daemon death (lease age > threshold).

Module split:

- **This file** — the lease wire schema, the atomic writer/reader,
  and the ``DaemonLeaseWriter`` async task that owns periodic
  renewal. Daemon integration (calling ``writer.start()`` at app
  startup, switching status to ``DRAINING`` on shutdown signal) is
  in ``host_daemon.py``.
- **``engine_runtime_publisher.py``** — separate per-run artifact;
  the lease is daemon-wide, the runtime snapshot is per-run.

The lease semantics (PRD §B):

- Cadence: 1Hz writes by default.
- Threshold: 5s. If a reader sees ``now - written_at_ms > 5_000`` the
  lease is **expired** and the child watchdog fail-closes.
- Status ``CONNECTED`` is the steady-state. ``DRAINING`` signals that
  the daemon is in graceful shutdown — children pause + flush evidence
  + disconnect + exit without flattening.

All timestamps are ``int64`` ms UTC at the artifact boundary.
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

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifact_io import read_pydantic_artifact

logger = logging.getLogger(__name__)


DAEMON_LEASE_FILENAME = "daemon_lease.json"
CONTROL_PLANE_DIRNAME = "control_plane"

DEFAULT_LEASE_CADENCE_MS = 1_000
DEFAULT_LEASE_THRESHOLD_MS = 5_000


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class DaemonLease(BaseModel):
    """The daemon's heartbeat record.

    A reader treats the lease as fresh iff
    ``now_ms - written_at_ms <= lease_threshold_ms``. ``boot_id`` is
    immutable for the lifetime of one daemon process; a value change
    means the daemon restarted under any running child.

    The wire shape is pinned in 619-B and intentionally minimal —
    additions later require a ``schema_version`` bump.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    boot_id: str
    written_at_ms: int = Field(ge=0)
    status: Literal["CONNECTED", "DRAINING"] = "CONNECTED"
    lease_cadence_ms: int = Field(default=DEFAULT_LEASE_CADENCE_MS, ge=1)
    lease_threshold_ms: int = Field(default=DEFAULT_LEASE_THRESHOLD_MS, ge=1)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def daemon_lease_path(artifacts_root: Path) -> Path:
    """``<artifacts_root>/control_plane/daemon_lease.json``.

    The directory is created on first write; readers must tolerate its
    absence.
    """
    return artifacts_root / CONTROL_PLANE_DIRNAME / DAEMON_LEASE_FILENAME


# ---------------------------------------------------------------------------
# Atomic writer + reader.
# ---------------------------------------------------------------------------


def write_daemon_lease(artifacts_root: Path, lease: DaemonLease) -> None:
    """Atomic write of the daemon lease — tmp + fsync + replace.

    Creates the ``control_plane/`` parent dir on first call. A partial
    read by the watchdog cannot observe a torn file.
    """
    path = daemon_lease_path(artifacts_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(lease.model_dump(mode="json"), separators=(",", ":"), sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def read_daemon_lease(artifacts_root: Path) -> DaemonLease | None:
    """Read the current lease, returning ``None`` on missing/malformed.

    Delegates the four fail-closed guards (missing / unreadable /
    malformed / forward-incompatible ``schema_version``) to the
    canonical ``read_pydantic_artifact`` helper. The watchdog reads
    ``None`` as ``UNREACHABLE`` and fail-closes.
    """
    return read_pydantic_artifact(daemon_lease_path(artifacts_root), DaemonLease)


def lease_is_fresh(
    lease: DaemonLease | None, *, now_ms: int, max_age_ms: int | None = None
) -> bool:
    """True iff ``lease`` exists and is within ``lease_threshold_ms``.

    ``max_age_ms`` overrides the lease's own threshold (test seam).
    """
    if lease is None:
        return False
    threshold = max_age_ms if max_age_ms is not None else lease.lease_threshold_ms
    return (now_ms - lease.written_at_ms) <= threshold


# ---------------------------------------------------------------------------
# DaemonLeaseWriter — async task that renews the lease on cadence.
# ---------------------------------------------------------------------------


class DaemonLeaseWriter:
    """Renew the daemon lease on a 1Hz cadence.

    Mirrors the ``AutoReconnectMonitor`` lifecycle shape: idempotent
    ``start()`` / bounded ``stop()`` / exception-tolerant inner loop.
    The daemon's ``/health`` endpoint reads ``current_status`` + the
    last write timestamp for diagnostics.

    Switching from ``CONNECTED`` → ``DRAINING`` causes an immediate
    flush (the watchdog observes the state change without waiting for
    the next cadence tick).
    """

    def __init__(
        self,
        *,
        artifacts_root: Path,
        boot_id: str,
        now_ms: Callable[[], int],
        cadence_ms: int = DEFAULT_LEASE_CADENCE_MS,
        threshold_ms: int = DEFAULT_LEASE_THRESHOLD_MS,
        writer: Callable[[Path, DaemonLease], None] = write_daemon_lease,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._boot_id = boot_id
        self._now_ms = now_ms
        self._cadence_ms = cadence_ms
        self._threshold_ms = threshold_ms
        self._writer = writer
        self._sleep = sleep_fn or asyncio.sleep

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._status: Literal["CONNECTED", "DRAINING"] = "CONNECTED"
        self._last_written_at_ms: int | None = None

    @property
    def boot_id(self) -> str:
        return self._boot_id

    @property
    def status(self) -> Literal["CONNECTED", "DRAINING"]:
        return self._status

    @property
    def last_written_at_ms(self) -> int | None:
        return self._last_written_at_ms

    def set_draining(self) -> None:
        """Switch the lease to ``DRAINING`` and schedule an immediate flush.

        Idempotent. Safe to call from a signal handler or shutdown
        hook on the asyncio loop.
        """
        if self._status == "DRAINING":
            return
        self._status = "DRAINING"
        self._wake.set()

    async def start(self) -> None:
        """Start renewing the lease. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._wake.clear()
        # Write once up-front so a freshly-started daemon is observably
        # connected before the first cadence tick fires.
        self._write_now()
        self._task = asyncio.create_task(self._run(), name="daemon_lease_writer")

    def renew_now(self) -> None:
        """Write one fresh lease immediately.

        Used by the cockpit recovery action when a child reports a stale
        control-plane lease but the daemon HTTP API is still reachable.
        The writer task remains the owner of periodic renewal; this method
        is just a synchronous nudge through the same atomic writer path.
        """
        self._write_now(raise_on_error=True)
        self._wake_threadsafe()

    async def stop(self) -> None:
        """Cancel + drain. Bounded by ``2 * cadence`` seconds."""
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.wait_for(
                self._task, timeout=max(self._cadence_ms * 2.0 / 1000.0, 0.5)
            )
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        finally:
            self._task = None
            self._loop = None

    async def _run(self) -> None:
        cadence_s = self._cadence_ms / 1000.0
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=cadence_s)
                self._wake.clear()
            except TimeoutError:
                pass
            self._write_now()

    def _wake_threadsafe(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._wake.set)
            return
        self._wake.set()

    def _write_now(self, *, raise_on_error: bool = False) -> None:
        lease = DaemonLease(
            boot_id=self._boot_id,
            written_at_ms=self._now_ms(),
            status=self._status,
            lease_cadence_ms=self._cadence_ms,
            lease_threshold_ms=self._threshold_ms,
        )
        try:
            self._writer(self._artifacts_root, lease)
        except OSError:
            logger.exception(
                "daemon_lease.json write failed",
                extra={
                    "boot_id": self._boot_id,
                    "status": self._status,
                    "artifacts_root": str(self._artifacts_root),
                },
            )
            if raise_on_error:
                raise
            return
        self._last_written_at_ms = lease.written_at_ms
