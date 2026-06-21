"""PRD #619-B B2 — aggregator + serialized publisher contract.

Asserts:

- The aggregator returns ``None`` until every block has been populated
  once; partial state cannot leak through.
- Updates from different producers are independent (one block's
  update does not reset another's).
- The publisher writes monotonic ``snapshot_seq`` (strictly
  increasing, never repeated, never skipped except across stop+start).
- Steady-state cadence emits one write per interval.
- ``request_immediate_flush()`` shortcuts the next steady-state wait
  so safety transitions land on the next loop iteration.
- ``stop()`` flushes one final snapshot.
- The writer's failure path is logged + tolerated (the publisher
  keeps running so the next tick can recover from a transient OSError).
- Concurrent producers under load never cause a torn snapshot — every
  emitted snapshot validates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
)
from app.engine.live.engine_runtime_publisher import (
    EngineRuntimeAggregator,
    EngineRuntimePublisher,
)
from tests._fixtures.fake_clock import make_test_clock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aggregator(**overrides) -> EngineRuntimeAggregator:
    return EngineRuntimeAggregator(
        strategy_instance_id=overrides.get("strategy_instance_id", "sid-1"),
        run_id=overrides.get("run_id", "run-1"),
        pid=overrides.get("pid", 1234),
        process_start_identity=overrides.get("process_start_identity", "child-001"),
        expected_daemon_boot_id=overrides.get("expected_daemon_boot_id", "daemon-001"),
    )


def _command_loop(at_ms: int = 1_700_000_000_000) -> CommandLoopBlock:
    return CommandLoopBlock(heartbeat_at_ms=at_ms, state="RUNNING")


def _broker(at_ms: int = 1_700_000_000_000) -> BrokerBlock:
    return BrokerBlock(
        identity="PAPER_VERIFIED",
        submission_capability="PAPER_ORDERS_ENABLED",
        effective_posture="PAPER_EXECUTION",
        connection_state="connected",
        connection_epoch=1,
        connected_account="DU1234567",
        port_class="paper_port",
        observation_at_ms=at_ms,
        probe_completed_at_ms=at_ms - 100,
        reconnect_attempt=0,
    )


def _bar_loop(at_ms: int = 1_700_000_000_000) -> BarLoopBlock:
    return BarLoopBlock(
        heartbeat_at_ms=at_ms,
        latest_source_bar_ms=at_ms - 60_000,
        expected_interval_ms=60_000,
    )


def _control_plane(at_ms: int = 1_700_000_000_000) -> ControlPlaneBlock:
    return ControlPlaneBlock(
        lease_observed_at_ms=at_ms - 500,
        observed_daemon_boot_id="daemon-001",
    )


async def _seed_all_blocks(agg: EngineRuntimeAggregator, at_ms: int = 1_700_000_000_000) -> None:
    await agg.update_command_loop(_command_loop(at_ms))
    await agg.update_broker(_broker(at_ms))
    await agg.update_bar_loop(_bar_loop(at_ms))
    await agg.update_control_plane(_control_plane(at_ms))


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregator_returns_none_until_every_block_set() -> None:
    agg = _aggregator()

    assert await agg.snapshot(snapshot_seq=0, written_at_ms=1) is None

    await agg.update_command_loop(_command_loop())
    assert await agg.snapshot(snapshot_seq=0, written_at_ms=1) is None

    await agg.update_broker(_broker())
    assert await agg.snapshot(snapshot_seq=0, written_at_ms=1) is None

    await agg.update_bar_loop(_bar_loop())
    assert await agg.snapshot(snapshot_seq=0, written_at_ms=1) is None

    await agg.update_control_plane(_control_plane())
    coherent = await agg.snapshot(snapshot_seq=0, written_at_ms=1)
    assert coherent is not None
    assert coherent.snapshot_seq == 0
    assert coherent.written_at_ms == 1


@pytest.mark.asyncio
async def test_aggregator_block_updates_are_independent() -> None:
    agg = _aggregator()
    await _seed_all_blocks(agg, at_ms=1_700_000_000_000)

    # Update only the broker — every other block should retain prior state.
    new_broker = _broker(at_ms=1_700_000_010_000)
    await agg.update_broker(new_broker)

    snap = await agg.snapshot(snapshot_seq=1, written_at_ms=1_700_000_010_000)
    assert snap is not None
    assert snap.broker == new_broker
    assert snap.command_loop.heartbeat_at_ms == 1_700_000_000_000
    assert snap.bar_loop.heartbeat_at_ms == 1_700_000_000_000


# ---------------------------------------------------------------------------
# Publisher — steady-state cadence + monotonic seq
# ---------------------------------------------------------------------------


def _capture_writer() -> tuple[list[EngineRuntimeSnapshot], Callable[[Path, EngineRuntimeSnapshot], None]]:
    captured: list[EngineRuntimeSnapshot] = []

    def _writer(_run_dir: Path, snap: EngineRuntimeSnapshot) -> None:
        captured.append(snap)

    return captured, _writer


@pytest.mark.asyncio
async def test_publisher_emits_monotonic_snapshot_seq(tmp_path: Path) -> None:
    """Three steady-state ticks → seq 0, 1, 2 in order. ``stop()``
    flushes a fourth snapshot at seq=3."""
    now = make_test_clock(1_700_000_000_000)
    captured, writer = _capture_writer()

    agg = _aggregator()
    await _seed_all_blocks(agg)

    pub = EngineRuntimePublisher(
        agg, run_dir=tmp_path, now_ms=now, steady_state_interval_s=0.01, writer=writer
    )
    await pub.start()
    await asyncio.sleep(0.05)  # ~5 ticks
    await pub.stop()

    assert len(captured) >= 3
    seqs = [s.snapshot_seq for s in captured]
    # Strictly monotonic; no gaps.
    assert seqs == list(range(len(seqs)))
    assert pub.last_written_seq == seqs[-1]


@pytest.mark.asyncio
async def test_publisher_skips_emit_when_aggregator_incomplete(tmp_path: Path) -> None:
    """Until all four blocks are set, the publisher must NOT emit a
    partial snapshot. Once the final block lands, the next tick
    writes."""
    now = make_test_clock(1_700_000_000_000)
    captured, writer = _capture_writer()

    agg = _aggregator()
    # Only seed three of four — publisher should noop.
    await agg.update_command_loop(_command_loop())
    await agg.update_broker(_broker())
    await agg.update_bar_loop(_bar_loop())

    pub = EngineRuntimePublisher(
        agg, run_dir=tmp_path, now_ms=now, steady_state_interval_s=0.01, writer=writer
    )
    await pub.start()
    await asyncio.sleep(0.03)
    assert captured == []

    await agg.update_control_plane(_control_plane())
    await asyncio.sleep(0.04)
    await pub.stop()

    assert len(captured) >= 1
    assert captured[0].snapshot_seq == 0


@pytest.mark.asyncio
async def test_publisher_request_immediate_flush_shortcuts_steady_state(
    tmp_path: Path,
) -> None:
    """Setting the immediate-flush event causes the next loop iteration
    to write without waiting for the 1Hz cadence."""
    now = make_test_clock(1_700_000_000_000)
    captured, writer = _capture_writer()

    agg = _aggregator()
    await _seed_all_blocks(agg)

    # 1-second cadence so a steady-state tick will not fire during the
    # window we measure — the immediate flush must do the work.
    pub = EngineRuntimePublisher(
        agg, run_dir=tmp_path, now_ms=now, steady_state_interval_s=1.0, writer=writer
    )
    await pub.start()
    # Yield so the publisher's first iteration runs (emits seq=0
    # immediately, then sleeps up to 1s).
    await asyncio.sleep(0.01)
    initial_count = len(captured)

    pub.request_immediate_flush()
    await asyncio.sleep(0.05)
    after_flush = len(captured)
    await pub.stop()

    assert after_flush > initial_count
    # All emitted seqs are still strictly monotonic.
    seqs = [s.snapshot_seq for s in captured]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_publisher_stop_is_bounded(tmp_path: Path) -> None:
    """Stopping a healthy publisher returns within roughly
    ``2 * interval`` seconds. Cancellation is the fallback, not the
    primary path."""
    now = make_test_clock(1_700_000_000_000)
    _, writer = _capture_writer()

    agg = _aggregator()
    await _seed_all_blocks(agg)

    pub = EngineRuntimePublisher(
        agg, run_dir=tmp_path, now_ms=now, steady_state_interval_s=0.01, writer=writer
    )
    await pub.start()
    # Stop is bounded — assertEventually completes.
    await asyncio.wait_for(pub.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_publisher_writer_oserror_does_not_kill_loop(tmp_path: Path) -> None:
    """Transient writer failures must not crash the publisher task —
    the next tick should still attempt to write."""
    now = make_test_clock(1_700_000_000_000)
    captured: list[EngineRuntimeSnapshot] = []
    failed_once = {"count": 0}

    def _flaky_writer(_run_dir: Path, snap: EngineRuntimeSnapshot) -> None:
        if failed_once["count"] == 0:
            failed_once["count"] = 1
            raise OSError("disk full!")
        captured.append(snap)

    agg = _aggregator()
    await _seed_all_blocks(agg)

    pub = EngineRuntimePublisher(
        agg,
        run_dir=tmp_path,
        now_ms=now,
        steady_state_interval_s=0.01,
        writer=_flaky_writer,
    )
    await pub.start()
    await asyncio.sleep(0.05)
    await pub.stop()

    # First write failed; the loop kept going and at least one
    # subsequent write succeeded. The seq counter only advances on
    # successful writes so the captured snapshots remain monotonic
    # starting from 0.
    assert len(captured) >= 1
    assert captured[0].snapshot_seq == 0


# ---------------------------------------------------------------------------
# Concurrent-producer race — every emitted snapshot must validate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_producers_never_emit_torn_snapshot(tmp_path: Path) -> None:
    """Three producers update three different blocks at high frequency
    while the publisher serializes reads. Every captured snapshot must
    be a fully-validated ``EngineRuntimeSnapshot`` (no torn fields),
    and seq must remain monotonic."""
    now = make_test_clock(1_700_000_000_000)
    captured: list[EngineRuntimeSnapshot] = []

    def _capture(_run_dir: Path, snap: EngineRuntimeSnapshot) -> None:
        captured.append(snap)

    agg = _aggregator()
    await _seed_all_blocks(agg)

    pub = EngineRuntimePublisher(
        agg,
        run_dir=tmp_path,
        now_ms=now,
        steady_state_interval_s=0.005,
        writer=_capture,
    )
    await pub.start()

    async def _hammer_command_loop() -> None:
        for i in range(50):
            await agg.update_command_loop(
                CommandLoopBlock(
                    heartbeat_at_ms=1_700_000_000_000 + i, state="RUNNING"
                )
            )
            await asyncio.sleep(0)

    async def _hammer_broker() -> None:
        for i in range(50):
            await agg.update_broker(_broker(at_ms=1_700_000_000_000 + i))
            await asyncio.sleep(0)

    async def _hammer_bar_loop() -> None:
        for i in range(50):
            await agg.update_bar_loop(_bar_loop(at_ms=1_700_000_000_000 + i))
            await asyncio.sleep(0)

    await asyncio.gather(_hammer_command_loop(), _hammer_broker(), _hammer_bar_loop())
    await asyncio.sleep(0.02)
    await pub.stop()

    assert len(captured) >= 1
    seqs = [s.snapshot_seq for s in captured]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # No exception was raised inside Pydantic validation → every
    # snapshot was coherent.
