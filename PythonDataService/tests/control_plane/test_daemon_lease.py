"""PRD #619-B B4 — daemon lease writer/reader + freshness contract.

Asserts:

- The lease writer round-trips through ``model_dump`` /
  ``model_validate_json`` cleanly.
- The atomic writer creates the ``control_plane/`` parent dir on
  first write and leaves no ``.tmp`` debris.
- ``lease_is_fresh`` honours the per-lease threshold and the override.
- Forward-incompatible ``schema_version`` reads as ``None``.
- ``DaemonLeaseWriter.start()`` writes the initial lease synchronously
  so a freshly-started daemon is observable before the first cadence
  tick fires.
- Steady-state cadence emits one renewal per interval.
- ``set_draining()`` causes an immediate flush — the watchdog sees
  the state change without waiting for the next cadence tick.
- ``stop()`` is bounded and exception-safe.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.engine.live.control_plane import (
    DAEMON_LEASE_FILENAME,
    DEFAULT_LEASE_CADENCE_MS,
    DEFAULT_LEASE_THRESHOLD_MS,
    DaemonLease,
    DaemonLeaseWriter,
    daemon_lease_path,
    lease_is_fresh,
    read_daemon_lease,
    write_daemon_lease,
)
from tests._fixtures.fake_clock import make_test_clock

# ---------------------------------------------------------------------------
# Schema + writer/reader
# ---------------------------------------------------------------------------


def test_lease_path_resolves_under_control_plane(tmp_path: Path) -> None:
    assert daemon_lease_path(tmp_path) == (
        tmp_path / "control_plane" / DAEMON_LEASE_FILENAME
    )


def test_write_round_trips(tmp_path: Path) -> None:
    lease = DaemonLease(boot_id="abc-123", written_at_ms=1_700_000_000_000)

    write_daemon_lease(tmp_path, lease)
    restored = read_daemon_lease(tmp_path)

    assert restored == lease


def test_write_creates_control_plane_dir(tmp_path: Path) -> None:
    assert not (tmp_path / "control_plane").exists()

    write_daemon_lease(
        tmp_path, DaemonLease(boot_id="abc", written_at_ms=1)
    )

    assert (tmp_path / "control_plane").exists()
    assert (tmp_path / "control_plane" / DAEMON_LEASE_FILENAME).exists()
    # No .tmp left behind.
    assert not (tmp_path / "control_plane" / f"{DAEMON_LEASE_FILENAME}.tmp").exists()


def test_read_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_daemon_lease(tmp_path) is None


def test_read_returns_none_for_malformed_json(tmp_path: Path) -> None:
    path = daemon_lease_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json\n", encoding="utf-8")

    assert read_daemon_lease(tmp_path) is None


def test_read_returns_none_for_forward_incompatible_schema(tmp_path: Path) -> None:
    path = daemon_lease_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 99,
        "boot_id": "x",
        "written_at_ms": 1,
        "status": "CONNECTED",
        "lease_cadence_ms": DEFAULT_LEASE_CADENCE_MS,
        "lease_threshold_ms": DEFAULT_LEASE_THRESHOLD_MS,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert read_daemon_lease(tmp_path) is None


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_lease_is_fresh_none_is_not_fresh() -> None:
    assert lease_is_fresh(None, now_ms=1) is False


def test_lease_is_fresh_within_threshold() -> None:
    lease = DaemonLease(boot_id="x", written_at_ms=1_000)
    assert lease_is_fresh(lease, now_ms=1_000 + DEFAULT_LEASE_THRESHOLD_MS) is True


def test_lease_is_fresh_past_threshold() -> None:
    lease = DaemonLease(boot_id="x", written_at_ms=1_000)
    assert lease_is_fresh(lease, now_ms=1_000 + DEFAULT_LEASE_THRESHOLD_MS + 1) is False


def test_lease_is_fresh_respects_override() -> None:
    lease = DaemonLease(boot_id="x", written_at_ms=1_000)
    # The lease's own threshold says fresh; a tighter override says stale.
    assert (
        lease_is_fresh(lease, now_ms=1_500, max_age_ms=DEFAULT_LEASE_THRESHOLD_MS)
        is True
    )
    assert lease_is_fresh(lease, now_ms=1_500, max_age_ms=100) is False


# ---------------------------------------------------------------------------
# DaemonLeaseWriter lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_emits_initial_lease_synchronously(tmp_path: Path) -> None:
    """A freshly-started daemon must be observably connected BEFORE
    the first cadence tick — otherwise a tight-loop watchdog could
    observe an expired-or-missing lease in the first second after
    daemon boot."""
    now = make_test_clock(1_700_000_000_000)

    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="daemon-boot-001",
        now_ms=now,
        cadence_ms=10,
    )
    await writer.start()
    try:
        lease = read_daemon_lease(tmp_path)
    finally:
        await writer.stop()

    assert lease is not None
    assert lease.boot_id == "daemon-boot-001"
    assert lease.status == "CONNECTED"


@pytest.mark.asyncio
async def test_writer_renews_on_cadence(tmp_path: Path) -> None:
    """The lease's ``written_at_ms`` advances with the clock tick."""
    now = make_test_clock(1_700_000_000_000)
    written_ats: list[int] = []

    def _capture(_root: Path, lease: DaemonLease) -> None:
        written_ats.append(lease.written_at_ms)
        write_daemon_lease(_root, lease)

    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="x",
        now_ms=now,
        cadence_ms=5,
        writer=_capture,
    )
    await writer.start()
    # Advance the simulated clock across multiple ticks.
    for _ in range(4):
        await asyncio.sleep(0.01)
        now.tick(5)  # type: ignore[attr-defined]
    await writer.stop()

    assert len(written_ats) >= 3
    assert written_ats == sorted(written_ats)  # monotonic non-decreasing


@pytest.mark.asyncio
async def test_set_draining_triggers_immediate_flush(tmp_path: Path) -> None:
    """The DRAINING transition must not wait for the next cadence
    tick — the watchdog needs to see it as soon as the daemon
    decides to drain."""
    now = make_test_clock(1_700_000_000_000)
    statuses: list[str] = []

    def _capture(_root: Path, lease: DaemonLease) -> None:
        statuses.append(lease.status)
        write_daemon_lease(_root, lease)

    # Cadence is 1s — only the immediate flush can land the DRAINING
    # status within the test window.
    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="x",
        now_ms=now,
        cadence_ms=1_000,
        writer=_capture,
    )
    await writer.start()
    # First emit is CONNECTED (initial synchronous write).
    assert statuses[0] == "CONNECTED"

    writer.set_draining()
    await asyncio.sleep(0.05)
    await writer.stop()

    # DRAINING must have landed before the steady-state 1s cadence
    # would have fired.
    assert "DRAINING" in statuses


@pytest.mark.asyncio
async def test_stop_is_bounded(tmp_path: Path) -> None:
    """``stop()`` returns within ``2 * cadence`` seconds. The fallback
    cancel path is allowed but ``stop()`` itself must not hang."""
    now = make_test_clock(1_700_000_000_000)
    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="x",
        now_ms=now,
        cadence_ms=10,
    )
    await writer.start()
    await asyncio.wait_for(writer.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_writer_oserror_does_not_kill_loop(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    failed_once = {"count": 0}
    successful: list[int] = []

    def _flaky(_root: Path, lease: DaemonLease) -> None:
        if failed_once["count"] == 0:
            failed_once["count"] = 1
            raise OSError("disk full")
        successful.append(lease.written_at_ms)

    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="x",
        now_ms=now,
        cadence_ms=5,
        writer=_flaky,
    )
    await writer.start()
    for _ in range(3):
        await asyncio.sleep(0.01)
        now.tick(5)  # type: ignore[attr-defined]
    await writer.stop()

    # First write failed; the loop kept going and at least one
    # subsequent write succeeded.
    assert len(successful) >= 1


@pytest.mark.asyncio
async def test_set_draining_is_idempotent(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    writer = DaemonLeaseWriter(
        artifacts_root=tmp_path,
        boot_id="x",
        now_ms=now,
        cadence_ms=10,
    )
    await writer.start()
    writer.set_draining()
    writer.set_draining()
    writer.set_draining()
    await writer.stop()

    assert writer.status == "DRAINING"
