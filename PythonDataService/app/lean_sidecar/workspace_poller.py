"""In-flight workspace-cap enforcement for the LEAN sidecar launcher.

This module owns the **background poller thread** that watches a LEAN
run's workspace size while the container is alive, and asks
``runner._kill_container_via_cidfile`` to stop the container if usage
exceeds ``workspace_max_mb``. The post-execute size check in
``launcher/service.py`` remains as a backstop for the race-path case
where a write lands as ``execute()`` is returning.

Threat model — load-bearing. READ THIS BEFORE CHANGING THE POLLER.
====================================================================

This cap defends against **benign overrun** — a buggy ``QCAlgorithm``
filling its own disk — NOT **adversarial input**.

The app has no caller authentication today. Normally that would argue
for a kernel-enforced cap (the kernel is the only thing the algorithm
can't lie to). It doesn't, because the data plane is a single-host
research tool — network surface is ``host.containers.internal`` and
localhost — and the realistic failure mode is "I wrote a buggy
QCAlgorithm that fills my own disk", not "a remote actor exfiltrates
compute resources."

If auth is added later and ``algorithm_source_kind=user_provided``
starts arriving from a genuinely untrusted caller, **escalate this
cap to kernel-enforced** (``--storage-opt size=<mb>m`` on
``podman run``; podman storage driver is already ``overlay``).
The post-execute backstop is **not sufficient against an adversary**
— they can wedge the host before the check fires. The poller alone
is theater under that threat model.

Authority: docs/handoffs/2026-05-18-design-p1-4-live-workspace-cap-v2.md.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from app.lean_sidecar.config import _WORKSPACE_POLL_INTERVAL_S

logger = logging.getLogger(__name__)


def workspace_size_bytes_scandir(root: Path) -> int:
    """Sum on-disk file sizes under ``root`` via ``os.scandir``.

    Why scandir, not ``Path.rglob``: under a hot LEAN run the
    workspace sees thousands of small writes/sec to ``cache/`` and
    intermediate ``result.json`` fragments. ``Path.rglob`` instantiates
    a ``Path`` per entry, making the poller itself an I/O contention
    source. ``os.scandir`` is C-backed (``stat`` lives in the same
    syscall as the directory enumeration) and is the lower-overhead
    primitive Python exposes.

    Symlinks are not followed — we count on-disk usage of files inside
    the workspace, not link-target sizes that may live outside it.

    Race-tolerant: if a file vanishes between the ``scandir`` entry
    and its ``stat`` call (LEAN was still finishing up), we treat the
    file as zero. The next walk catches up.

    Returns 0 if ``root`` does not exist or cannot be read. The
    launcher pre-creates the workspace before invoking the runner,
    so missing-root only happens in test paths.
    """
    total = 0
    stack: list[Path] = [Path(root)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        try:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    # File vanished mid-walk (LEAN's still-shutting-down
                    # race) or stat failed; ignore and move on.
                    continue
        finally:
            it.close()
    return total


class WorkspacePoller:
    """Background thread that enforces ``workspace_max_mb`` mid-run.

    Lifecycle:

    1. Construct with the cidfile path, workspace dir, byte cap, and
       (optional, tests-only) poll interval.
    2. Call :meth:`start` *before* invoking ``runner.execute()``.
    3. Call :meth:`stop` after ``execute()`` returns (or in a
       ``finally`` so a launcher crash doesn't leak the thread).
    4. After ``stop()``, check :attr:`fired` to see whether the poller
       killed the container — if True, the launcher returns
       ``LaunchRejectedError("workspace_max_mb_exceeded", ...)`` rather
       than a normal response.

    The interval defaults to :data:`config._WORKSPACE_POLL_INTERVAL_S`
    (1.0s) and IS the overshoot budget — see the module docstring.
    Tests override it to keep wall-clock test time bounded; production
    code never should.
    """

    def __init__(
        self,
        *,
        cidfile_path: Path,
        workspace_dir: Path,
        max_bytes: int,
        interval_s: float | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"WorkspacePoller.max_bytes must be positive, got {max_bytes!r}")
        self._cidfile_path = cidfile_path
        self._workspace_dir = workspace_dir
        self._max_bytes = max_bytes
        # Resolve interval at __init__ so tests that monkeypatch the
        # module constant after a poller is constructed don't change
        # the poller's behaviour mid-flight. The integration test
        # monkeypatches BEFORE start() to exercise short intervals.
        self._interval_s = interval_s if interval_s is not None else _WORKSPACE_POLL_INTERVAL_S
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired = False

    @property
    def fired(self) -> bool:
        """True iff the poller observed an overrun and called the kill helper."""
        return self._fired

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("WorkspacePoller.start called twice")
        self._thread = threading.Thread(target=self._run, daemon=True, name="lean-workspace-poller")
        self._thread.start()

    def stop(self) -> None:
        """Signal the poller to exit and join its thread.

        Idempotent: safe to call multiple times. Prompt: returns
        without waiting for a full ``interval_s`` tick because the
        wait is on a ``threading.Event``, which wakes immediately on
        ``set()``.
        """
        self._stop_event.set()
        if self._thread is not None:
            # Join generously — the kill helper inside _run can take up
            # to ~30s on a wedged podman. Without an upper bound the
            # launcher's main thread blocks indefinitely.
            self._thread.join(timeout=60.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                size = workspace_size_bytes_scandir(self._workspace_dir)
            except OSError as e:
                # Defensive — workspace_size_bytes_scandir swallows
                # OSError internally, but log the surprise and keep
                # going. A wedged FS shouldn't take down the poller.
                logger.warning(
                    "workspace_size walk failed for %s: %s",
                    self._workspace_dir,
                    e,
                )
                size = 0
            if size > self._max_bytes:
                self._fired = True
                logger.info(
                    "workspace cap exceeded: %d bytes > %d bytes; killing container",
                    size,
                    self._max_bytes,
                )
                # Local import to break the runner -> workspace_poller
                # cycle (runner.py exports KillReason which we'd want
                # at module level). The cycle would be load-order
                # only — both modules import each other — but local
                # import keeps this file independent of runner's
                # initialization order.
                from app.lean_sidecar.runner import (
                    KillReason,
                    _kill_container_via_cidfile,
                )

                _kill_container_via_cidfile(
                    self._cidfile_path,
                    reason=KillReason.WORKSPACE_MAX_MB_EXCEEDED,
                )
                return
            self._stop_event.wait(self._interval_s)
