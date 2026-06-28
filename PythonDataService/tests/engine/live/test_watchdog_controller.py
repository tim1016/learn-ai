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

  _watchdog_flatten_now (engine adapter logic):
  - Returns "not_needed" when engine has no open positions (flag never set).
  - Returns "completed" when bar loop clears the flag and positions are zero.
  - Returns "timed_out" when bar loop never clears the flag (via executor timeout).
  - Returns "failed" when flag clears but positions remain (partial fill).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.engine.live.account_artifacts import read_account_freeze
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
    artifacts_root: Path | None = None,
    account_id: str | None = None,
) -> WatchdogHaltExecutor:
    store = IncidentStore(tmp_path)
    # Monotonic clock so latency assertions don't flake on very fast CI.
    _tick = [0]

    def _clock_ms() -> int:
        _tick[0] += 1
        return _tick[0]

    return WatchdogHaltExecutor(
        controller,
        store,
        timeouts=timeouts,
        clock_ms=_clock_ms,
        artifacts_root=artifacts_root,
        account_id=account_id,
    )


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
    gate = incident.evidence["gate_results"][0]  # type: ignore[index]
    assert gate["gate_id"] == "watchdog.lease_loss"
    assert gate["status"] == "freeze"
    assert gate["source"] == "watchdog_halt_executor"
    assert gate["operator_reason"] == "watchdog.flatten_failed"
    assert gate["operator_next_step"] == "CHECK_IBKR"
    assert isinstance(gate["evidence_at_ms"], int)


@pytest.mark.asyncio
async def test_unsafe_watchdog_outcome_writes_account_freeze(tmp_path: Path) -> None:
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec, flatten_outcome="failed")
    executor = _make_executor(
        tmp_path,
        controller,
        artifacts_root=tmp_path,
        account_id="DU123456",
    )

    await executor.execute("LEASE_EXPIRED")

    freeze = read_account_freeze(tmp_path, "DU123456")
    assert freeze is not None
    assert freeze.reason == "watchdog.flatten_failed"
    assert freeze.source == "watchdog_halt_executor"
    assert freeze.operator_next_step == "CHECK_IBKR"


@pytest.mark.asyncio
async def test_flatten_failure_evidence_is_persisted_before_disconnect(tmp_path: Path) -> None:
    """The broker may disconnect only after terminal flatten evidence exists.

    The initial scaffold is not enough; disconnect must observe the
    post-flatten ``flatten_outcome`` evidence on disk so a crash during
    broker teardown still leaves an explainable unresolved exposure record.
    """
    rec = _OrderingRecorder()
    store = IncidentStore(tmp_path)

    class _CheckingController(_FakeController):
        async def disconnect_broker(self) -> BrokerDisconnectOutcome:
            unresolved = store.list_unresolved()
            assert any(
                incident.evidence.get("flatten_outcome") == "failed"
                and incident.evidence.get("disconnect_outcome") is None
                for incident in unresolved
            ), "terminal flatten failure evidence must be durable before disconnect"
            return await super().disconnect_broker()

    controller = _CheckingController(
        recorder=rec, flatten_raises=RuntimeError("broker gone")
    )
    tick = [0]

    def _clock_ms() -> int:
        tick[0] += 1
        return tick[0]

    executor = WatchdogHaltExecutor(
        controller, store, timeouts=_FAST_TIMEOUTS, clock_ms=_clock_ms
    )

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_failed"
    assert rec.seq_of("disconnect_broker") > rec.seq_of("flatten_now")


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


# ---------------------------------------------------------------------------
# _watchdog_flatten_now — engine adapter logic
# ---------------------------------------------------------------------------


class _FakeEngineRef:
    """Minimal stand-in for ``LiveEngine`` used by ``_watchdog_flatten_now``."""

    def __init__(self, *, has_positions: bool = False) -> None:
        self._flatten_now_requested: bool = False
        self._has_positions = has_positions
        # Track whether the flag was ever set by the coroutine.
        self.flag_was_set = False

    def _has_open_positions(self) -> bool:
        return self._has_positions


@pytest.mark.asyncio
async def test_flatten_now_returns_not_needed_when_no_positions() -> None:
    """When the engine has no open positions the flag must NOT be set."""
    from app.engine.live.live_engine import _watchdog_flatten_now

    engine = _FakeEngineRef(has_positions=False)
    result = await _watchdog_flatten_now(engine)

    assert result == "not_needed"
    assert not engine._flatten_now_requested, "flag must remain False for a no-op"


@pytest.mark.asyncio
async def test_flatten_now_returns_completed_when_positions_close_in_time() -> None:
    """Bar loop clears the flag after ~50ms and positions become zero → completed."""
    from app.engine.live.live_engine import _watchdog_flatten_now

    engine = _FakeEngineRef(has_positions=True)

    async def _simulate_bar_loop() -> None:
        """Emulate the bar loop: wait briefly, clear flag, mark positions gone."""
        await asyncio.sleep(0.05)
        engine._flatten_now_requested = False
        engine._has_positions = False

    bar_loop_task = asyncio.create_task(_simulate_bar_loop())
    result = await _watchdog_flatten_now(engine)
    await bar_loop_task

    assert result == "completed"


@pytest.mark.asyncio
async def test_flatten_now_returns_failed_when_positions_remain_after_flatten() -> None:
    """Bar loop clears the flag but positions are still non-zero (partial fill)."""
    from app.engine.live.live_engine import _watchdog_flatten_now

    engine = _FakeEngineRef(has_positions=True)

    async def _simulate_partial_fill() -> None:
        await asyncio.sleep(0.05)
        # Clear the flag but leave positions open (partial fill scenario).
        engine._flatten_now_requested = False
        # _has_positions stays True

    bar_loop_task = asyncio.create_task(_simulate_partial_fill())
    result = await _watchdog_flatten_now(engine)
    await bar_loop_task

    assert result == "failed"


@pytest.mark.asyncio
async def test_flatten_now_returns_timed_out_when_bar_loop_does_not_clear_flag() -> None:
    """The executor wraps flatten_now in wait_for; when the flag never clears
    the coroutine is cancelled and the executor records 'timed_out'."""
    from app.engine.live.live_engine import _watchdog_flatten_now

    engine = _FakeEngineRef(has_positions=True)

    # Use a tiny timeout via asyncio.wait_for, mirroring the executor's path.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_watchdog_flatten_now(engine), timeout=0.15)

    # The flag was set before the timeout fired.
    assert engine._flatten_now_requested, (
        "flag must be set even when the coroutine is cancelled by timeout"
    )


# ---------------------------------------------------------------------------
# Finding 1: halt sequence continues when initial incident write fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_continues_when_initial_incident_write_fails(tmp_path: Path) -> None:
    """Finding 1: if the incident store raises on the initial append, all 5 steps still run."""
    from unittest.mock import patch

    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec)

    # Build an executor with a real store, then make .append raise on first call.
    store = IncidentStore(tmp_path)
    original_append = store.append
    call_count = [0]

    def _fail_first_append(incident: object) -> object:
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("disk full")
        return original_append(incident)  # type: ignore[arg-type]

    _tick = [0]

    def _clock_ms() -> int:
        _tick[0] += 1
        return _tick[0]

    executor = WatchdogHaltExecutor(
        controller, store, timeouts=_FAST_TIMEOUTS, clock_ms=_clock_ms
    )

    with patch.object(store, "append", side_effect=_fail_first_append):
        incident = await executor.execute("LEASE_EXPIRED")

    # All 5 steps must have run despite the initial write failure.
    labels = [e.split(":", 1)[1] for e in rec.events]
    assert "block_submissions" in labels, "step 1 must run even when initial append fails"
    assert "persist_paused:LEASE_EXPIRED" in labels, "step 2 must run"
    assert "flatten_now" in labels, "step 3 must run"
    assert "disconnect_broker" in labels, "step 4 must run"
    assert "request_engine_exit" in labels, "step 5 must run"

    # The returned incident is still valid (in-memory object).
    assert incident is not None
    assert incident.notice.code == "watchdog.flatten_completed"


# ---------------------------------------------------------------------------
# Finding 2: critical terminal incidents stay unresolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_terminal_incidents_stay_unresolved(tmp_path: Path) -> None:
    """Finding 2: flatten_timed_out incident is NOT resolved by the executor."""
    rec = _OrderingRecorder()
    # flatten sleeps long enough to trip the 100ms timeout
    controller = _FakeController(recorder=rec, flatten_delay_s=10.0)
    store = IncidentStore(tmp_path)
    _tick = [0]

    def _clock_ms() -> int:
        _tick[0] += 1
        return _tick[0]

    executor = WatchdogHaltExecutor(
        controller, store, timeouts=_FAST_TIMEOUTS, clock_ms=_clock_ms
    )

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_timed_out"
    assert incident.resolved_at_ms is None, (
        "critical outcome must NOT be auto-resolved; post-halt gate needs to see it"
    )
    # The store's unresolved list must include it.
    unresolved = store.list_unresolved()
    codes = [i.notice.code for i in unresolved]
    assert "watchdog.flatten_timed_out" in codes


@pytest.mark.asyncio
async def test_critical_flatten_failed_stays_unresolved(tmp_path: Path) -> None:
    """Finding 2: flatten_failed incident is NOT resolved by the executor."""
    rec = _OrderingRecorder()
    controller = _FakeController(recorder=rec, flatten_raises=RuntimeError("broker gone"))
    store = IncidentStore(tmp_path)
    _tick = [0]

    def _clock_ms() -> int:
        _tick[0] += 1
        return _tick[0]

    executor = WatchdogHaltExecutor(
        controller, store, timeouts=_FAST_TIMEOUTS, clock_ms=_clock_ms
    )

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.notice.code == "watchdog.flatten_failed"
    assert incident.resolved_at_ms is None


async def _assert_safe_outcome_is_resolved(tmp_path: Path, flatten_outcome: str) -> None:
    store = IncidentStore(tmp_path / flatten_outcome)
    tick = [0]

    def _clock_ms() -> int:
        tick[0] += 1
        return tick[0]

    rec = _OrderingRecorder()
    controller = _FakeController(
        recorder=rec,
        flatten_outcome=flatten_outcome,  # type: ignore[arg-type]
    )
    executor = WatchdogHaltExecutor(
        controller, store, timeouts=_FAST_TIMEOUTS, clock_ms=_clock_ms
    )

    incident = await executor.execute("LEASE_EXPIRED")

    assert incident.resolved_at_ms is not None, (
        f"safe outcome {flatten_outcome!r} must be auto-resolved"
    )
    unresolved = store.list_unresolved()
    assert unresolved == [], f"Expected empty unresolved for {flatten_outcome!r}; got {unresolved}"


@pytest.mark.asyncio
async def test_safe_terminal_incidents_are_resolved(tmp_path: Path) -> None:
    """flatten_completed and flatten_not_needed get resolved by the executor."""
    await _assert_safe_outcome_is_resolved(tmp_path, "completed")
    await _assert_safe_outcome_is_resolved(tmp_path, "not_needed")


# ---------------------------------------------------------------------------
# Finding 3: scaffold incident (flatten_failed) blocks restart if child dies
# ---------------------------------------------------------------------------


def test_initial_scaffold_blocks_restart_if_child_dies(tmp_path: Path) -> None:
    """Finding 3: a scaffold-only incident (no terminal write) blocks the post-halt gate.

    Simulate a child crash AFTER the initial append but BEFORE the terminal
    write by manually writing just the initial scaffold produced by
    ``watchdog_incident()``.  The gate must return a blocking incident.
    """
    from app.engine.live.post_halt_gate import check_post_halt_gate
    from app.operator.incidents.watchdog_notices import watchdog_incident

    # Write only the scaffold — no terminal step, no resolve.
    scaffold = watchdog_incident(reason="LEASE_EXPIRED", started_at_ms=1_700_000_000_000)
    IncidentStore(tmp_path).append(scaffold)

    # The scaffold's notice code is watchdog.flatten_failed (critical / uncertain).
    assert scaffold.notice.code == "watchdog.flatten_failed", (
        "scaffold must use the pessimistic flatten_failed code so the gate blocks restart"
    )

    result = check_post_halt_gate(tmp_path, now_ms=1_700_000_001_000)
    assert result is not None, (
        "post-halt gate must block restart when only the scaffold is present"
    )
    assert result.notice.code == "reconciliation.required_after_uncertain_flatten"
