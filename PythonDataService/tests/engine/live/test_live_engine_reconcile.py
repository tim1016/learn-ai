"""Reconciliation PR 2 — runtime RECONCILE verb in LiveEngine.

Exercises the async control task spawned by ``_dispatch_command`` when it
receives the RECONCILE verb:

* the submit lock is shared with the bar-loop submit critical section;
* the inhibit flag is set the instant the verb is acked and stays set
  through the lock-wait;
* the verdict from the orchestrator drives whether the barrier releases
  (Continue / Adopt-no-pause), stays set + the engine pauses (Adopt-pause),
  or stays set + the engine halts (Poison);
* concurrent RECONCILE returns ``already_running`` rather than racing the
  in-flight task;
* the initial ack carries ``status="accepted"`` + ``request_id`` and the
  completion overwrites it with ``status="completed"`` + ``verdict=...``.

The orchestrator and broker probe are mocked at the engine import seam so
the tests exercise the engine's plumbing — not the orchestrator (PR 1's
tests already cover that) and not the IBKR client (its tests cover the
real probe).
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Any

import pytest

from app.engine.live import reconciliation_orchestrator
from app.engine.live.account_registry import AccountInstanceBinding, write_account_instance_binding
from app.engine.live.command_channel import Command, CommandChannel, CommandVerb
from app.engine.live.config import LiveConfig
from app.engine.live.desired_state import DesiredState
from app.engine.live.fleet_reset_baseline import baseline_path
from app.engine.live.live_engine import (
    BrokerRecoveryReconcileBlockedError,
    LiveEngine,
    ReconnectAccountMismatchHaltError,
)
from app.engine.live.reconciliation_classifier import (
    Adopt,
    BrokerSnapshot,
    Continue,
    OwnedOrphan,
    Poison,
)
from app.engine.live.reconciliation_orchestrator import ReconciliationResult
from app.schemas.live_runs import ReconciliationReceipt
from tests.engine.live.fixtures.fake_broker import FakeBroker


class _EngineHarness:
    """Bundle the engine + a stub broker-snapshot builder and orchestrator.

    Wiring at construction time keeps each test's setup short and lets the
    tests drive the engine via direct ``_dispatch_command`` calls — the
    full bar loop is overkill for this slice.
    """

    def __init__(
        self,
        engine: LiveEngine,
        channel: CommandChannel,
        *,
        snapshot: BrokerSnapshot,
    ) -> None:
        self.engine = engine
        self.channel = channel
        self._snapshot = snapshot

        async def _fake_snapshot() -> BrokerSnapshot:
            return self._snapshot

        # Override the broker-snapshot builder so the FakeBroker engine
        # doesn't try to call real IBKR cache-sync routines.
        engine._build_runtime_broker_snapshot = _fake_snapshot  # type: ignore[assignment]

    def stub_reconcile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        result_factory,
    ) -> None:
        """Patch the orchestrator's ``reconcile`` function so the engine's
        local import picks up our fake.
        """

        async def _fake(**kwargs: Any) -> ReconciliationResult:
            return await result_factory(kwargs)

        monkeypatch.setattr(reconciliation_orchestrator, "reconcile", _fake)


def _make_engine(
    tmp_path: Path, *, command_channel: CommandChannel | None = None
) -> tuple[LiveEngine, CommandChannel]:
    if command_channel is None:
        command_channel = CommandChannel(tmp_path / "commands")
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        artifacts_root=tmp_path / "artifacts",
        account_id="DU123",
        run_id="run-test",
        strategy_instance_id="spy_ema_paper",
        command_channel=command_channel,
    )
    return engine, command_channel


def _make_receipt(
    *, status: str = "passed", outcome: str | None = "clean"
) -> ReconciliationReceipt:
    return ReconciliationReceipt(
        status=status,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        run_id="run-test",
        strategy_instance_id="spy_ema_paper",
        namespace="learn-ai/spy_ema_paper/v1",
        started_at_ms=1,
        completed_at_ms=2,
        last_reconcile_ms=2,
    )


async def _await_task(engine: LiveEngine, timeout: float = 2.0) -> None:
    assert engine._reconcile_task is not None
    await asyncio.wait_for(engine._reconcile_task, timeout=timeout)


def _harness(tmp_path: Path) -> _EngineHarness:
    engine, channel = _make_engine(tmp_path)
    return _EngineHarness(
        engine, channel, snapshot=BrokerSnapshot(open_orders=(), executions=())
    )


@pytest.mark.asyncio
async def test_runtime_reconcile_passes_known_sibling_namespaces_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    engine, channel = _make_engine(run_dir)
    harness = _EngineHarness(
        engine, channel, snapshot=BrokerSnapshot(open_orders=(), executions=())
    )
    write_account_instance_binding(
        run_dir / "artifacts",
        AccountInstanceBinding(
            account_id="DU123",
            strategy_instance_id="aapl_ema_paper",
            run_id="run-aapl",
            bot_order_namespace="learn-ai/aapl_ema_paper/v1",
            lifecycle_state="ACTIVE",
            recorded_at_ms=1_700_000_000_000,
            source="test",
        ),
    )
    path = baseline_path(run_dir.parent, "DU123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps(
            {
                "account_id": "DU123",
                "baseline_at_ms": 1_700_000_010_000,
                "positions": [],
                "open_orders": [],
                "applies_to_strategy_instance_ids": ["spy_ema_paper"],
            }
        ),
        encoding="utf-8",
    )
    shutdown_event = asyncio.Event()

    async def _result(kwargs: dict) -> ReconciliationResult:
        assert kwargs["owned_namespaces"] == frozenset({"learn-ai/spy_ema_paper/v1"})
        assert kwargs["known_sibling_namespaces"] == frozenset({"learn-ai/aapl_ema_paper/v1"})
        assert kwargs["ignore_unknown_namespaces_before_ms"] == 1_700_000_010_000
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    await _await_task(engine)

    assert engine._inhibit_submits is False
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_reconcile_acquires_submit_lock_before_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start a fake submit that holds the lock; enqueue RECONCILE; assert
    the reconcile task waits for the lock before its broker probe runs.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    shutdown_event = asyncio.Event()
    probe_ran = asyncio.Event()

    async def _result(kwargs: dict) -> ReconciliationResult:
        probe_ran.set()
        await kwargs["broker_probe"]()
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    release_submit = asyncio.Event()

    async def _holds_lock() -> None:
        async with engine._submit_lock:
            await release_submit.wait()

    holder = asyncio.create_task(_holds_lock())
    # Yield so the holder grabs the lock first.
    await asyncio.sleep(0)

    outcome = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    assert outcome["status"] == "accepted"
    # Yield enough for the task to attempt lock acquisition.
    for _ in range(5):
        await asyncio.sleep(0)
    # The probe must not have run yet — the holder still owns the lock.
    assert not probe_ran.is_set()
    # The inhibit flag is set the moment the verb is acked.
    assert engine._inhibit_submits is True
    # Release the holder.
    release_submit.set()
    await holder
    await _await_task(engine)
    assert probe_ran.is_set()


@pytest.mark.asyncio
async def test_reconcile_inhibits_submits_until_clean_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RECONCILE issued → ``_inhibit_submits`` is True; a Continue verdict
    releases it.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    shutdown_event = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    outcome = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    assert outcome["status"] == "accepted"
    assert engine._inhibit_submits is True
    await _await_task(engine)

    # Continue verdict releases the barrier.
    assert engine._inhibit_submits is False
    assert engine._paused is False
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_broker_recovery_reconcile_releases_barrier_and_bumps_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnect recovery uses the runtime reconcile core and records a
    successful recovery as a new connection epoch.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    engine._connection_epoch = 7
    shutdown_event = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        assert engine._inhibit_submits is True
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    outcome = await engine.run_broker_recovery_reconcile(shutdown_event)

    assert outcome["status"] == "completed"
    assert outcome["verdict"] == "clean"
    assert engine._inhibit_submits is False
    assert engine._connection_epoch == 8
    assert engine._paused is False
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_broker_recovery_reconcile_snapshots_client_reconnect_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monitor-owned recovery should consume the client reconnect counter so
    the legacy bar-loop gate does not count the same reconnect again.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    engine._connection_epoch = 7
    engine._last_connectivity_lost_count = 1

    class _Client:
        connectivity_lost_count = 3
        connected_account = "DU123"

    engine._client = _Client()  # type: ignore[assignment]
    shutdown_event = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    await engine.run_broker_recovery_reconcile(shutdown_event)

    assert engine._connection_epoch == 8
    assert engine._last_connectivity_lost_count == 3
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_broker_recovery_reconcile_account_mismatch_halts_before_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconnect recovery preserves the old account-mismatch halt before
    probing or adopting broker orders.
    """
    harness = _harness(tmp_path)
    engine = harness.engine

    class _Client:
        connectivity_lost_count = 4
        connected_account = "DU999"

    class _Portfolio:
        pending_orders = [object()]

    engine._client = _Client()  # type: ignore[assignment]
    engine._run_portfolio = _Portfolio()
    shutdown_event = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        pytest.fail("account mismatch should halt before reconciliation")

    harness.stub_reconcile(monkeypatch, _result)

    with pytest.raises(ReconnectAccountMismatchHaltError) as raised:
        await engine.run_broker_recovery_reconcile(shutdown_event)

    assert raised.value.connected_account == "DU999"
    assert raised.value.connection_epoch == 1
    assert engine._inhibit_submits is True
    assert engine._connection_epoch == 1
    assert engine._last_connectivity_lost_count == 4
    assert engine._run_portfolio.pending_orders == []  # type: ignore[union-attr]
    assert shutdown_event.is_set()
    halt_payload = (tmp_path / "halt.flag").read_text(encoding="utf-8")
    assert "RECONNECT_ACCOUNT_MISMATCH_HALT" in halt_payload
    assert "connected_account=DU999" in halt_payload


@pytest.mark.asyncio
async def test_reconcile_adoption_with_active_exposure_stays_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adopt(pause=True) → ``_inhibit_submits`` stays True, ``_paused``
    becomes True, and the desired-state writer persists PAUSED.
    """
    persisted: list[tuple[DesiredState, str]] = []

    def _writer(state: DesiredState, reason: str) -> None:
        persisted.append((state, reason))

    harness = _harness(tmp_path)
    engine = harness.engine
    engine._desired_state_writer = _writer
    shutdown_event = asyncio.Event()

    orphan = OwnedOrphan(
        order_ref="learn-ai/spy_ema_paper/v1:iid-1",
        intent_id="iid-1",
        perm_id=7,
        order_id=42,
        active=True,
        source="broker_open_order",
    )

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(
            verdict=Adopt(orphans=(orphan,), pause=True, pause_reason="active_exposure"),
            receipt=_make_receipt(outcome="adopted"),
        )

    harness.stub_reconcile(monkeypatch, _result)

    engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    await _await_task(engine)

    assert engine._inhibit_submits is True
    assert engine._paused is True
    assert persisted == [
        (DesiredState.PAUSED, "runtime_reconcile:ambiguous_exposure"),
    ]


@pytest.mark.asyncio
async def test_broker_recovery_reconcile_adoption_pause_raises_and_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-recovery must fail closed when reconciliation adopts ambiguous
    active exposure.
    """
    persisted: list[tuple[DesiredState, str]] = []

    def _writer(state: DesiredState, reason: str) -> None:
        persisted.append((state, reason))

    harness = _harness(tmp_path)
    engine = harness.engine
    engine._desired_state_writer = _writer
    engine._connection_epoch = 3
    shutdown_event = asyncio.Event()

    orphan = OwnedOrphan(
        order_ref="learn-ai/spy_ema_paper/v1:iid-1",
        intent_id="iid-1",
        perm_id=7,
        order_id=42,
        active=True,
        source="broker_open_order",
    )

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(
            verdict=Adopt(orphans=(orphan,), pause=True, pause_reason="active_exposure"),
            receipt=_make_receipt(outcome="adopted"),
        )

    harness.stub_reconcile(monkeypatch, _result)

    with pytest.raises(BrokerRecoveryReconcileBlockedError) as raised:
        await engine.run_broker_recovery_reconcile(shutdown_event)

    assert raised.value.outcome["verdict"] == "adopted_paused"
    assert engine._inhibit_submits is True
    assert engine._paused is True
    assert engine._connection_epoch == 3
    assert persisted == [
        (DesiredState.PAUSED, "broker_recovery_reconcile:ambiguous_exposure"),
    ]
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_reconcile_poison_halts_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poison verdict → ``shutdown_event`` is set so the engine halts."""
    harness = _harness(tmp_path)
    engine = harness.engine
    shutdown_event = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(
            verdict=Poison(reason="broker_probe_failed"),
            receipt=_make_receipt(status="failed", outcome=None),
        )

    harness.stub_reconcile(monkeypatch, _result)

    engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    await _await_task(engine)

    assert shutdown_event.is_set()
    # The barrier stays set fail-closed.
    assert engine._inhibit_submits is True


@pytest.mark.asyncio
async def test_concurrent_reconciles_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second RECONCILE while the first is mid-flight returns
    ``{"status": "already_running"}`` rather than spawning a second task.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    shutdown_event = asyncio.Event()
    release = asyncio.Event()

    async def _result(_kwargs: dict) -> ReconciliationResult:
        await release.wait()
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    outcome1 = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    assert outcome1["status"] == "accepted"
    first_task = engine._reconcile_task
    # Yield so the task starts.
    await asyncio.sleep(0)

    outcome2 = engine._dispatch_command(
        Command(seq=2, verb=CommandVerb.RECONCILE),
        shutdown_event,
    )
    # The second dispatch must NOT spawn a new task; it must return
    # the already-running envelope so the operator can see why their
    # second click had no effect.
    assert outcome2["status"] == "already_running"
    assert engine._reconcile_task is first_task

    release.set()
    await _await_task(engine)


@pytest.mark.asyncio
async def test_reconcile_acknowledgement_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial ack must carry ``status="accepted"`` + request_id +
    accepted_at_ms; the completion artifact overwrites it with
    ``status="completed"`` + verdict.
    """
    harness = _harness(tmp_path)
    engine = harness.engine
    channel = harness.channel
    shutdown_event = asyncio.Event()
    cmd = channel.write_from_operator(CommandVerb.RECONCILE)

    async def _result(_kwargs: dict) -> ReconciliationResult:
        return ReconciliationResult(verdict=Continue(), receipt=_make_receipt())

    harness.stub_reconcile(monkeypatch, _result)

    # Simulate the command poll loop: dispatch, then ack with the
    # initial outcome, then await the spawned reconcile task.
    outcome = engine._dispatch_command(cmd, shutdown_event)
    channel.ack(cmd, outcome=outcome)

    ack_path = tmp_path / "commands" / f"command.{cmd.seq}.RECONCILE.ack.json"
    accepted = _json.loads(ack_path.read_text(encoding="utf-8"))["outcome"]
    assert accepted["status"] == "accepted"
    assert accepted["request_id"] and len(accepted["request_id"]) == 22
    assert isinstance(accepted["accepted_at_ms"], int)
    assert accepted["accepted_at_ms"] > 0

    await _await_task(engine)

    completed = _json.loads(ack_path.read_text(encoding="utf-8"))["outcome"]
    assert completed["status"] == "completed"
    assert completed["verdict"] == "clean"
    assert completed["request_id"] == accepted["request_id"]
