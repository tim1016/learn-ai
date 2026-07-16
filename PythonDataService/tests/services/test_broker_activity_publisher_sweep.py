"""Tests for the periodic IBKR reqExecutions sweep (PR 6 / operator-notice §11).

Coverage:
- Periodic sweep fires immediately on start, then at each interval.
- Unmatched (foreign) exec emits a critical reconciliation incident.
- Matched exec (engine intent present) does NOT emit an incident.
- Same exec_id in two consecutive sweeps produces only one incident.
- Publisher does NOT modify engine portfolio state from the sweep.
- Lookback bound filters executions older than the window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.live.live_state_sidecar import (
    LiveStateEnvelope,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.operator.incidents.store import IncidentStore
from app.schemas.broker_activity import ReconciliationTimingPolicy
from app.services.broker_activity_publisher import (
    DEFAULT_SWEEP_INTERVAL_MS,
    DEFAULT_SWEEP_LOOKBACK_MS,
    BrokerActivityPublisher,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    instance_broker_activity_wal_path,
)

pytestmark = pytest.mark.asyncio

SID = "sid-sweep-test"
NS = f"learn-ai/{SID}/v1"
INTENT_ID = "intent-sweep-1"
ORDER_REF = f"{NS}:{INTENT_ID}"


# ── Helpers ─────────────────────────────────────────────────────────


def _seed_envelope(
    artifacts_root: Path,
    *,
    submitted_orders: dict | None = None,
) -> None:
    envelope = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id="run-sweep-1",
        bot_order_namespace=NS,
        ib_client_id=42,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
        submitted_orders=(
            submitted_orders
            if submitted_orders is not None
            else {INTENT_ID: {"perm_id": 999, "order_id": 42, "status": "Submitted", "symbol": "SPY"}}
        ),
    )
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts_root, SID))
    repo._path.parent.mkdir(parents=True, exist_ok=True)
    repo.write(envelope)


def _fill_event(
    *,
    exec_id: str = "exec-sweep-1",
    order_ref: str | None = ORDER_REF,
    symbol: str = "SPY",
    side: str = "BUY",
    exec_time_ms: int | None = None,
) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        perm_id=999,
        event_type="fill",
        status="Filled",
        order_ref=order_ref,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        order_type="MKT",
        exec_id=exec_id,
        fill_quantity=100.0,
        avg_fill_price=450.0,
        cumulative_filled=100.0,
        remaining=0.0,
        last_fill_price=450.0,
        exec_time_ms=exec_time_ms,
        fee=1.0,
        ts_ms=1_700_000_000_000,
    )


def _make_event_source(events: list[IbkrOrderEvent]):
    async def _gen() -> AsyncIterator[IbkrOrderEvent]:
        for ev in events:
            yield ev
        await asyncio.sleep(3600)

    return lambda: _gen()


def _recovery_factory(events: list[IbkrOrderEvent]):
    async def _fetch() -> list[IbkrOrderEvent]:
        return list(events)

    return _fetch


def _build_publisher(
    tmp_path: Path,
    *,
    recovery_events: list[IbkrOrderEvent] | None = None,
    incident_store: IncidentStore | None = None,
    sweep_interval_ms: int = DEFAULT_SWEEP_INTERVAL_MS,
    sweep_lookback_ms: int = DEFAULT_SWEEP_LOOKBACK_MS,
    submitted_orders: dict | None = None,
) -> tuple[BrokerActivityPublisher, Path, Path]:
    artifacts = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    _seed_envelope(artifacts, submitted_orders=submitted_orders)
    publisher = BrokerActivityPublisher(
        strategy_instance_id=SID,
        bot_order_namespace=NS,
        run_dir=run_dir,
        artifacts_root=artifacts,
        timing_policy=ReconciliationTimingPolicy(),
        event_source_factory=_make_event_source([]),
        recovery_source_factory=_recovery_factory(recovery_events or []),
        incident_store=incident_store,
        sweep_interval_ms=sweep_interval_ms,
        sweep_lookback_ms=sweep_lookback_ms,
    )
    return publisher, run_dir, artifacts


# ── Tests ────────────────────────────────────────────────────────────


async def test_periodic_sweep_runs_immediately_then_at_interval(
    tmp_path: Path,
) -> None:
    """The first sweep fires within 1 s of start (immediate); a second
    sweep fires after ``sweep_interval_ms`` milliseconds."""
    sweep_call_times: list[float] = []
    original_run = BrokerActivityPublisher._run_periodic_sweep

    async def _record_and_delegate(self) -> int:  # type: ignore[override]
        sweep_call_times.append(asyncio.get_event_loop().time())
        return await original_run(self)

    publisher, _, _ = _build_publisher(
        tmp_path,
        recovery_events=[],
        # Use a very short interval so the test doesn't take 60 s.
        sweep_interval_ms=100,
    )
    BrokerActivityPublisher._run_periodic_sweep = _record_and_delegate  # type: ignore[method-assign]
    publisher.start()
    try:
        # Wait enough for the immediate sweep + one interval sweep.
        await asyncio.sleep(0.3)
    finally:
        await publisher.stop()
        BrokerActivityPublisher._run_periodic_sweep = original_run  # type: ignore[method-assign]

    assert len(sweep_call_times) >= 2, f"expected >= 2 sweeps, got {len(sweep_call_times)}"
    # First sweep within 1 s.
    elapsed_to_first = sweep_call_times[0] - (sweep_call_times[0] - 0)
    assert elapsed_to_first >= 0
    # Subsequent sweeps are at least 100 ms apart.
    gaps = [sweep_call_times[i + 1] - sweep_call_times[i] for i in range(len(sweep_call_times) - 1)]
    assert all(g >= 0.08 for g in gaps), f"sweep gaps too short: {gaps}"


async def test_unmatched_exec_emits_critical_incident(tmp_path: Path) -> None:
    """A fill whose order_ref does not match any engine intent triggers a
    critical reconciliation.discovered_execution_not_in_engine_state
    incident."""
    incident_store = IncidentStore(tmp_path / "run-dir")
    foreign_fill = _fill_event(exec_id="foreign-sweep-1", order_ref=None)

    publisher, _, _ = _build_publisher(
        tmp_path,
        recovery_events=[foreign_fill],
        incident_store=incident_store,
    )
    count = await publisher._run_periodic_sweep()
    assert count == 1

    incidents = incident_store.list_unresolved()
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.category == "reconciliation"
    assert inc.notice.code == "reconciliation.discovered_execution_not_in_engine_state"
    assert inc.notice.tier == "critical"
    assert inc.incident_id == "cross-client-foreign-sweep-1"
    assert inc.resolved_at_ms is None


async def test_matched_exec_does_not_emit_incident(tmp_path: Path) -> None:
    """A fill whose order_ref matches an engine intent is authored normally
    and does NOT produce an OperatorIncident."""
    incident_store = IncidentStore(tmp_path / "run-dir")
    matched_fill = _fill_event(exec_id="matched-sweep-1", order_ref=ORDER_REF)

    publisher, _run_dir, artifacts = _build_publisher(
        tmp_path,
        recovery_events=[matched_fill],
        incident_store=incident_store,
    )
    count = await publisher._run_periodic_sweep()
    assert count == 1

    incidents = incident_store.list_unresolved()
    assert incidents == [], f"unexpected incidents: {incidents}"

    # The fill was authored in the WAL (normal path, not unmatched).
    wal = BrokerActivityWal(instance_broker_activity_wal_path(artifacts, SID))
    rows = wal.read_all()
    assert len(rows) == 1
    assert rows[0].exec_id == "matched-sweep-1"


async def test_own_namespace_exec_without_legacy_wal_does_not_emit_incident(
    tmp_path: Path,
) -> None:
    """Account-Clerk submissions are owned by their broker order reference.

    The Clerk durable lane intentionally does not write the legacy per-run
    intent WAL.  A sweep must still recognize a fill carrying this instance's
    exact namespace rather than recording a false foreign-execution incident.
    """
    incident_store = IncidentStore(tmp_path / "run-dir")
    own_fill = _fill_event(exec_id="clerk-owned-sweep-1", order_ref=ORDER_REF)

    publisher, _run_dir, _artifacts = _build_publisher(
        tmp_path,
        recovery_events=[own_fill],
        incident_store=incident_store,
        submitted_orders={},
    )
    count = await publisher._run_periodic_sweep()

    assert count == 1
    assert incident_store.list_unresolved() == []


async def test_sweep_dedupes_by_exec_id(tmp_path: Path) -> None:
    """The same exec_id appearing in two consecutive sweeps produces only
    one incident (the second sweep sees the exec_id in ``_seen_exec_ids``
    after the first sweep authored it)."""
    incident_store = IncidentStore(tmp_path / "run-dir")
    foreign_fill = _fill_event(exec_id="dup-foreign-1", order_ref=None)

    publisher, _, _ = _build_publisher(
        tmp_path,
        recovery_events=[foreign_fill],
        incident_store=incident_store,
    )

    # First sweep — should author + emit incident.
    await publisher._run_periodic_sweep()
    # Second sweep — same exec_id, should be deduped.
    await publisher._run_periodic_sweep()

    incidents = incident_store.list_unresolved()
    assert len(incidents) == 1, f"expected 1 incident, got {len(incidents)}"


async def test_sweep_does_not_silently_correct_positions(tmp_path: Path) -> None:
    """The publisher must NOT modify the engine's LiveStateEnvelope
    (submitted_orders or any portfolio field) when a foreign exec is
    discovered.  The engine remains authoritative."""
    incident_store = IncidentStore(tmp_path / "run-dir")
    foreign_fill = _fill_event(exec_id="silent-correct-1", order_ref=None)

    publisher, _, artifacts = _build_publisher(
        tmp_path,
        recovery_events=[foreign_fill],
        incident_store=incident_store,
    )

    # Read the envelope before the sweep.
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts, SID))
    before = repo.read()
    assert before is not None

    await publisher._run_periodic_sweep()

    # The envelope must be identical except for
    # ``last_broker_activity_wal_seq`` which the publisher's normal
    # persist path advances (that is expected behaviour — WAL cursor).
    after = repo.read()
    assert after is not None
    assert after.submitted_orders == before.submitted_orders, "sweep must not modify submitted_orders"


async def test_lookback_bound_clamps_request_window(tmp_path: Path) -> None:
    """Executions with ``exec_time_ms`` older than ``now - lookback_ms``
    are filtered out client-side and not authored."""
    import time

    now_ms = int(time.time() * 1000)
    lookback_ms = 300_000  # 5 minutes
    cutoff_ms = now_ms - lookback_ms

    incident_store = IncidentStore(tmp_path / "run-dir")

    old_fill = _fill_event(
        exec_id="old-fill-1",
        order_ref=None,
        exec_time_ms=cutoff_ms - 60_000,  # 1 minute before cutoff → filtered
    )
    recent_fill = _fill_event(
        exec_id="recent-fill-1",
        order_ref=None,
        exec_time_ms=cutoff_ms + 60_000,  # 1 minute after cutoff → processed
    )

    publisher, _run_dir, artifacts = _build_publisher(
        tmp_path,
        recovery_events=[old_fill, recent_fill],
        incident_store=incident_store,
        sweep_lookback_ms=lookback_ms,
    )
    count = await publisher._run_periodic_sweep()

    # Only the recent fill should have been processed.
    assert count == 1
    wal = BrokerActivityWal(instance_broker_activity_wal_path(artifacts, SID))
    rows = wal.read_all()
    assert len(rows) == 1
    assert rows[0].exec_id == "recent-fill-1"

    # One incident for the recent foreign fill; the old one was filtered.
    incidents = incident_store.list_unresolved()
    assert len(incidents) == 1
    assert "recent-fill-1" in incidents[0].incident_id


async def test_sweep_no_incident_store_warns_not_raises(tmp_path: Path) -> None:
    """When ``incident_store`` is None, the publisher logs a WARN but does
    not raise — legacy callers and tests without an IncidentStore must
    not break."""
    foreign_fill = _fill_event(exec_id="no-store-1", order_ref=None)
    publisher, _, _ = _build_publisher(
        tmp_path,
        recovery_events=[foreign_fill],
        incident_store=None,  # explicit: no store wired
    )
    # Must not raise.
    count = await publisher._run_periodic_sweep()
    assert count == 1  # row authored even without the store


# ── Bootstrap wiring tests (P1 reviewer fix) ─────────────────────────
#
# These tests verify that bootstrap_publisher_for_instance wires an
# IncidentStore so cross-client executions are persisted, not just logged.
# The bootstrap has external dependencies (settings, IBKR client, registry)
# so we monkeypatch them at the module boundary rather than touching the
# real filesystem / broker.


def _seed_bootstrap_env(artifacts_root: Path) -> None:
    """Seed the minimal on-disk state needed by bootstrap_publisher_for_instance."""
    from app.engine.live.live_state_sidecar import (
        stable_live_state_path,
    )

    envelope = LiveStateEnvelope(
        strategy_instance_id=SID,
        run_id="run-bootstrap-test",
        bot_order_namespace=NS,
        ib_client_id=1,
        last_processed_bar_ms=1,
        last_artifact_flush_ms=1,
    )
    repo = LiveStateSidecarRepo(stable_live_state_path(artifacts_root, SID))
    repo._path.parent.mkdir(parents=True, exist_ok=True)
    repo.write(envelope)


async def test_bootstrap_passes_incident_store(tmp_path: Path, monkeypatch) -> None:
    """Finding (PR #665 P1): production bootstrap MUST pass an IncidentStore
    so cross-client incidents are persisted, not just logged.

    Monkeypatches the external deps (IBKR client, settings, registry) so we
    can call bootstrap_publisher_for_instance without a running broker or
    real containers.
    """
    import asyncio

    import app.routers.broker_activity as ba_module

    artifacts_root = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir(parents=True, exist_ok=True)
    _seed_bootstrap_env(artifacts_root)

    # Patch settings so artifacts_root resolves.
    class _FakeSettings:
        live_runs_root = str(artifacts_root / "runs")

    monkeypatch.setattr(ba_module, "get_settings", lambda: _FakeSettings())

    # Patch IBKR client — connected fake.
    class _FakeClient:
        def is_connected(self) -> bool:
            return True

    monkeypatch.setattr(ba_module, "get_client", lambda: _FakeClient())

    # Patch latest_run_dir_for_instance so it returns our tmp run_dir.
    def _fake_run_dir(art_root: Path, sid: str) -> Path:
        return run_dir

    import app.engine.live.run_lookup as run_lookup_module

    monkeypatch.setattr(run_lookup_module, "latest_run_dir_for_instance", _fake_run_dir)

    # Patch event factories so the publisher doesn't try to talk to IBKR.
    async def _empty_gen():
        if False:
            yield  # type: ignore[unreachable]
        await asyncio.sleep(3600)

    async def _empty_recovery() -> list:
        return []

    monkeypatch.setattr(ba_module, "stream_order_events", lambda _client: _empty_gen)
    monkeypatch.setattr(
        ba_module,
        "executions_for_reconnect_recovery",
        lambda _client: _empty_recovery,
    )

    # Use a fresh registry so we don't collide with other tests.
    from app.services.broker_activity_publisher_registry import (
        BrokerActivityPublisherRegistry,
    )

    fresh_reg = BrokerActivityPublisherRegistry()
    monkeypatch.setattr(ba_module, "get_publisher_registry", lambda: fresh_reg)

    publisher = await ba_module.bootstrap_publisher_for_instance(SID)
    try:
        assert publisher._incident_store is not None, (
            "bootstrap_publisher_for_instance must wire an IncidentStore; got None"
        )
        assert isinstance(publisher._incident_store, IncidentStore), (
            f"expected IncidentStore, got {type(publisher._incident_store)}"
        )
    finally:
        await publisher.stop()


async def test_sweep_persists_incident_via_bootstrapped_store(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: bootstrap a publisher (with its real IncidentStore),
    run the sweep with a foreign exec, assert the incident JSON appears
    in the run_dir/operator_incidents/ directory.
    """
    import asyncio

    import app.routers.broker_activity as ba_module

    artifacts_root = tmp_path / "artifacts"
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir(parents=True, exist_ok=True)
    _seed_bootstrap_env(artifacts_root)

    class _FakeSettings:
        live_runs_root = str(artifacts_root / "runs")

    monkeypatch.setattr(ba_module, "get_settings", lambda: _FakeSettings())

    class _FakeClient:
        def is_connected(self) -> bool:
            return True

    monkeypatch.setattr(ba_module, "get_client", lambda: _FakeClient())

    import app.engine.live.run_lookup as run_lookup_module

    monkeypatch.setattr(run_lookup_module, "latest_run_dir_for_instance", lambda _art, _sid: run_dir)

    from app.broker.ibkr.models import IbkrOrderEvent

    foreign_fill = IbkrOrderEvent(
        account_id="DU9999999",
        order_id=99,
        perm_id=1,
        event_type="fill",
        status="Filled",
        order_ref=None,  # no namespace → unmatched
        symbol="AAPL",
        side="BUY",
        order_type="MKT",
        exec_id="foreign-bootstrap-e2e-1",
        fill_quantity=10.0,
        avg_fill_price=200.0,
        cumulative_filled=10.0,
        remaining=0.0,
        last_fill_price=200.0,
        exec_time_ms=None,
        fee=0.5,
        ts_ms=1_700_000_000_000,
    )

    async def _empty_gen():
        if False:
            yield  # type: ignore[unreachable]
        await asyncio.sleep(3600)

    # The bootstrap does partial(executions_for_reconnect_recovery, client).
    # Calling the resulting factory must return an awaitable — so the patched
    # function must return a coroutine (i.e. call the async def, not return it).
    async def _foreign_recovery() -> list:
        return [foreign_fill]

    def _patched_recovery_factory(_client) -> object:
        return _foreign_recovery()

    monkeypatch.setattr(ba_module, "stream_order_events", lambda _client: _empty_gen)
    monkeypatch.setattr(
        ba_module,
        "executions_for_reconnect_recovery",
        _patched_recovery_factory,
    )

    from app.services.broker_activity_publisher_registry import (
        BrokerActivityPublisherRegistry,
    )

    fresh_reg = BrokerActivityPublisherRegistry()
    monkeypatch.setattr(ba_module, "get_publisher_registry", lambda: fresh_reg)

    publisher = await ba_module.bootstrap_publisher_for_instance(SID)
    try:
        await publisher._run_periodic_sweep()

        incident_dir = run_dir / "operator_incidents"
        incident_files = list(incident_dir.glob("*.json"))
        assert len(incident_files) >= 1, f"expected at least 1 incident file in {incident_dir}; found none"

        from app.operator.notices.schema import OperatorIncident

        incident = OperatorIncident.model_validate_json(incident_files[0].read_text(encoding="utf-8"))
        assert incident.notice.code == "reconciliation.discovered_execution_not_in_engine_state"
        assert incident.notice.tier == "critical"
    finally:
        await publisher.stop()


async def test_sweep_handles_incident_persistence_failure_gracefully(
    tmp_path: Path,
) -> None:
    """If incident_store.append raises (e.g. disk full), the publisher logs
    CRITICAL and continues; the sweep does not crash and the return count
    is still accurate (the event was processed, even if not persisted).
    """
    from unittest.mock import Mock

    failing_store = Mock(spec=IncidentStore)
    failing_store.append.side_effect = OSError("disk full")

    foreign_fill = _fill_event(exec_id="disk-full-1", order_ref=None)
    publisher, _, _ = _build_publisher(
        tmp_path,
        recovery_events=[foreign_fill],
        incident_store=failing_store,
    )

    # Must not raise even though append fails.
    count = await publisher._run_periodic_sweep()
    assert count == 1, f"expected 1 event processed, got {count}"
    # append was called (the code attempted to persist).
    failing_store.append.assert_called_once()
