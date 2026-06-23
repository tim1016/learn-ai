"""Tests for the SUSPECTED_LOSS grace state in ChildWatchdog.

Verifies:
  - A single bad observation enters SUSPECTED_LOSS without triggering halt.
  - A recovery within the grace window returns to HEALTHY with no incident.
  - Sustained loss past the grace window triggers the halt handler.
  - Multiple polls within the grace window do NOT prematurely trigger halt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.engine.live.child_watchdog import (
    DEFAULT_LEASE_THRESHOLD_MS,
    ChildWatchdog,
)
from app.engine.live.control_plane import DaemonLease, write_daemon_lease
from tests._fixtures.fake_clock import make_test_clock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _OrderingRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, label: str) -> None:
        self.events.append(label)


def _make_watchdog(
    tmp_path: Path,
    *,
    now: Callable[[], int],
    expected_boot: str | None = "daemon-boot-A",
    lease_threshold_ms: int = DEFAULT_LEASE_THRESHOLD_MS,
    lease_loss_grace_ms: int = 5_000,
    recorder: _OrderingRecorder | None = None,
    incident_writer: Callable[..., None] | None = None,
) -> ChildWatchdog:
    rec = recorder or _OrderingRecorder()

    def _incident_writer_shim(**kw: object) -> None:
        rec.record(f"write_incident:{kw['reason']}")
        if incident_writer is not None:
            incident_writer(**kw)

    return ChildWatchdog(
        artifacts_root=tmp_path,
        run_dir=tmp_path / "run-1",
        expected_daemon_boot_id=expected_boot,
        block_submissions=lambda: rec.record("block_submissions"),
        persist_paused=lambda reason: rec.record(f"persist_paused:{reason}"),
        disconnect_broker=_make_async_record(rec, "disconnect_broker"),
        request_engine_exit=lambda: rec.record("request_engine_exit"),
        now_ms=now,
        lease_threshold_ms=lease_threshold_ms,
        lease_loss_grace_ms=lease_loss_grace_ms,
        evidence_flush_grace_ms=0,
        incident_writer=_incident_writer_shim,
    )


def _make_async_record(rec: _OrderingRecorder, label: str) -> Callable[[], Awaitable[None]]:
    async def _fn() -> None:
        rec.record(label)

    return _fn


# ---------------------------------------------------------------------------
# SUSPECTED_LOSS entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_bad_observation_enters_suspected_loss(tmp_path: Path) -> None:
    """First bad observation → SUSPECTED_LOSS, NOT LEASE_LOST_HANDLING."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    # Write an expired lease.
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now() - 10_000),
    )

    watchdog = _make_watchdog(
        tmp_path, now=now, lease_threshold_ms=5_000, recorder=rec
    )

    await watchdog.poll_once()

    assert watchdog.state == "SUSPECTED_LOSS"
    assert rec.events == []  # No halt yet


# ---------------------------------------------------------------------------
# Recovery within grace window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flap_within_grace_window_does_not_trigger_halt(tmp_path: Path) -> None:
    """Lease recovers within the 5s grace → returns to HEALTHY, no incident."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    # Poll 1: expired lease → SUSPECTED_LOSS.
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now() - 10_000),
    )
    watchdog = _make_watchdog(
        tmp_path, now=now, lease_threshold_ms=5_000, lease_loss_grace_ms=5_000, recorder=rec
    )
    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"

    # Advance clock by 2s (still within 5s grace) and write a fresh lease.
    now.tick(2_000)
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )

    await watchdog.poll_once()

    assert watchdog.state == "HEALTHY"
    assert rec.events == []  # No halt triggered


# ---------------------------------------------------------------------------
# Sustained loss past grace → halt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_loss_past_grace_triggers_halt(tmp_path: Path) -> None:
    """Loss sustained past 5s grace → LEASE_LOST_HANDLING + halt sequence."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    # Write expired lease.
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now() - 10_000),
    )
    watchdog = _make_watchdog(
        tmp_path, now=now, lease_threshold_ms=5_000, lease_loss_grace_ms=5_000, recorder=rec
    )

    # Poll 1: enter SUSPECTED_LOSS.
    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"

    # Advance clock past the 5s grace period; lease still expired.
    now.tick(6_000)

    # Poll 2: grace elapsed → trigger halt.
    await watchdog.poll_once()

    assert watchdog.state == "EXITED"
    assert "block_submissions" in rec.events
    assert "request_engine_exit" in rec.events


# ---------------------------------------------------------------------------
# Multiple polls within grace do NOT trigger halt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grace_state_persists_across_poll_intervals(tmp_path: Path) -> None:
    """Multiple polls within the grace window must NOT prematurely trigger halt."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now() - 10_000),
    )
    watchdog = _make_watchdog(
        tmp_path, now=now, lease_threshold_ms=5_000, lease_loss_grace_ms=5_000, recorder=rec
    )

    # Poll 1: enter SUSPECTED_LOSS.
    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"

    # Poll 2 at +1s — still in grace.
    now.tick(1_000)
    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"
    assert rec.events == []

    # Poll 3 at +3s — still in grace.
    now.tick(2_000)
    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"
    assert rec.events == []


# ---------------------------------------------------------------------------
# SUSPECTED_LOSS doesn't fire if BOOT_ID recovers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_id_mismatch_recovers_within_grace(tmp_path: Path) -> None:
    """BOOT_ID_CHANGED observed once; correct boot_id appears → back to HEALTHY."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    # Write lease with mismatched boot_id.
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-WRONG", written_at_ms=now()),
    )
    watchdog = _make_watchdog(
        tmp_path,
        now=now,
        expected_boot="daemon-boot-A",
        lease_loss_grace_ms=5_000,
        recorder=rec,
    )

    await watchdog.poll_once()
    assert watchdog.state == "SUSPECTED_LOSS"

    # Write correct boot_id.
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )
    await watchdog.poll_once()

    assert watchdog.state == "HEALTHY"
    assert rec.events == []
