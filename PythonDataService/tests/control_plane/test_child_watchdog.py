"""PRD #619-B B5 — child watchdog ordering + lease-loss contract.

Asserts:

- The watchdog stays HEALTHY when the lease is fresh and the boot_id
  matches.
- Lease expiry (now - written_at_ms > threshold) triggers the
  lease-lost handler with reason ``LEASE_EXPIRED``.
- ``boot_id`` mismatch triggers the handler with reason
  ``BOOT_ID_CHANGED``.
- A missing lease (file absent) triggers ``LEASE_EXPIRED``.
- **The 5-step handler runs in the contract order**:
  1. block submissions
  2. persist PAUSED + write incident
  3. evidence-flush grace
  4. broker disconnect
  5. request engine exit
- Each callback's failure must not prevent later steps from running
  (best-effort; the engine must still tear down).
- ``incident_writer`` is called with the right fields (reason, ms,
  observed/expected boot ids).
- ``poll_once`` is idempotent once the handler has fired — subsequent
  ticks do not re-run the 5 steps.
- ``stop()`` is bounded and shuts down the cadence task cleanly.
- ControlPlaneBlock updates flow into the runtime aggregator on every
  poll (so the 619-B B3 engine_runtime publisher records fresh
  observations).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.engine.live.child_watchdog import (
    DEFAULT_LEASE_THRESHOLD_MS,
    INCIDENT_FILENAME,
    INCIDENT_SCHEMA_VERSION,
    ChildWatchdog,
    write_lease_lost_incident,
)
from app.engine.live.control_plane import (
    DaemonLease,
    write_daemon_lease,
)
from tests._fixtures.fake_clock import make_test_clock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _OrderingRecorder:
    """Records the sequence of side-effect callbacks the watchdog
    fires. The test asserts on this list, not on any single fake's
    state."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, label: str) -> None:
        self.events.append(label)


class _NoopAggregator:
    """Stand-in for ``EngineRuntimeAggregator`` that records
    control-plane updates. We don't need the other three update
    methods for these tests."""

    def __init__(self) -> None:
        self.control_plane_updates: list[tuple[int, str | None]] = []

    async def update_control_plane(self, block) -> None:
        self.control_plane_updates.append(
            (block.lease_observed_at_ms, block.observed_daemon_boot_id)
        )


def _make_watchdog(
    tmp_path: Path,
    *,
    now: Callable[[], int],
    aggregator: _NoopAggregator | None = None,
    expected_boot: str | None = "daemon-boot-A",
    poll_cadence_ms: int = 10,
    lease_threshold_ms: int = DEFAULT_LEASE_THRESHOLD_MS,
    evidence_flush_grace_ms: int = 0,  # 0 = instant for tests
    lease_loss_grace_ms: int = 0,  # 0 = bypass grace window for existing halt tests
    recorder: _OrderingRecorder | None = None,
    incident_writer: Callable[..., None] | None = None,
) -> ChildWatchdog:
    rec = recorder or _OrderingRecorder()

    def _block_submissions() -> None:
        rec.record("block_submissions")

    def _persist_paused(reason: str) -> None:
        rec.record(f"persist_paused:{reason}")

    async def _disconnect_broker() -> None:
        rec.record("disconnect_broker")

    def _request_engine_exit() -> None:
        rec.record("request_engine_exit")

    def _wrapped_incident_writer(
        run_dir: Path,
        *,
        reason: str,
        observed_at_ms: int,
        expected_daemon_boot_id: str | None,
        observed_daemon_boot_id: str | None,
        lease_written_at_ms: int | None,
    ) -> None:
        rec.record(f"write_incident:{reason}")
        if incident_writer is not None:
            incident_writer(
                run_dir,
                reason=reason,
                observed_at_ms=observed_at_ms,
                expected_daemon_boot_id=expected_daemon_boot_id,
                observed_daemon_boot_id=observed_daemon_boot_id,
                lease_written_at_ms=lease_written_at_ms,
            )

    return ChildWatchdog(
        artifacts_root=tmp_path,
        run_dir=tmp_path / "run-1",
        expected_daemon_boot_id=expected_boot,
        block_submissions=_block_submissions,
        persist_paused=_persist_paused,
        disconnect_broker=_disconnect_broker,
        request_engine_exit=_request_engine_exit,
        now_ms=now,
        aggregator=aggregator,
        poll_cadence_ms=poll_cadence_ms,
        lease_threshold_ms=lease_threshold_ms,
        evidence_flush_grace_ms=evidence_flush_grace_ms,
        lease_loss_grace_ms=lease_loss_grace_ms,
        incident_writer=_wrapped_incident_writer,
    )


# ---------------------------------------------------------------------------
# Healthy path — no handler fires when lease is fresh and boot_id matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_when_lease_fresh_and_boot_id_matches(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()
    aggregator = _NoopAggregator()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )

    watchdog = _make_watchdog(
        tmp_path,
        now=now,
        aggregator=aggregator,
        expected_boot="daemon-boot-A",
        recorder=rec,
    )

    await watchdog.poll_once()

    assert watchdog.state == "HEALTHY"
    assert rec.events == []
    # Control-plane block was updated with the observed boot_id.
    assert aggregator.control_plane_updates == [(now(), "daemon-boot-A")]


# ---------------------------------------------------------------------------
# Lease loss triggers — expired, missing, boot_id mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_expired_triggers_handler(tmp_path: Path) -> None:
    """A lease older than ``lease_threshold_ms`` triggers
    ``LEASE_EXPIRED``."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now() - 10_000),
    )

    watchdog = _make_watchdog(
        tmp_path,
        now=now,
        lease_threshold_ms=5_000,
        recorder=rec,
    )

    await watchdog.poll_once()

    assert watchdog.state == "EXITED"
    assert "write_incident:LEASE_EXPIRED" in rec.events


@pytest.mark.asyncio
async def test_missing_lease_triggers_handler(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    # No lease file written.
    watchdog = _make_watchdog(tmp_path, now=now, recorder=rec)

    await watchdog.poll_once()

    assert watchdog.state == "EXITED"
    assert "write_incident:LEASE_EXPIRED" in rec.events


@pytest.mark.asyncio
async def test_boot_id_mismatch_triggers_handler(tmp_path: Path) -> None:
    """A fresh lease whose ``boot_id`` differs from the expected one
    triggers ``BOOT_ID_CHANGED``."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-DIFFERENT", written_at_ms=now()),
    )

    watchdog = _make_watchdog(
        tmp_path, now=now, expected_boot="daemon-boot-A", recorder=rec
    )

    await watchdog.poll_once()

    assert watchdog.state == "EXITED"
    assert "write_incident:BOOT_ID_CHANGED" in rec.events


@pytest.mark.asyncio
async def test_boot_id_check_skipped_when_expected_is_none(tmp_path: Path) -> None:
    """When ``expected_daemon_boot_id`` is None (e.g., before daemon
    integration lands), the boot_id check is skipped — only freshness
    matters."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="anything", written_at_ms=now()),
    )

    watchdog = _make_watchdog(tmp_path, now=now, expected_boot=None, recorder=rec)

    await watchdog.poll_once()

    assert watchdog.state == "HEALTHY"
    assert rec.events == []


# ---------------------------------------------------------------------------
# Ordering contract — 5 steps in exact order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_lost_handler_runs_steps_in_contract_order(tmp_path: Path) -> None:
    """PRD #619-B B5 contract: the handler steps run in this exact
    order. This test is the single source of truth for the ordering
    invariant; any future refactor must preserve it."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    watchdog = _make_watchdog(tmp_path, now=now, recorder=rec)

    await watchdog.poll_once()  # no lease → LEASE_EXPIRED

    assert rec.events == [
        "block_submissions",  # 1
        "persist_paused:LEASE_EXPIRED",  # 2a
        "write_incident:LEASE_EXPIRED",  # 2b
        # step 3 (evidence-flush grace) is a sleep — no recorded event
        "disconnect_broker",  # 4
        "request_engine_exit",  # 5
    ]


@pytest.mark.asyncio
async def test_lease_lost_handler_is_idempotent(tmp_path: Path) -> None:
    """Once the handler has fired, subsequent poll ticks do not
    re-execute the 5 steps."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    watchdog = _make_watchdog(tmp_path, now=now, recorder=rec)

    await watchdog.poll_once()
    first_round = list(rec.events)

    await watchdog.poll_once()
    await watchdog.poll_once()

    assert rec.events == first_round  # no new events on subsequent polls


# ---------------------------------------------------------------------------
# Best-effort step failures must not prevent later steps from running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_failure_does_not_block_steps_2_3_4_5(tmp_path: Path) -> None:
    """A bug in ``block_submissions`` must not leave the child stuck —
    every subsequent step still runs."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    def _broken_block() -> None:
        rec.record("block_submissions")
        raise RuntimeError("simulated bug in submission gating")

    async def _disconnect() -> None:
        rec.record("disconnect_broker")

    watchdog = ChildWatchdog(
        artifacts_root=tmp_path,
        run_dir=tmp_path / "run-1",
        expected_daemon_boot_id="daemon-boot-A",
        block_submissions=_broken_block,
        persist_paused=lambda reason: rec.record(f"persist_paused:{reason}"),
        disconnect_broker=_disconnect,
        request_engine_exit=lambda: rec.record("request_engine_exit"),
        now_ms=now,
        evidence_flush_grace_ms=0,
        lease_loss_grace_ms=0,
        incident_writer=lambda **kw: rec.record(f"write_incident:{kw['reason']}"),
    )

    await watchdog.poll_once()

    assert watchdog.state == "EXITED"
    # Every later step still ran despite step 1 raising.
    assert "disconnect_broker" in rec.events
    assert "request_engine_exit" in rec.events


# ---------------------------------------------------------------------------
# Incident file content
# ---------------------------------------------------------------------------


def test_write_lease_lost_incident_writes_atomic_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"

    write_lease_lost_incident(
        run_dir,
        reason="BOOT_ID_CHANGED",
        observed_at_ms=1_700_000_000_500,
        expected_daemon_boot_id="boot-old",
        observed_daemon_boot_id="boot-new",
        lease_written_at_ms=1_700_000_000_000,
    )

    path = run_dir / INCIDENT_FILENAME
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == INCIDENT_SCHEMA_VERSION
    assert data["reason"] == "BOOT_ID_CHANGED"
    assert data["expected_daemon_boot_id"] == "boot-old"
    assert data["observed_daemon_boot_id"] == "boot-new"
    assert data["lease_written_at_ms"] == 1_700_000_000_000


@pytest.mark.asyncio
async def test_handler_writes_incident_file_to_run_dir(tmp_path: Path) -> None:
    """The default incident writer (not the recorder shim) is invoked
    when no override is supplied. The file lands under run_dir."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()
    run_dir = tmp_path / "run-1"

    # Don't pass an override — use the real writer.
    watchdog = ChildWatchdog(
        artifacts_root=tmp_path,
        run_dir=run_dir,
        expected_daemon_boot_id="daemon-boot-A",
        block_submissions=lambda: rec.record("block_submissions"),
        persist_paused=lambda reason: rec.record(f"persist_paused:{reason}"),
        disconnect_broker=_make_async_record(rec, "disconnect_broker"),
        request_engine_exit=lambda: rec.record("request_engine_exit"),
        now_ms=now,
        evidence_flush_grace_ms=0,
        lease_loss_grace_ms=0,
    )

    await watchdog.poll_once()

    assert (run_dir / INCIDENT_FILENAME).exists()


def _make_async_record(rec: _OrderingRecorder, label: str) -> Callable[[], Awaitable[None]]:
    async def _fn() -> None:
        rec.record(label)

    return _fn


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_bounded(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )

    watchdog = _make_watchdog(tmp_path, now=now, recorder=rec)

    await watchdog.start()
    await asyncio.wait_for(watchdog.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path: Path) -> None:
    now = make_test_clock(1_700_000_000_000)
    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )

    watchdog = _make_watchdog(tmp_path, now=now)

    await watchdog.start()
    await watchdog.start()
    await watchdog.start()
    await watchdog.stop()


@pytest.mark.asyncio
async def test_inner_loop_tolerates_exceptions(tmp_path: Path) -> None:
    """A poll exception must not kill the watchdog loop — it should
    keep cadencing."""
    now = make_test_clock(1_700_000_000_000)
    rec = _OrderingRecorder()

    write_daemon_lease(
        tmp_path,
        DaemonLease(boot_id="daemon-boot-A", written_at_ms=now()),
    )

    flaky_count = {"count": 0}

    def _flaky_aggregator():
        class _Flaky:
            async def update_control_plane(self, _block) -> None:
                flaky_count["count"] += 1
                if flaky_count["count"] == 1:
                    raise RuntimeError("transient")

        return _Flaky()

    watchdog = _make_watchdog(
        tmp_path,
        now=now,
        aggregator=_flaky_aggregator(),
        poll_cadence_ms=5,
        recorder=rec,
    )

    await watchdog.start()
    await asyncio.sleep(0.03)
    await watchdog.stop()

    # First aggregator call raised; subsequent polls still ran.
    assert flaky_count["count"] >= 2
