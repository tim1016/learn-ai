"""Tests for WatchdogHaltExecutor — 5-step halt with per-step timeouts.

Coverage:
  - Happy path: all steps succeed; flatten_completed notice emitted.
  - Flatten timeout: notice is critical flatten_timed_out; disconnect STILL runs.
  - Flatten exception: notice is critical flatten_failed; disconnect STILL runs;
    engine_exit STILL runs.
  - Broker already disconnected before flatten: broker_disconnected_before_flatten.
  - Disconnect timeout: engine_exit still called.
  - Exception in block_submissions: halt continues; engine_exit still called;
    incident records exception in evidence.
  - Incident evidence contains per-step latency.
  - Ordering: step 1 < step 2 < step 3 < step 4 < step 5.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.engine.live.watchdog_controller import (
    BrokerDisconnectOutcome,
    FlattenOutcome,
    LeaseLossReason,
    WatchdogHaltExecutor,
    WatchdogTimeouts,
)
from app.operator.incidents.store import IncidentStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _OrderingRecorder:
    """Records the sequence of step invocations with a monotonic counter."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._seq = 0

    def record(self, label: str) -> str:
        self._seq += 1
        entry = f"{self._seq}:{label}"
        self.events.append(entry)
        return entry

    def seq_of(self, prefix: str) -> int:
        """Return the sequence number of the first event matching prefix."""
        for e in self.events:
            seq, label = e.split(":", 1)
            if label == prefix:
                return int(seq)
        raise AssertionError(f"No event with label {prefix!r} in {self.events}")


class _FakeController:
    """Configurable fake for WatchdogShutdownController."""

    def __init__(
        self,
        *,
        recorder: _OrderingRecorder,
        flatten_outcome: FlattenOutcome = "completed",
        flatten_delay_s: float = 0.0,
        flatten_raises: Exception | None = None,
        disconnect_outcome: BrokerDisconnectOutcome = "completed",
        disconnect_delay_s: float = 0.0,
        disconnect_raises: Exception | None = None,
        block_raises: Exception | None = None,
        persist_raises: Exception | None = None,
    ) -> None:
        self._rec = recorder
        self._flatten_outcome = flatten_outcome
        self._flatten_delay_s = flatten_delay_s
        self._flatten_raises = flatten_raises
        self._disconnect_outcome = disconnect_outcome
        self._disconnect_delay_s = disconnect_delay_s
        self._disconnect_raises = disconnect_raises
        self._block_raises = block_raises
        self._persist_raises = persist_raises

    async def block_submissions(self) -> None:
        self._rec.record("block_submissions")
        if self._block_raises is not None:
            raise self._block_raises

    async def persist_paused(self, reason: LeaseLossReason) -> None:
        self._rec.record(f"persist_paused:{reason}")
        if self._persist_raises is not None:
            raise self._persist_raises

    async def flatten_now(self, reason: LeaseLossReason) -> FlattenOutcome:
        self._rec.record("flatten_now")
        if self._flatten_delay_s:
            await asyncio.sleep(self._flatten_delay_s)
        if self._flatten_raises is not None:
            raise self._flatten_raises
        return self._flatten_outcome

    async def disconnect_broker(self) -> BrokerDisconnectOutcome:
        self._rec.record("disconnect_broker")
        if self._disconnect_delay_s:
            await asyncio.sleep(self._disconnect_delay_s)
        if self._disconnect_raises is not None:
            raise self._disconnect_raises
        return self._disconnect_outcome

    async def request_engine_exit(self) -> None:
        self._rec.record("request_engine_exit")


# Fast timeouts for tests (ms)
_FAST_TIMEOUTS = WatchdogTimeouts(flatten_timeout_ms=100, disconnect_timeout_ms=100)


def _make_executor(
    tmp_path: Path,
    controller: _FakeController,
    *,
    timeouts: WatchdogTimeouts = _FAST_TIMEOUTS,
) -> WatchdogHaltExecutor:
    store = IncidentStore(tmp_path)
    # Monotonic clock so latency assertions don't flake on very fast CI.
    _tick = [0]

    def _clock_ms() -> int:
        _tick[0] += 1
        return _tick[0]

    return WatchdogHaltExecutor(controller, store, timeouts=timeouts, clock_ms=_clock_ms)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_writes_completed_incident(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_completed"
    assert incident.notice.tier == "info"
    assert incident.resolved_at_ms is not None
    # Evidence carries step outcomes
    assert incident.evidence["flatten_outcome"] == "completed"
    assert incident.evidence["disconnect_outcome"] == "completed"


@pytest.mark.asyncio
async def test_happy_path_all_5_steps_run(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    executor = _make_executor(tmp_path, controller)

    await executor.execute("LEASE_EXPIRED")

    labels = [e.split(":", 1)[1] for e in rec.events]
    assert "block_submissions" in labels
    assert "persist_paused:LEASE_EXPIRED" in labels
    assert "flatten_now" in labels
    assert "disconnect_broker" in labels
    assert "request_engine_exit" in labels


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steps_run_in_contract_order(tmp_path: Path) -> None:
    """1 < 2 < 3 < 4 < 5 — strict ordering invariant."""
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    executor = _make_executor(tmp_path, controller)

    await executor.execute("LEASE_EXPIRED")

    s1 = rec.seq_of("block_submissions")
    s2 = rec.seq_of("persist_paused:LEASE_EXPIRED")
    s3 = rec.seq_of("flatten_now")
    s4 = rec.seq_of("disconnect_broker")
    s5 = rec.seq_of("request_engine_exit")
    assert s1 < s2 < s3 < s4 < s5


# ---------------------------------------------------------------------------
# Flatten timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flatten_timeout_records_critical_and_continues(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    # flatten sleeps longer than the 100ms timeout
    controller = _FakeController(recorder=rec, flatten_delay_s=10.0)
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_timed_out"
    assert incident.notice.tier == "critical"
    # disconnect STILL ran
    assert rec.seq_of("disconnect_broker") > rec.seq_of("flatten_now")
    # engine_exit STILL ran
    assert rec.seq_of("request_engine_exit") > rec.seq_of("disconnect_broker")


# ---------------------------------------------------------------------------
# Flatten failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flatten_failed_records_critical_and_continues(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(
        recorder=rec, flatten_raises=RuntimeError("broker gone")
    )
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_failed"
    assert incident.notice.tier == "critical"
    # disconnect STILL ran
    assert "disconnect_broker" in [e.split(":", 1)[1] for e in rec.events]
    # engine_exit STILL ran
    assert "request_engine_exit" in [e.split(":", 1)[1] for e in rec.events]
    # error captured in evidence
    assert "broker gone" in str(incident.evidence.get("flatten_error", ""))


# ---------------------------------------------------------------------------
# Broker already disconnected before flatten
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broker_already_disconnected_short_circuits_to_critical(tmp_path: Path) -> None:
    """flatten_now returns 'broker_disconnected_before_flatten' → critical notice."""
    rec = _OrderingRecorder()
    controller = _FakeController(
        recorder=rec, flatten_outcome="broker_disconnected_before_flatten"
    )
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.broker_disconnected_before_flatten"
    assert incident.notice.tier == "critical"


# ---------------------------------------------------------------------------
# Flatten not_needed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flatten_not_needed_writes_info_notice(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec, flatten_outcome="not_needed")
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_not_needed"
    assert incident.notice.tier == "info"


# ---------------------------------------------------------------------------
# Disconnect timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_timeout_continues_to_engine_exit(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec, disconnect_delay_s=10.0)
    executor = _make_executor(tmp_path, controller)

    await executor.execute("LEASE_EXPIRED")

    # engine_exit must have been called despite disconnect timeout
    assert "request_engine_exit" in [e.split(":", 1)[1] for e in rec.events]


# ---------------------------------------------------------------------------
# Exception in block_submissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_in_block_submissions_does_not_propagate(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(
        recorder=rec, block_raises=RuntimeError("submission gate broken")
    )
    executor = _make_executor(tmp_path, controller)

    # Must NOT raise
    incident = await executor.execute("LEASE_EXPIRED")

    # engine_exit still ran
    assert "request_engine_exit" in [e.split(":", 1)[1] for e in rec.events]
    # Error captured in evidence
    errors = incident.evidence.get("per_step_errors", [])
    assert any("block_submissions" in e for e in errors)


# ---------------------------------------------------------------------------
# Incident evidence contains per-step latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_evidence_contains_per_step_latency(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("LEASE_EXPIRED")

    ev = incident.evidence
    assert ev.get("flatten_ms") is not None
    assert ev.get("disconnect_ms") is not None


# ---------------------------------------------------------------------------
# Incident is persisted by IncidentStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incident_is_written_to_store(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    store = IncidentStore(tmp_path)
    executor = WatchdogHaltExecutor(
        controller,
        store,
        timeouts=_FAST_TIMEOUTS,
        clock_ms=lambda: 1_700_000_000_000,
    )

    await executor.execute("LEASE_EXPIRED")

    # The resolved incident should be on disk (resolved_at_ms not None)
    unresolved = store.list_unresolved()
    assert unresolved == [], f"Expected empty unresolved list; got {unresolved}"


# ---------------------------------------------------------------------------
# BOOT_ID_CHANGED reason passes through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_id_changed_reason_passes_through(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)
    executor = _make_executor(tmp_path, controller)

    incident = await executor.execute("BOOT_ID_CHANGED")

    assert incident.evidence["reason"] == "BOOT_ID_CHANGED"


# ---------------------------------------------------------------------------
# Partial-failure rule: failing step does NOT skip subsequent steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_paused_failure_does_not_skip_flatten(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(
        recorder=rec, persist_raises=RuntimeError("sidecar write failed")
    )
    executor = _make_executor(tmp_path, controller)

    await executor.execute("LEASE_EXPIRED")

    labels = [e.split(":", 1)[1] for e in rec.events]
    assert "flatten_now" in labels
    assert "disconnect_broker" in labels
    assert "request_engine_exit" in labels
