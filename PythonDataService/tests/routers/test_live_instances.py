"""Contract tests for the instance-addressed operator console API (ADR 0004).

The host daemon is faked at the client seam (no network); liveness is resolved
server-side and the serialized response carries both `live_binding` and
`evidence_binding` so the client cannot confuse them.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.api_evidence import evidence_request, evidence_response, get_ibkr_api_evidence_recorder
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    append_account_event,
    read_account_events,
    write_account_freeze,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    crash_retired_restart_blocking_binding,
    write_account_instance_binding,
)
from app.engine.live.artifacts import ExecutionRow, ExecutionWriter, TradeRow, TradeWriter
from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateRepo,
    BotRollCallOfferRecord,
    BotRollCallOfferRepo,
    stable_bot_lifecycle_state_path,
    stable_bot_roll_call_offers_path,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo, stable_desired_state_path
from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_wal import IntentWal
from app.engine.live.run_ledger import LiveRunLedger
from app.engine.live.run_ledger import write_ledger as write_live_run_ledger
from app.operator.incidents.safety_halt_notices import build_safety_halt_incident
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    NOTICE_CODE_CONTRACTS,
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)
from app.routers import live_instances
from app.schemas.broker_activity import BrokerActivityRow
from app.schemas.live_runs import LiveBinding
from app.services.live_chart_window import ChartTimeframe, ChartWindowResult
from tests._fixtures.daemon_transport import as_typed_get


def _write_ledger(
    root: Path,
    run_id: str,
    sid: str,
    created_at_ms: int,
    spec_path: Path | None = None,
    account_id: str | None = None,
    strategy_key: str | None = None,
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    payload: dict = {"run_id": run_id, "strategy_instance_id": sid, "created_at_ms": created_at_ms}
    if account_id is not None:
        payload["account_id"] = account_id
    if spec_path is not None:
        payload["strategy_spec_path"] = str(spec_path)
    if strategy_key is not None:
        payload["strategy_key"] = strategy_key
    (run_dir / "run_ledger.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_roll_call_offer(
    artifacts_root: Path,
    *,
    sid: str = "spy_ema_paper",
    run_id: str,
    offer_id: str = "offer-active",
    issued_at_ms: int = 1_783_519_200_000,
    expires_at_ms: int = 1_783_540_500_000,
) -> str:
    BotRollCallOfferRepo(stable_bot_roll_call_offers_path(artifacts_root, sid)).append(
        BotRollCallOfferRecord(
            offer_id=offer_id,
            strategy_instance_id=sid,
            run_id=run_id,
            session_date="2026-07-08",
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            evidence_snapshot={"readiness_verdict": "READY"},
        )
    )
    return offer_id


def _set_startable_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_instances, "_now_ms", lambda: 1_783_533_600_000)


def _write_crash_retired_binding(
    artifacts_root: Path,
    *,
    account_id: str,
    sid: str,
    run_id: str,
    recorded_at_ms: int = 1_700_000_000_000,
) -> None:
    write_account_instance_binding(
        artifacts_root,
        AccountInstanceBinding(
            account_id=account_id,
            strategy_instance_id=sid,
            run_id=run_id,
            bot_order_namespace=f"learn-ai/{sid}/v1",
            lifecycle_state="RETIRED",
            recorded_at_ms=recorded_at_ms,
            source="host_daemon.process_crashed",
        ),
    )


def _append_watchdog_incident(run_dir: Path, *, incident_id: str, message: str, started_at_ms: int) -> None:
    IncidentStore(run_dir).append(
        OperatorIncident(
            incident_id=incident_id,
            category="watchdog",
            started_at_ms=started_at_ms,
            notice=OperatorNotice(
                code="watchdog.flatten_failed",
                tier="critical",
                title="Watchdog flatten failed",
                message=message,
                actionability="routed",
                resolution="Clears after the operator verifies IBKR positions and runs Reconcile.",
                action=OperatorNoticeAction(kind="open_runbook", label="Open runbook", target="watchdog-halt"),
                runbook_slug="watchdog-halt",
                occurred_at_ms=started_at_ms,
            ),
        )
    )


def _append_operator_incident(
    run_dir: Path,
    *,
    incident_id: str,
    category: str,
    code: str,
    title: str,
    message: str,
    started_at_ms: int,
) -> None:
    contract = NOTICE_CODE_CONTRACTS[code]
    if contract.actionability == "routed":
        action = OperatorNoticeAction(
            kind="external_manual_check",
            label="Check external evidence",
            target="operator_external_evidence",
        )
    elif contract.actionability == "actuatable":
        action = OperatorNoticeAction(
            kind="focus_cockpit_action",
            label="Focus action",
            target="reconcile_now",
        )
    else:
        action = OperatorNoticeAction(kind="none")
    IncidentStore(run_dir).append(
        OperatorIncident(
            incident_id=incident_id,
            category=category,
            started_at_ms=started_at_ms,
            notice=OperatorNotice(
                code=code,
                tier=contract.tier,
                title=title,
                message=message,
                actionability=contract.actionability,
                resolution="Clears when the fixture's contract condition is satisfied.",
                remedy_status=contract.remedy_status,
                action=action,
                occurred_at_ms=started_at_ms,
            ),
        )
    )


def test_resolve_symbol_prefers_stock_action_plan_over_signal_stream_and_spec_fixture(tmp_path: Path) -> None:
    """A deployed TSLA action plan must not chart the SPY fixture symbol.

    The deployment-validation spec fixture is pinned to SPY, but live deploys
    can carry the operator-selected stock in ``live_config.action``. The
    cockpit chart/activity resolver must use that stock before falling back to
    the signal stream or fixture.
    """
    root = tmp_path / "live_runs"
    run_dir = root / "run-tsla"
    run_dir.mkdir(parents=True)
    spec_path = tmp_path / "deployment_validation.spec.json"
    spec_path.write_text(json.dumps({"symbols": ["SPY"]}), encoding="utf-8")
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-tsla",
                "strategy_instance_id": "JUN26TSLA",
                "created_at_ms": 100,
                "strategy_spec_path": str(spec_path),
                "live_config": {
                    "symbol": "SPY",
                    "action": {
                        "on_enter": [
                            {
                                "leg_id": "leg_1",
                                "instrument": {"kind": "stock", "underlying": "TSLA"},
                                "position": "long",
                                "qty_ratio": 1,
                            }
                        ],
                        "on_exit": [{"kind": "close_leg", "entry_leg_id": "leg_1"}],
                    },
                    "sizing": {"kind": "FixedShares", "value": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    symbol = live_instances._resolve_symbol(
        root,
        live_binding=None,
        runs=[{"run_dir": str(run_dir)}],
    )

    assert symbol == "TSLA"


def test_resolve_incident_headline_uses_evidence_run_dir_guard(tmp_path: Path) -> None:
    root = tmp_path / "live_runs"
    _write_ledger(root, "run-live", "spy_ema_paper", 200)
    _write_ledger(root, "run-latest", "spy_ema_paper", 100)
    _append_watchdog_incident(
        root / "run-live",
        incident_id="live",
        message="This mismatched live binding must not win.",
        started_at_ms=200,
    )
    _append_watchdog_incident(
        root / "run-latest",
        incident_id="latest",
        message="Latest evidence incident wins.",
        started_at_ms=100,
    )

    notice = live_instances._resolve_incident_headline(
        root,
        LiveBinding(run_id="run-live", run_dir="run-live"),
        [{"run_dir": str(root / "run-latest")}],
    )

    assert notice is not None
    assert notice.message == "Latest evidence incident wins."


def test_resolve_incident_headline_includes_order_and_submit_incidents(tmp_path: Path) -> None:
    root = tmp_path / "live_runs"
    _write_ledger(root, "run-latest", "spy_ema_paper", 100)
    run_dir = root / "run-latest"
    _append_operator_incident(
        run_dir,
        incident_id="order",
        category="order",
        code="order.rejected",
        title="Order rejected",
        message="IBKR rejected the order.",
        started_at_ms=200,
    )
    _append_operator_incident(
        run_dir,
        incident_id="submit",
        category="submit",
        code="submit.uncertain",
        title="Submit uncertain",
        message="Order submission outcome is uncertain.",
        started_at_ms=300,
    )
    _append_operator_incident(
        run_dir,
        incident_id="activity",
        category="activity",
        code="activity.publisher_degraded",
        title="Publisher degraded",
        message="Activity diagnostics are degraded.",
        started_at_ms=400,
    )

    notice = live_instances._resolve_incident_headline(
        root,
        live_binding=None,
        runs=[{"run_dir": str(run_dir)}],
    )

    assert notice is not None
    assert notice.code == "submit.uncertain"


def test_resolve_incident_headline_includes_safety_halt_incidents(tmp_path: Path) -> None:
    from app.engine.live.halt import PoisonedHaltReason, PoisonedHaltTrigger

    root = tmp_path / "live_runs"
    _write_ledger(root, "run-latest", "spy_ema_paper", 100)
    run_dir = root / "run-latest"
    IncidentStore(run_dir).append(
        build_safety_halt_incident(
            strategy_instance_id="spy_ema_paper",
            run_id="run-latest",
            halt_reason=PoisonedHaltReason(
                trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
                halted_at_ms=500,
                last_clean_bar_close_ms=400,
                details={"reason": "foreign_perm_id", "source": "reconciliation_orchestrator"},
            ),
            artifact_path=run_dir / "poisoned.flag",
            log_path=run_dir / "live.log",
        )
    )

    notice = live_instances._resolve_incident_headline(
        root,
        live_binding=None,
        runs=[{"run_dir": str(run_dir)}],
    )

    assert notice is not None
    assert notice.code == "safety_halt.poisoned"


def _write_live_state(root: Path, sid: str, run_id: str, positions: dict[str, int]) -> None:
    live_state_dir = root.parent / "live_state" / sid
    live_state_dir.mkdir(parents=True, exist_ok=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "bot_order_namespace": f"{sid}_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": positions,
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )


def _broker_activity_row(**overrides) -> BrokerActivityRow:
    payload = {
        "seq": 1,
        "ts_ms": 1_700_000_000_000,
        "exec_id": "exec-1",
        "perm_id": 9001,
        "order_ref": "learn-ai/spy_activity/v1:intent-1",
        "symbol": "SPY",
        "side": "BUY",
        "quantity": 1.0,
        "price": 100.0,
        "commission": 1.0,
        "net_amount": -101.0,
        "order_type": "MKT",
        "exec_ts_ms": 1_700_000_000_000,
        "verdict": "expected",
        "template_key": "normal_fill_v1",
        "template_version": 1,
        "headline": "BUY 1 SPY @ $100.00",
        "narrative": "Filled as intended.",
        "reason_codes": ["normal_fill"],
        "engine_overlay": None,
        "divergence_facts": None,
    }
    payload.update(overrides)
    return BrokerActivityRow.model_validate(payload)


class _StatusPublisher:
    is_running = True
    latest_row_ms = 1_700_000_000_000

    def last_persisted_seq(self) -> int:
        return 12


def _write_broker_activity_rows(root: Path, sid: str, rows: list[BrokerActivityRow]) -> None:
    path = root.parent / "live_instances" / sid / "broker_activity.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(row.model_dump_json() + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_intent_events(run_dir: Path, events: list[IntentEvent]) -> None:
    (run_dir / "intent_events.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in events),
        encoding="utf-8",
    )


def _write_repairable_ledger(root: Path, run_id: str, sid: str, created_at_ms: int) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_live_run_ledger(
        run_dir / "run_ledger.json",
        LiveRunLedger(
            run_id=run_id,
            code_sha="abc123",
            strategy_instance_id=sid,
            strategy_spec_path="spec.json",
            strategy_spec_sha256="spec-sha",
            qc_audit_copy_path="qc.py",
            qc_audit_copy_sha256="qc-sha",
            qc_cloud_backtest_id="qc-1",
            account_id="DU123",
            start_date_ms=created_at_ms,
            live_config={},
        ),
    )
    return run_dir


def _write_repair_intent(run_dir: Path, sid: str, *, quantity: int = 100) -> None:
    namespace = f"learn-ai/{sid}/v1"
    intent_id = "intent-repair-1"
    order_ref = f"{namespace}:{intent_id}"
    wal = IntentWal(run_dir / "intent_events.jsonl")
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=intent_id,
        bot_order_namespace=namespace,
        order_ref=order_ref,
        order_spec={
            "symbol": "SPY",
            "action": "BUY" if quantity > 0 else "SELL",
            "quantity": abs(quantity),
            "order_type": "MKT",
        },
        ts_ms=1_782_400_000_000,
    )
    wal.append(
        event_type=IntentEventType.SUBMITTED,
        intent_id=intent_id,
        bot_order_namespace=namespace,
        order_ref=order_ref,
        order_id=42,
        perm_id=9001,
        ts_ms=1_782_400_000_100,
    )


def test_resolve_reconciliation_inputs_returns_intent_events_for_relative_live_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "live_runs"
    run_dir = root / "run-1"
    run_dir.mkdir(parents=True)
    namespace = "learn-ai/spy_rel/v1"
    intent_id = "intent-rel-1"
    wal = IntentWal(run_dir / "intent_events.jsonl")
    wal.append(
        event_type=IntentEventType.PENDING_INTENT,
        intent_id=intent_id,
        bot_order_namespace=namespace,
        order_ref=f"{namespace}:{intent_id}",
        ts_ms=1_700_000_000_000,
    )

    receipt, current_wal_seq, current_run_id, current_namespace, events = live_instances._resolve_reconciliation_inputs(
        root,
        LiveBinding(run_id="run-1", run_dir="run-1"),
    )

    assert receipt is None
    assert current_wal_seq == 1
    assert current_run_id == "run-1"
    assert current_namespace == namespace
    assert [event.event_type for event in events] == [IntentEventType.PENDING_INTENT]


def _write_execution(run_dir: Path, *, ts_ms: int, exec_id: str = "exec-repair-1") -> None:
    writer = ExecutionWriter(run_dir / "executions.parquet")
    writer.append_row(
        ExecutionRow(
            ts_ms=ts_ms,
            exec_id=exec_id,
            perm_id=9001,
            client_order_id="live-42",
            account_id="DU123",
            symbol="SPY",
            fill_quantity=100,
            fill_price=501.25,
            fee=1.0,
            exec_time_ms=ts_ms - 50,
        )
    )
    writer.close()


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        # Mirror the real default env (IBKR_MODE=paper, IBKR_READONLY=false) so
        # start_defaults resolves to place-orders; dedicated tests override.
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.services.deploy_preflight import DeployPreflightSignals

    async def healthy_deploy_preflight(
        _strategy_key: str,
        _account_id: str,
        _instance_id: str,
    ) -> DeployPreflightSignals:
        return DeployPreflightSignals(
            daemon_reachable=True,
            broker_connection_state="connected",
            account_frozen=False,
            account_proven=True,
            fleet_blocks_starts=False,
            strategy_deployable=True,
            instance_already_running=False,
        )

    monkeypatch.setattr(
        live_instances.deploy_preflight_service,
        "gather_deploy_preflight_signals",
        healthy_deploy_preflight,
    )
    from app.main import app

    return app, root


@pytest.fixture(autouse=True)
def clear_ibkr_api_evidence_recorder():
    recorder = get_ibkr_api_evidence_recorder()
    recorder.clear()
    yield
    recorder.clear()


def _set_daemon(monkeypatch: pytest.MonkeyPatch, *, instances: dict | None = None, process: dict | None = None) -> None:
    async def fake_instances(_base_url: str):
        return as_typed_get(instances)

    async def fake_process(_base_url: str, _sid: str):
        return as_typed_get(process)

    monkeypatch.setattr(host_daemon_client, "fetch_instances", fake_instances)
    monkeypatch.setattr(host_daemon_client, "fetch_instance_process", fake_process)


async def test_instance_status_running_exposes_live_binding(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-aaa", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live-aaa", "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "running"
    assert body["process"]["bound_run_id"] == "run-live-aaa"
    assert body["live_binding"]["run_id"] == "run-live-aaa"
    assert body["live_binding"]["source"] == "registry"
    assert body["evidence_binding"]["run_id"] == "run-live-aaa"
    assert body["evidence_binding"]["is_live"] is False
    assert body["desired_state"] is not None


async def test_instance_status_blocks_start_when_crash_recovery_required(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_crash_status"
    account_id = "DU123456"
    run_id = "run-crash-status"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    run_dir = root / run_id
    (run_dir / "verdict_snapshot.json").write_text(json.dumps({"verdict": "paper-only"}), encoding="utf-8")
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": run_id,
                "started_at_ms": 1_700_000_000_000,
                "last_update_ms": 1_700_000_000_000,
                "host_pid": 1,
                "submit_mode_at_start": "live_paper",
                "readonly_at_start": False,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "intent_events.jsonl").write_text("", encoding="utf-8")
    _write_crash_retired_binding(
        root.parent,
        account_id=account_id,
        sid=sid,
        run_id=run_id,
        recorded_at_ms=1_700_000_000_000,
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    start_capability = response.json()["operator_surface"]["host_process"]["start_capability"]
    assert start_capability["enabled"] is False
    assert start_capability["disabled_reason_code"] == "CRASH_RECOVERY_REQUIRED"
    assert start_capability["gate_results"][0]["gate_id"] == "account.crash_recovery"


async def test_status_activity_publisher_resolution_never_bootstraps_missing_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "spy_bootstrap_status"
    bootstrap_called = False

    class Registry:
        def get(self, _sid: str):
            return None

        def registered_at_ms(self, _sid: str) -> int | None:
            return None

    registry = Registry()

    async def fake_bootstrap(strategy_instance_id: str):
        nonlocal bootstrap_called
        assert strategy_instance_id == sid
        bootstrap_called = True

    from app.routers import broker_activity

    monkeypatch.setattr(live_instances, "get_publisher_registry", lambda: registry)
    monkeypatch.setattr(broker_activity, "bootstrap_publisher_for_instance", fake_bootstrap)

    resolved, registered_at_ms = await live_instances._resolve_activity_publisher_for_status(
        sid,
        LiveBinding(run_id="run-live-aaa"),
    )

    assert resolved is None
    assert registered_at_ms is None
    assert bootstrap_called is False


async def test_status_activity_publisher_resolution_reads_registered_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "spy_registered_publisher"
    publisher = _StatusPublisher()

    class Registry:
        def get(self, _sid: str):
            return publisher

        def registered_at_ms(self, _sid: str) -> int | None:
            return 123

    monkeypatch.setattr(live_instances, "get_publisher_registry", lambda: Registry())

    resolved, registered_at_ms = await live_instances._resolve_activity_publisher_for_status(
        sid,
        LiveBinding(run_id="run-live-aaa"),
    )

    assert resolved is publisher
    assert registered_at_ms == 123


async def test_status_is_versioned_from_stored_surface_without_publisher_bootstrap(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_surface_hub_status"
    run_id = "run-surface-hub-status"
    _write_ledger(root, run_id, sid, 100)
    _set_daemon(monkeypatch, process={"state": "idle"})
    bootstrap_called = False

    async def fake_bootstrap(_strategy_instance_id: str):
        nonlocal bootstrap_called
        bootstrap_called = True

    from app.routers import broker_activity

    monkeypatch.setattr(broker_activity, "bootstrap_publisher_for_instance", fake_bootstrap)
    monkeypatch.setattr(live_instances, "_now_ms", lambda: 1_700_000_000_000)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assembled = await live_instances._assemble_instance_surface(sid)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.get(f"/api/live-instances/{sid}/status")).json()
        second = (await client.get(f"/api/live-instances/{sid}/status")).json()
        refreshed = (
            await client.get(f"/api/live-instances/{sid}/status?refresh=true")
        ).json()

    assert {
        key: value
        for key, value in first.items()
        if key not in {"stream_epoch", "surface_version"}
    } == assembled.model_dump(mode="json", exclude={"stream_epoch", "surface_version"})
    assert first["stream_epoch"] == second["stream_epoch"]
    assert first["surface_version"] == second["surface_version"] == refreshed["surface_version"] == 1
    assert first == second
    assert refreshed == second
    assert bootstrap_called is False
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


async def test_surface_hub_lifecycle_owns_activity_publisher_bootstrap(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_surface_lifecycle"
    _write_ledger(root, "run-surface-lifecycle", sid, 100)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})
    started: list[str] = []

    async def fake_start_publisher(strategy_instance_id: str) -> None:
        started.append(strategy_instance_id)

    from app.services.surface_hub import SurfaceHubRegistry

    monkeypatch.setattr(live_instances, "_SURFACE_HUBS", SurfaceHubRegistry())
    monkeypatch.setattr(
        live_instances,
        "_start_surface_activity_publisher",
        fake_start_publisher,
    )

    try:
        await live_instances.start_surface_hubs()
        hub = live_instances._SURFACE_HUBS.get(sid)
        assert hub is not None
        assert hub.is_running is True
        assert hub.latest is not None
        assert started == [sid]

        async def fail_if_status_reassembles(_strategy_instance_id: str):
            raise AssertionError("normal status GET must use the stored snapshot")

        monkeypatch.setattr(
            live_instances,
            "_assemble_instance_surface",
            fail_if_status_reassembles,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/api/live-instances/{sid}/status")
        assert response.status_code == 200
        assert response.json()["stream_epoch"] == hub.latest.stream_epoch
    finally:
        await live_instances.stop_surface_hubs()


async def test_instance_status_uses_recovered_activity_publisher_for_health_and_chart(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_status_publisher"
    run_id = "run-status-publisher"
    _write_ledger(root, run_id, sid, 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": run_id, "pid": 99, "started_at_ms": 100},
    )

    async def fake_resolve_activity_publisher(status_sid: str, live_binding: LiveBinding | None):
        assert status_sid == sid
        assert live_binding is not None
        return _StatusPublisher(), 1_699_999_999_000

    monkeypatch.setattr(
        live_instances,
        "_resolve_activity_publisher_for_status",
        fake_resolve_activity_publisher,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    body = response.json()
    health = body["operator_surface"]["broker_activity_health"]
    assert health["state"] == "ready"
    assert health["headline"] is None
    assert health["facts"]["publisher_registered"] is True
    assert health["facts"]["publisher_running"] is True
    assert health["facts"]["latest_row_seq"] == 12
    broker_writer = {node["id"]: node for node in body["lifecycle_chart"]["global_graph"]["nodes"]}["broker_writer"]
    assert broker_writer["status"] == "passed"


async def test_instance_status_projects_account_owner_submit_events_into_lifecycle_chart(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_account_owner"
    account_id = "DU123456"
    run_id = "run-account-owner"
    namespace = f"learn-ai/{sid}/v1"
    intent_id = "intent-owner-1"
    order_ref = f"{namespace}:{intent_id}"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    append_account_event(
        root.parent,
        account_id,
        {
            "event_type": "account_owner_submit_prepared",
            "created_at_ms": 1_700_000_010_000,
            "diagnostics": {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "intent_id": intent_id,
                "order_ref": order_ref,
            },
        },
    )
    append_account_event(
        root.parent,
        account_id,
        {
            "event_type": "account_owner_submit_accepted",
            "created_at_ms": 1_700_000_010_000,
            "diagnostics": {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "intent_id": intent_id,
                "order_ref": order_ref,
                "order_id": 42,
            },
        },
    )
    append_account_event(
        root.parent,
        account_id,
        {
            "event_type": "account_owner_submit_accepted",
            "created_at_ms": 1_700_000_020_000,
            "diagnostics": {
                "strategy_instance_id": "other_bot",
                "run_id": "run-other",
                "intent_id": "intent-other",
                "order_ref": "learn-ai/other_bot/v1:intent-other",
                "order_id": 99,
            },
        },
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": run_id, "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    submit_nodes = {
        node["id"]: node for node in response.json()["lifecycle_chart"]["subgraphs"]["submit_order"]["nodes"]
    }
    assert submit_nodes["intent_wal"]["status"] == "active"
    assert submit_nodes["intent_wal"]["summary"] == "AccountOwner intent persisted before broker submission."
    assert submit_nodes["intent_wal"]["technical_label"] == "intent_pending"
    assert submit_nodes["place_order"]["status"] == "passed"
    assert submit_nodes["place_order"]["summary"] == "AccountOwner order reached the broker submit boundary."
    assert submit_nodes["place_order"]["technical_label"] == "submit"


async def test_instance_status_skips_malformed_account_events_without_500(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_bad_account_events"
    account_id = "DU123456"
    run_id = "run-bad-account-events"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    account_events_path = root.parent / "accounts" / account_id / "account_events.jsonl"
    account_events_path.parent.mkdir(parents=True)
    account_events_path.write_text("{bad json\n", encoding="utf-8")
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": run_id, "pid": 99, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    broker_nodes = {
        node["id"]: node for node in response.json()["lifecycle_chart"]["subgraphs"]["broker_writer"]["nodes"]
    }
    assert broker_nodes["writer_guard"]["status"] == "unknown"


async def test_instance_status_does_not_project_pre_session_intent_wal_as_current(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_stale_intent_wal"
    account_id = "DU123456"
    run_id = "run-stale-intent-wal"
    namespace = f"learn-ai/{sid}/v1"
    intent_id = "intent-stale-1"
    order_ref = f"{namespace}:{intent_id}"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    _write_live_state(root, sid, run_id, {})
    run_dir = root / run_id
    (run_dir / "intent_events.jsonl").write_text(
        "".join(
            event.model_dump_json() + "\n"
            for event in (
                IntentEvent(
                    seq=1,
                    event_type=IntentEventType.PENDING_INTENT,
                    intent_id=intent_id,
                    bot_order_namespace=namespace,
                    order_ref=order_ref,
                    ts_ms=900,
                    appended_at_ms=1_000,
                ),
                IntentEvent(
                    seq=2,
                    event_type=IntentEventType.SUBMITTED,
                    intent_id=intent_id,
                    bot_order_namespace=namespace,
                    order_ref=order_ref,
                    order_id=42,
                    ts_ms=950,
                    appended_at_ms=1_100,
                ),
            )
        ),
        encoding="utf-8",
    )
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": run_id, "pid": 99, "started_at_ms": 2_000},
    )

    async def fake_resolve_activity_publisher(_sid: str, _live_binding: LiveBinding | None):
        return None, None

    monkeypatch.setattr(
        live_instances,
        "_resolve_activity_publisher_for_status",
        fake_resolve_activity_publisher,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    submit_nodes = {
        node["id"]: node for node in response.json()["lifecycle_chart"]["subgraphs"]["submit_order"]["nodes"]
    }
    assert submit_nodes["intent_wal"]["status"] == "unknown"
    assert submit_nodes["intent_wal"]["summary"] == "Intent WAL evidence is stale for the current live session."
    assert "last_intent_wal_seq=0" in submit_nodes["intent_wal"]["why"]
    assert submit_nodes["place_order"]["status"] == "unknown"
    assert submit_nodes["place_order"]["summary"] == "Intent WAL evidence is stale for the current live session."


async def test_instance_status_dead_is_evidence_only(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-old-bbb", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "idle"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-old-bbb"


async def test_status_start_defaults_seed_strategy_from_ledger_key(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#416: the Start-card defaults seed ``strategy`` from the run ledger's
    ``strategy_key`` so the console never starts from a blank/hardcoded field."""
    app, root = app_with_root
    run_dir = root / "run-keyed"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-keyed",
                "strategy_instance_id": "spy_ema_paper",
                "created_at_ms": 100,
                "strategy_key": "spy_ema_crossover",
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    defaults = response.json()["start_defaults"]
    assert defaults["strategy"] == "spy_ema_crossover"
    # readonly now defaults to False in paper mode (the fixture stub has no
    # explicit mode → treated as paper); see the dedicated paper/live tests.
    assert defaults["readonly"] is False
    assert defaults["hydrate_policy"] == "require"
    assert defaults["max_orders_per_day"] == 2
    assert defaults["ibkr_host"] == "127.0.0.1"


async def test_status_start_defaults_empty_strategy_for_legacy_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy ledger without ``strategy_key`` yields an empty ``strategy`` for
    the operator to supply — the field is present, just unseeded."""
    app, root = app_with_root
    _write_ledger(root, "run-legacy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["strategy"] == ""


async def test_status_start_defaults_carry_redeploy_identity_from_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Start-card defaults also carry the ledger's deploy identity (spec
    path, qc audit copy, qc backtest id, account) so the console can deep-link a
    one-click re-deploy (fresh run_id) to recover a poisoned/halted instance
    without the operator re-typing the deploy form."""
    app, root = app_with_root
    run_dir = root / "run-redeploy"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-redeploy",
                "strategy_instance_id": "spy_ema_paper",
                "created_at_ms": 100,
                "strategy_key": "spy_ema_crossover",
                "strategy_spec_path": "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json",
                "qc_audit_copy_path": "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
                "qc_cloud_backtest_id": "d2fe45a7142e88575f6fbd75229f8681",
                "account_id": "DU1234567",
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    defaults = response.json()["start_defaults"]
    assert defaults["strategy_spec_path"].endswith("spy_ema_crossover.spec.json")
    assert defaults["qc_audit_copy_path"] == "references/qc-shadow/SpyEmaCrossoverAlgorithm.py"
    assert defaults["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert defaults["account_id"] == "DU1234567"


async def test_chart_snapshot_today_returns_bars_and_runs(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 5: ``GET /chart-snapshot`` returns the day's bars + every run
    of the instance that touched the day. ``has_bars`` is True iff the
    response carries at least one bar."""
    app, root = app_with_root

    # Run with sidecar started_at_ms so it counts as "active today".
    # VCR-P3-I: ``today`` here must match the endpoint's _today_ny() — the
    # trading-day date in America/New_York, NOT UTC. Otherwise this test
    # flakes in the ~5h window every day where the two calendars disagree.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")
    today = datetime.now(ny_tz).date()
    today_start_ms = int(datetime(today.year, today.month, today.day, tzinfo=ny_tz).timestamp() * 1000)

    run_dir = root / "run-chart"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-chart",
                "strategy_instance_id": "spy_chart",
                "created_at_ms": today_start_ms,
                "live_config": {"symbol": "SPY"},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "run_id": "run-chart",
                "started_at_ms": today_start_ms + 1_000,
                "last_update_ms": today_start_ms + 60_000,
                "ended_at_ms": None,
                "exit_code": None,
                "exit_reason": None,
                "host_pid": 7,
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_chart/chart-snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["date"] == today.isoformat()
    assert body["symbol"] == "SPY"
    assert body["resolution"] == "1m"
    assert body["has_bars"] is False  # no live aggregator data in this test
    assert isinstance(body["now_ms"], int)
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["run_id"] == "run-chart"
    assert run["started_at_ms"] == today_start_ms + 1_000
    assert run["is_current"] is False
    assert run["color_index"] == 0


async def test_chart_snapshot_rejects_invalid_resolution(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 5: unsupported chart timeframes are a 400, not a silent default."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_chart/chart-snapshot", params={"resolution": "2m"})
    assert response.status_code == 400


async def test_chart_snapshot_historical_window_omits_live_buffer(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5: a historical window request ignores the live aggregator buffer. With
    no persistence data and no runs touching that day, ``has_bars`` is
    False and ``runs`` is empty — the frontend renders the "bars
    unavailable" badge from this state."""
    app, _root = app_with_root
    window_from_ms, window_to_ms = live_instances._ny_session_bounds_ms(date(2026, 1, 5))
    monkeypatch.setattr(live_instances, "_now_ms", lambda: window_to_ms + 60_000)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_chart/chart-snapshot",
            params={"from_ms": window_from_ms, "to_ms": window_to_ms},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2026-01-05"
    assert body["has_bars"] is False
    assert body["runs"] == []


async def test_chart_snapshot_clamps_explicit_future_to_ms_to_server_now(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    server_now_ms = int(datetime(2026, 1, 5, 15, 0, tzinfo=UTC).timestamp() * 1000)
    window_from_ms = server_now_ms - 60_000
    captured: dict[str, int] = {}
    monkeypatch.setattr(live_instances, "_now_ms", lambda: server_now_ms)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async def fake_resolve_chart_window(
        *,
        symbol: str | None,
        timeframe: ChartTimeframe,
        from_ms: int,
        to_ms: int,
        now_ms: int,
        polygon_api_key: str,
        live_aggregator: object,
    ) -> ChartWindowResult:
        _ = (symbol, from_ms, now_ms, polygon_api_key, live_aggregator)
        captured["to_ms"] = to_ms
        return ChartWindowResult(
            bars=[],
            timeframe=timeframe,
            resolution="1m",
            is_streaming=False,
        )

    monkeypatch.setattr(live_instances, "resolve_chart_window", fake_resolve_chart_window)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_chart/chart-snapshot",
            params={"from_ms": window_from_ms, "to_ms": server_now_ms + 30_000},
        )

    assert response.status_code == 200
    assert response.json()["to_ms"] == server_now_ms
    assert captured["to_ms"] == server_now_ms


async def test_chart_snapshot_rejects_malformed_date(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 5: a malformed date string is a 400; never silently coerced."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_chart/chart-snapshot", params={"date": "not-a-date"})
    assert response.status_code == 400


async def test_activity_projection_uses_broker_ledger_for_chart_markers_and_orders(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Activity projection owns the chart-table invariant: broker fills
    render as chart markers from the same same-day ledger rows that feed
    Orders Today / Broker Activity. Duplicate broker replays collapse to one
    marker with a replay count instead of becoming phantom sells."""
    app, root = app_with_root
    sid = "spy_activity"
    _write_ledger(root, "run-activity", sid, 100)
    day = live_instances._today_ny()
    base_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=base_ms - 120_000,
                exec_ts_ms=None,
                exec_id=None,
                perm_id=None,
                order_ref=f"learn-ai/{sid}/v1:intent-pending",
                side="SELL",
                price=None,
                commission=None,
                net_amount=None,
                verdict="engine_only_pending",
                template_key="pending_v1",
                headline="Awaiting broker acknowledgement",
                narrative="Engine emitted intent; no broker ack yet.",
                reason_codes=["pending_acknowledgement"],
            ),
            _broker_activity_row(
                seq=2,
                ts_ms=base_ms,
                exec_ts_ms=base_ms,
                exec_id="exec-buy",
                perm_id=101,
                order_ref=f"learn-ai/{sid}/v1:intent-buy",
                side="BUY",
                price=735.72,
                headline="BUY 1 SPY @ $735.72",
            ),
            _broker_activity_row(
                seq=3,
                ts_ms=base_ms + 60_000,
                exec_ts_ms=base_ms + 60_000,
                exec_id="exec-sell",
                perm_id=102,
                order_ref=f"learn-ai/{sid}/v1:intent-sell",
                side="SELL",
                price=738.06,
                headline="SELL 1 SPY @ $738.06",
            ),
            _broker_activity_row(
                seq=4,
                ts_ms=base_ms + 120_000,
                exec_ts_ms=base_ms + 60_000,
                exec_id="exec-sell",
                perm_id=102,
                order_ref=f"learn-ai/{sid}/v1:intent-sell",
                side="SELL",
                price=738.06,
                verdict="unexpected",
                template_key="duplicate_execution_v1",
                headline="Duplicate broker execution replay",
                narrative="IBKR replayed an execution we already observed.",
                reason_codes=["duplicate_execution"],
            ),
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "America/New_York"
    assert body["has_bars"] is False
    assert body["bars"] == []
    assert [m["side"] for m in body["fill_markers"]] == ["BUY", "SELL"]
    assert body["fill_markers"][1]["replay_count"] == 2
    assert body["fill_markers"][0]["position_effect"] == "Open long"
    assert body["fill_markers"][1]["position_effect"] == "Close long"
    assert body["fill_markers"][0]["chart_ts_ms"] == base_ms
    assert body["fill_markers"][0]["exec_ts_ms"] == base_ms
    assert {o["group"] for o in body["orders_today"]} == {"engine_pending", "resolved"}
    resolved_orders = [o for o in body["orders_today"] if o["group"] == "resolved"]
    assert {o["chart_ts_ms"] for o in resolved_orders} == {base_ms, base_ms + 60_000}
    assert any(w["code"] == "broker_replay_collapsed" for w in body["reconciliation_warnings"])
    fill_event_ids = [row["id"] for row in body["broker_activity_rows"] if row["row_type"] == "fill"]
    assert fill_event_ids.count("exec:exec-sell") == 1
    fill_events = [row for row in body["broker_activity_rows"] if row["row_type"] == "fill"]
    assert {row["ts_ms"] for row in fill_events} == {base_ms, base_ms + 60_000}


async def test_activity_projection_warns_when_lifecycle_order_missing_from_activity(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_lifecycle_gap"
    run_id = "run-lifecycle-gap"
    day = live_instances._today_ny()
    base_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_ledger(root, run_id, sid, base_ms)
    namespace = f"learn-ai/{sid}/v1"
    order_ref = f"{namespace}:intent-gap"
    _write_intent_events(
        root / run_id,
        [
            IntentEvent(
                seq=1,
                event_type=IntentEventType.SUBMITTED,
                intent_id="intent-gap",
                bot_order_namespace=namespace,
                order_ref=order_ref,
                order_id=42,
                ts_ms=base_ms,
                appended_at_ms=base_ms,
            )
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    warnings = {warning["code"]: warning for warning in response.json()["reconciliation_warnings"]}
    assert warnings["lifecycle_order_missing_activity"]["row_ids"] == [order_ref]


async def test_activity_projection_warns_when_account_owner_order_missing_from_activity(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_account_owner_activity_gap"
    account_id = "DU123456"
    run_id = "run-account-owner-activity-gap"
    day = live_instances._today_ny()
    base_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_ledger(root, run_id, sid, base_ms, account_id=account_id)
    order_ref = f"learn-ai/{sid}/v1:intent-owner-gap"
    append_account_event(
        root.parent,
        account_id,
        {
            "event_type": "account_owner_submit_accepted",
            "created_at_ms": base_ms,
            "diagnostics": {
                "strategy_instance_id": sid,
                "run_id": run_id,
                "intent_id": "intent-owner-gap",
                "order_ref": order_ref,
                "order_id": 42,
            },
        },
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    warnings = {warning["code"]: warning for warning in response.json()["reconciliation_warnings"]}
    assert warnings["lifecycle_order_missing_activity"]["row_ids"] == [order_ref]


async def test_activity_projection_warns_when_activity_order_missing_from_lifecycle(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_activity_gap"
    run_id = "run-activity-gap"
    day = live_instances._today_ny()
    base_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_ledger(root, run_id, sid, base_ms)
    order_ref = f"learn-ai/{sid}/v1:intent-activity-only"
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=base_ms,
                exec_ts_ms=base_ms,
                exec_id="exec-activity-gap",
                perm_id=401,
                order_ref=order_ref,
            )
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    warnings = {warning["code"]: warning for warning in response.json()["reconciliation_warnings"]}
    assert warnings["activity_order_missing_lifecycle"]["row_ids"] == [order_ref]


async def test_activity_projection_preserves_backend_fill_verdict(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_unexpected_fill"
    _write_ledger(root, "run-unexpected", sid, 100)
    day = live_instances._today_ny()
    fill_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=fill_ms,
                exec_ts_ms=fill_ms,
                exec_id="exec-foreign",
                perm_id=301,
                order_ref=None,
                verdict="unexpected",
                template_key="unmatched_execution_v1",
                headline="Unmatched broker execution",
                narrative="Broker reported a fill without a matching engine intent.",
                reason_codes=["unmatched_execution"],
            )
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    fill_events = [row for row in response.json()["broker_activity_rows"] if row["row_type"] == "fill"]
    assert len(fill_events) == 1
    assert fill_events[0]["verdict"] == "unexpected"


async def test_activity_projection_matches_evidence_to_specific_order(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_evidence_join"
    _write_ledger(root, "run-evidence-join", sid, 100)
    day = live_instances._today_ny()
    fill_ms = int(datetime(day.year, day.month, day.day, 12, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    buy_ref = f"learn-ai/{sid}/v1:intent-buy"
    sell_ref = f"learn-ai/{sid}/v1:intent-sell"
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=fill_ms,
                exec_ts_ms=fill_ms,
                exec_id="exec-buy",
                perm_id=501,
                order_ref=buy_ref,
                side="BUY",
            ),
            _broker_activity_row(
                seq=2,
                ts_ms=fill_ms + 60_000,
                exec_ts_ms=fill_ms + 60_000,
                exec_id="exec-sell",
                perm_id=502,
                order_ref=sell_ref,
                side="SELL",
            ),
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})
    buy_evidence = get_ibkr_api_evidence_recorder().record(
        source="orders.place_paper_order",
        account_id="DU123",
        symbol="SPY",
        strategy_instance_id=sid,
        request=evidence_request(
            "placeOrder",
            order={"orderRef": buy_ref, "permId": 501, "orderId": 11},
        ),
        response=evidence_response("openOrder", fields={"permId": 501}),
    )
    sell_evidence = get_ibkr_api_evidence_recorder().record(
        source="orders.place_paper_order",
        account_id="DU123",
        symbol="SPY",
        strategy_instance_id=sid,
        request=evidence_request(
            "placeOrder",
            order={"orderRef": sell_ref, "permId": 502, "orderId": 12},
        ),
        response=evidence_response("openOrder", fields={"permId": 502}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    fills = {row["id"]: row for row in response.json()["broker_activity_rows"] if row["row_type"] == "fill"}
    assert [ref["seq"] for ref in fills["exec:exec-buy"]["evidence"]] == [buy_evidence.seq]
    assert [ref["seq"] for ref in fills["exec:exec-sell"]["evidence"]] == [sell_evidence.seq]


async def test_activity_projection_emits_terminal_non_fill_events(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_cancelled"
    _write_ledger(root, "run-cancelled", sid, 100)
    day = live_instances._today_ny()
    cancel_ms = int(datetime(day.year, day.month, day.day, 13, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=cancel_ms,
                exec_ts_ms=None,
                exec_id=None,
                perm_id=401,
                price=None,
                commission=None,
                net_amount=None,
                verdict="expected",
                template_key="cancellation",
                headline="Broker cancelled SPY order",
                narrative="Broker terminal state was cancellation.",
                reason_codes=["cancellation"],
            )
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["orders_today"][0]["group"] == "resolved"
    terminal_events = [row for row in body["broker_activity_rows"] if row["row_type"] == "order_terminal"]
    assert len(terminal_events) == 1
    assert terminal_events[0]["summary"] == "Broker cancelled SPY order"
    assert terminal_events[0]["status"] == "cancellation"


def test_ny_session_bounds_use_next_calendar_midnight_on_dst_transition() -> None:
    start_ms, end_ms = live_instances._ny_session_bounds_ms(date(2026, 3, 8))

    start = datetime.fromtimestamp(start_ms / 1000, tz=live_instances._NY_TZ)
    end = datetime.fromtimestamp(end_ms / 1000, tz=live_instances._NY_TZ)
    assert start == datetime(2026, 3, 8, 0, 0, tzinfo=live_instances._NY_TZ)
    assert end == datetime(2026, 3, 9, 0, 0, tzinfo=live_instances._NY_TZ)
    assert end_ms - start_ms == 23 * 60 * 60 * 1000


def test_lifecycle_activity_refs_use_ny_session_window_not_utc_day(tmp_path: Path) -> None:
    root = tmp_path / "live_runs"
    sid = "spy_evening_session"
    run_id = "run-evening-session"
    day = date(2026, 6, 29)
    event_ms = int(datetime(2026, 6, 29, 21, 0, tzinfo=live_instances._NY_TZ).timestamp() * 1000)
    namespace = f"learn-ai/{sid}/v1"
    order_ref = f"{namespace}:intent-evening"
    _write_ledger(root, run_id, sid, event_ms)
    _write_intent_events(
        root / run_id,
        [
            IntentEvent(
                seq=1,
                event_type=IntentEventType.SUBMITTED,
                intent_id="intent-evening",
                bot_order_namespace=namespace,
                order_ref=order_ref,
                order_id=42,
                ts_ms=event_ms,
                appended_at_ms=event_ms,
            )
        ],
    )
    start_ms, end_ms = live_instances._ny_session_bounds_ms(day)

    refs = live_instances._lifecycle_order_refs_for_activity(
        artifacts_root=root.parent,
        sid=sid,
        day=day,
        runs=[{"run_id": run_id, "run_dir": str(root / run_id), "created_at_ms": event_ms}],
        live_binding=None,
        start_ms=start_ms,
        end_ms=end_ms,
    )

    assert datetime.fromtimestamp(event_ms / 1000, tz=UTC).date() == date(2026, 6, 30)
    assert refs == {order_ref}


async def test_activity_projection_defaults_to_latest_broker_ledger_session(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "june23"
    _write_ledger(root, "run-june23", sid, 100)
    latest_day = live_instances._today_ny() - timedelta(days=2)
    latest_ms = int(
        datetime(
            latest_day.year,
            latest_day.month,
            latest_day.day,
            12,
            0,
            tzinfo=live_instances._NY_TZ,
        ).timestamp()
        * 1000
    )
    _write_broker_activity_rows(
        root,
        sid,
        [
            _broker_activity_row(
                seq=1,
                ts_ms=latest_ms,
                exec_ts_ms=latest_ms,
                exec_id="exec-latest",
                perm_id=201,
                order_ref=f"learn-ai/{sid}/v1:intent-latest",
                side="BUY",
                price=700.00,
                headline="BUY 1 SPY @ $700.00",
            )
        ],
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["session_date"] == latest_day.isoformat()
    assert [m["id"] for m in body["fill_markers"]] == ["exec:exec-latest"]


async def test_activity_projection_surfaces_full_broker_api_evidence_rows(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "spy_evidence"
    _write_ledger(root, "run-evidence", sid, 100)
    _set_daemon(monkeypatch, process={"state": "idle"})
    get_ibkr_api_evidence_recorder().record(
        source="account.fetch_positions",
        account_id="DU123",
        symbol="SPY",
        strategy_instance_id=sid,
        request=evidence_request("reqPositionsAsync"),
        response=evidence_response("position", fields={"row_count": 1}),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/activity")

    assert response.status_code == 200
    body = response.json()
    assert any(ref["request_call"] == "reqPositionsAsync" for ref in body["evidence"])
    assert any(
        row["row_type"] == "broker_evidence"
        and row["display_type"] == "Broker positions refreshed"
        and row["status"] == "Positions refreshed"
        and row["source"] == "account.fetch_positions"
        for row in body["broker_activity_rows"]
    )
    assert not any(row["row_type"] == "endpoint_snapshot" for row in body["broker_activity_rows"])
    assert not any(
        warning["code"] == "broker_position_snapshot_unavailable" for warning in body["reconciliation_warnings"]
    )


async def test_activity_projection_repairs_execution_artifact_without_wal_mutation(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "june25"
    run_id = "run-june25"
    fill_ms = int(datetime(2026, 6, 25, 15, 0, tzinfo=UTC).timestamp() * 1000)
    run_dir = _write_repairable_ledger(root, run_id, sid, fill_ms)
    _write_repair_intent(run_dir, sid)
    _write_execution(run_dir, ts_ms=fill_ms)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/live-instances/{sid}/activity",
            params={"session_date": "2026-06-25"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_date"] == "2026-06-25"
    assert [row["id"] for row in body["fill_markers"]] == ["exec:exec-repair-1"]
    assert body["fill_markers"][0]["chart_ts_ms"] == fill_ms
    fill_rows = [row for row in body["broker_activity_rows"] if row["row_type"] == "fill"]
    assert len(fill_rows) == 1
    assert fill_rows[0]["ts_ms"] == fill_ms
    assert fill_rows[0]["source"] == "activity_repair_projection"
    assert fill_rows[0]["display_type"] == "Broker fill"
    assert fill_rows[0]["visible_row_id"] == "fill:exec:exec-repair-1"
    assert not (root.parent / "live_instances" / sid / "broker_activity.jsonl").exists()


async def test_activity_projection_adds_closed_trade_summary_without_double_counting(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    sid = "june25_closed"
    run_id = "run-june25-closed"
    entry_ms = int(datetime(2026, 6, 25, 14, 30, tzinfo=UTC).timestamp() * 1000)
    exit_ms = int(datetime(2026, 6, 25, 15, 0, tzinfo=UTC).timestamp() * 1000)
    run_dir = _write_repairable_ledger(root, run_id, sid, entry_ms)
    _write_repair_intent(run_dir, sid)
    _write_execution(run_dir, ts_ms=exit_ms, exec_id="exec-closed-1")
    writer = TradeWriter(run_dir / "trades.parquet")
    writer.append_row(
        TradeRow(
            entry_time_ms=entry_ms,
            exit_time_ms=exit_ms,
            entry_price=500.0,
            exit_price=501.25,
            pnl_points=1.25,
        )
    )
    writer.close()
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/live-instances/{sid}/activity",
            params={"session_date": "2026-06-25"},
        )

    assert response.status_code == 200
    rows = response.json()["broker_activity_rows"]
    fill_rows = [row for row in rows if row["row_type"] == "fill"]
    summary_rows = [row for row in rows if row["row_type"] == "closed_trade_summary"]
    assert len(fill_rows) == 1
    assert len(summary_rows) == 1
    assert summary_rows[0]["display_type"] == "Closed trade"
    assert summary_rows[0]["source_label"] == "Trade history"
    assert summary_rows[0]["constituent_fill_ids"] == []


async def test_active_dates_returns_run_dates_with_no_bars_marker(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 6: dates the instance ran on but pre-date persistence still
    appear in the picker with ``has_bars=False``."""
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    app, root = app_with_root
    run_dir = root / "run-day1"
    run_dir.mkdir(parents=True)
    started_ms = int(datetime(2026, 1, 5, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-day1",
                "strategy_instance_id": "spy_dates",
                "created_at_ms": started_ms,
            }
        ),
        encoding="utf-8",
    )
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-day1",
            started_at_ms=started_ms,
            last_update_ms=started_ms + 60_000,
            ended_at_ms=started_ms + 3_600_000,
            exit_code=0,
            host_pid=11,
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_dates/active-dates")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["date"] == "2026-01-05"
    assert entry["run_count"] == 1
    assert entry["has_bars"] is False


async def test_active_dates_counts_every_utc_day_a_run_overlaps(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 6 (PR #483 review): a run spanning midnight UTC must appear on
    BOTH dates the picker shows, not just its start day. Anchoring solely
    on started_at_ms previously hid the later day."""
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    app, root = app_with_root
    run_dir = root / "run-overnight"
    run_dir.mkdir(parents=True)
    started_ms = int(datetime(2026, 1, 5, 22, 0, tzinfo=UTC).timestamp() * 1000)
    ended_ms = int(datetime(2026, 1, 7, 4, 0, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-overnight",
                "strategy_instance_id": "spy_overnight",
                "created_at_ms": started_ms,
            }
        ),
        encoding="utf-8",
    )
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-overnight",
            started_at_ms=started_ms,
            last_update_ms=ended_ms,
            ended_at_ms=ended_ms,
            exit_code=0,
            host_pid=12,
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_overnight/active-dates")

    assert response.status_code == 200
    body = response.json()
    dates = [entry["date"] for entry in body]
    # Spans 2026-01-05 22:00 UTC → 2026-01-07 04:00 UTC, so all three UTC
    # days must appear.
    assert dates == ["2026-01-05", "2026-01-06", "2026-01-07"]
    for entry in body:
        assert entry["run_count"] == 1


async def test_chart_snapshot_filters_trades_to_requested_utc_day(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 (PR #483 review): a multi-day run's trades from other UTC
    days must NOT project onto a per-date /chart-snapshot response."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    app, root = app_with_root
    run_dir = root / "run-spans"
    run_dir.mkdir(parents=True)
    day_a_ms = int(datetime(2026, 1, 5, 14, 30, tzinfo=UTC).timestamp() * 1000)
    day_b_ms = int(datetime(2026, 1, 6, 14, 30, tzinfo=UTC).timestamp() * 1000)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-spans",
                "strategy_instance_id": "spy_spans",
                "created_at_ms": day_a_ms,
                "live_config": {"symbol": "SPY"},
            }
        ),
        encoding="utf-8",
    )
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import RunStatusSidecar

    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-spans",
            started_at_ms=day_a_ms,
            last_update_ms=day_b_ms,
            ended_at_ms=day_b_ms + 3_600_000,
            exit_code=0,
            host_pid=14,
        ),
    )
    # Trades from two different UTC days under the same run.
    table = pa.table(
        {
            "entry_time_ms": pa.array([day_a_ms, day_b_ms], type=pa.int64()),
            "exit_time_ms": pa.array([day_a_ms + 60_000, day_b_ms + 60_000], type=pa.int64()),
            "entry_price": pa.array([100.0, 200.0], type=pa.float64()),
            "exit_price": pa.array([101.0, 201.0], type=pa.float64()),
            "pnl_points": pa.array([1.0, 1.0], type=pa.float64()),
        }
    )
    pq.write_table(table, run_dir / "trades.parquet")
    window_from_ms = int(datetime(2026, 1, 5, 0, 0, tzinfo=UTC).timestamp() * 1000)
    window_to_ms = int(datetime(2026, 1, 6, 0, 0, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr(live_instances, "_now_ms", lambda: window_to_ms + 60_000)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/live-instances/spy_spans/chart-snapshot",
            params={"from_ms": window_from_ms, "to_ms": window_to_ms},
        )

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert len(runs) == 1
    trades = runs[0]["trades"]
    assert len(trades) == 1
    assert trades[0]["entry_time_ms"] == day_a_ms


async def test_active_dates_rejects_invalid_resolution(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 6: only 1m / 5s accepted at the boundary."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_dates/active-dates", params={"resolution": "10s"})
    assert response.status_code == 400


async def test_status_provenance_attests_the_run_identity(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """The status carries what the run's content-addressed identity attests to —
    the hashed inputs (commit, spec+SHA, QC audit copy+SHA, backtest id, account)
    — so the console can explain the hashes ("what this proves") not dump them."""
    app, root = app_with_root
    run_dir = root / "run-prov"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-prov",
                "schema_version": "1.2",
                "strategy_instance_id": "spy_ema_paper",
                "strategy_key": "spy_ema_crossover",
                "code_sha": "c0ffee1234deadbeef",
                "strategy_spec_path": "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json",
                "strategy_spec_sha256": "aaaaspec",
                "qc_audit_copy_path": "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
                "qc_audit_copy_sha256": "bbbbaudit",
                "qc_cloud_backtest_id": "d2fe45a7142e88575f6fbd75229f8681",
                "account_id": "DU1234567",
                "start_date_ms": 1714838400000,
                "created_at_ms": 1714838400500,
                "live_config": {"symbol": "SPY", "consolidator_period_min": 15},
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    prov = response.json()["provenance"]
    assert prov["run_id"] == "run-prov"
    assert prov["code_sha"] == "c0ffee1234deadbeef"
    assert prov["strategy_spec_sha256"] == "aaaaspec"
    assert prov["qc_audit_copy_sha256"] == "bbbbaudit"
    assert prov["qc_cloud_backtest_id"] == "d2fe45a7142e88575f6fbd75229f8681"
    assert prov["account_id"] == "DU1234567"
    assert prov["start_date_ms"] == 1714838400000
    # live_config is part of the identity hash, so it must be in the provenance.
    assert prov["live_config"] == {"symbol": "SPY", "consolidator_period_min": 15}


async def test_status_exposes_symbol_from_ledger_live_config(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slice 2: the chart card needs the traded symbol to drop its 'SPY' default.
    Symbol is sourced from the ledger's ``live_config.symbol`` so two strategies
    that differ only in symbol don't have to plumb it through the URL."""
    app, root = app_with_root
    run_dir = root / "run-sym"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-sym",
                "strategy_instance_id": "qqq_strategy",
                "created_at_ms": 1714838400500,
                "live_config": {"symbol": "QQQ", "consolidator_period_min": 1},
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qqq_strategy/status")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "QQQ"


async def test_status_symbol_is_null_when_nothing_deployed(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ledger → no symbol. The frontend must treat ``null`` as 'unknown' and
    not fall back to a hardcoded ticker — the prior 'SPY' default was the bug
    Slice 2 closes."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/ghost_instance/status")

    assert response.status_code == 200
    assert response.json()["symbol"] is None


async def test_status_symbol_is_null_when_live_config_missing_symbol(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy ledger that predates the symbol field must not crash — the field
    surfaces ``null`` and the UI handles that explicitly."""
    app, root = app_with_root
    run_dir = root / "run-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy",
                "strategy_instance_id": "legacy_strategy",
                "created_at_ms": 1714838400500,
                # No live_config — pre-symbol ledger.
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/legacy_strategy/status")

    assert response.status_code == 200
    assert response.json()["symbol"] is None


async def test_status_provenance_none_when_nothing_deployed(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/ghost_instance/status")

    assert response.status_code == 200
    assert response.json()["provenance"] is None


async def test_status_last_exit_surfaces_the_specific_halt_trigger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted run leaves a poisoned.flag carrying the SPECIFIC safety trigger.
    The status surfaces it (+ forensic details) so the console can explain *what*
    the engine detected, not just a generic 'Safety halt'."""
    from app.engine.live.halt import (
        PoisonedHaltReason,
        PoisonedHaltTrigger,
        write_poisoned_flag,
    )
    from app.engine.live.run_status import write_run_status
    from app.schemas.live_runs import ExitReason, RunStatusSidecar

    app, root = app_with_root
    _write_ledger(root, "run-halt", "spy_ema_paper", 100)
    run_dir = root / "run-halt"
    write_run_status(
        run_dir,
        RunStatusSidecar(
            run_id="run-halt",
            started_at_ms=1,
            last_update_ms=2,
            ended_at_ms=3,
            exit_code=1,
            exit_reason=ExitReason.fatal_halt,
            host_pid=7,
        ),
    )
    write_poisoned_flag(
        run_dir,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1_700_000_000_000,
            last_clean_bar_close_ms=0,
            details={"client_order_id": "live-42", "symbol": "SPY"},
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit["exit_reason"] == "fatal_halt"
    assert last_exit["halt_trigger"] == "outside_mutation"
    assert last_exit["halt_at_ms"] == 1_700_000_000_000
    assert last_exit["halt_detail"]["symbol"] == "SPY"


async def test_emergency_flatten_works_without_live_binding(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """The account-wide flatten reaches the latest run's daemon emergency-flatten
    even with NO live binding (the binding-gated console FLATTEN command can't) —
    exactly the post-halt/poison case where flattening matters most."""
    app, root = app_with_root
    _write_ledger(root, "run-flat", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})  # not running -> no live binding

    captured: dict = {}

    async def fake_flatten(base_url: str, run_id: str, payload: dict) -> dict:
        captured["run_id"] = run_id
        captured["payload"] = payload
        return {"accepted": True, "process": {"state": "idle"}}

    monkeypatch.setattr(host_daemon_client, "emergency_flatten_run", fake_flatten)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert captured["run_id"] == "run-flat"
    assert captured["payload"] == {"account": "DU123", "confirm": True}


async def test_emergency_flatten_requires_confirm(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-flat2", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/emergency-flatten",
            json={"account": "DU123", "confirm": False},
        )

    assert response.status_code == 400


async def test_emergency_flatten_404_when_instance_has_no_run(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/ghost_instance/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 404


async def test_status_start_defaults_redeploy_fields_empty_for_legacy_ledger(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy ledgers missing the deploy fields yield empty strings (the deploy
    form then asks for them) rather than erroring."""
    app, root = app_with_root
    _write_ledger(root, "run-legacy-redeploy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    defaults = response.json()["start_defaults"]
    assert defaults["strategy_spec_path"] == ""
    assert defaults["qc_cloud_backtest_id"] == ""
    assert defaults["account_id"] == ""


async def test_instance_status_unreachable_daemon_is_not_guessed(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-x", "spy_ema_paper", 10)
    _set_daemon(monkeypatch, process=None)  # daemon unreachable -> None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["state"] == "unreachable"
    assert body["live_binding"] is None
    assert body["evidence_binding"]["run_id"] == "run-x"


async def test_list_instances_merges_daemon_and_disk(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema-1", "spy_ema_paper", 100)
    _write_ledger(root, "run-vwap-1", "spy_vwap_shadow", 100)
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "spy_ema_paper",
                    "run_id": "run-ema-1",
                    "run_dir": str(root / "run-ema-1"),
                    "process": {"state": "running", "run_id": "run-ema-1"},
                }
            ],
            "fetched_at_ms": 1,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances")

    assert response.status_code == 200
    rows = {row["strategy_instance_id"]: row for row in response.json()}
    assert set(rows) == {"spy_ema_paper", "spy_vwap_shadow"}
    assert rows["spy_ema_paper"]["process_state"] == "running"
    assert rows["spy_ema_paper"]["bound_run_id"] == "run-ema-1"
    # Disk-only instance: daemon reachable but not managing it -> offline, no bound run.
    assert rows["spy_vwap_shadow"]["process_state"] == "offline"
    assert rows["spy_vwap_shadow"]["bound_run_id"] is None
    assert rows["spy_vwap_shadow"]["latest_run_id"] == "run-vwap-1"


async def test_bot_catalog_returns_backend_composed_rows(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema-1", "spy_ema_paper", 100)
    _write_live_state(root, "spy_ema_paper", "run-ema-1", {"SPY": 5})
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "spy_ema_paper",
                    "run_id": "run-ema-1",
                    "run_dir": str(root / "run-ema-1"),
                    "process": {"state": "running", "run_id": "run-ema-1"},
                }
            ],
            "fetched_at_ms": 1,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"bots", "roll_call", "evening_report"}
    assert body["roll_call"]["on_duty"] == 1
    assert body["evening_report"]["rows"][0]["strategy_instance_id"] == "spy_ema_paper"
    row = body["bots"][0]
    assert row["strategy_instance_id"] == "spy_ema_paper"
    assert row["name"] == "spy_ema_paper"
    assert row["trading_mode"] == "paper"
    assert row["symbols"] == ["SPY"]
    assert row["metrics"]["current_exposure"] == "SPY 5"
    assert row["metrics"]["open_positions"] == 1
    assert row["metrics"]["pnl"] == {"realized": None, "unrealized": None, "total": None}
    assert isinstance(row["status_label"], str)
    assert isinstance(row["status_detail"], str)
    assert row["status_tone"] in {"positive", "warning", "danger", "neutral"}
    assert row["status_label"] == "On duty"
    assert row["daily_lifecycle"]["phase"] == "ON_DUTY"
    assert row["daily_lifecycle"]["display_status"] == "On duty"
    assert row["last_run_label"] == "Not yet proven"
    assert row["last_run_result"] == "UNKNOWN"
    assert row["last_run_detail"] == "No completed run has been recorded for this bot."


async def test_bot_catalog_offloads_run_scan(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    called = False

    async def fake_to_thread(func, *args, **kwargs):
        nonlocal called
        called = True
        return func(*args, **kwargs)

    monkeypatch.setattr(live_instances.asyncio, "to_thread", fake_to_thread)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    assert called is True


async def test_bot_catalog_reuses_run_scan_for_status_rows(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema-1", "spy_ema_paper", 100)
    _write_ledger(root, "run-vwap-1", "spy_vwap_shadow", 110)
    calls = 0
    real_scan = live_instances._scan_runs_by_instance

    def counted_scan(scan_root: Path):
        nonlocal calls
        calls += 1
        return real_scan(scan_root)

    monkeypatch.setattr(live_instances, "_scan_runs_by_instance", counted_scan)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    assert {row["strategy_instance_id"] for row in response.json()["bots"]} == {
        "spy_ema_paper",
        "spy_vwap_shadow",
    }
    assert calls == 1


async def test_roll_call_tick_persists_start_offer_and_catalog_reads_it(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-offer", "spy_ema_paper", 100, strategy_key="spy_ema")
    _set_startable_now(monkeypatch)
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "spy_ema_paper",
                    "run_id": "run-offer",
                    "run_dir": str(root / "run-offer"),
                    "process": {"state": "idle", "run_id": "run-offer"},
                }
            ],
            "fetched_at_ms": 1,
        },
        process={"state": "idle"},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        roll_call = await client.post("/api/live-instances/roll-call")
        catalog = await client.get("/api/live-instances/catalog")

    assert roll_call.status_code == 200
    body = roll_call.json()
    assert body["summary"]["ready"] == 1
    assert body["offers"][0]["strategy_instance_id"] == "spy_ema_paper"
    row = catalog.json()["bots"][0]
    assert row["daily_lifecycle"]["display_status"] == "Ready"
    assert row["daily_lifecycle"]["primary_action"]["offer_id"] == body["offers"][0]["offer_id"]
    assert row["start_request"]["roll_call_offer_id"] is None
    assert catalog.json()["roll_call"]["ready"] == 1
    assert catalog.json()["evening_report"]["summary"].endswith("0 retired")


async def test_status_marks_bot_sick_bay_for_terminal_account_condition(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_sick_bot"
    run_id = "run-sick-bot"
    _write_ledger(root, run_id, sid, 100, account_id="DU1234567")
    write_account_instance_binding(
        root.parent,
        AccountInstanceBinding(
            account_id="DU1234567",
            strategy_instance_id=sid,
            run_id=run_id,
            bot_order_namespace=f"learn-ai/{sid}/v1",
            lifecycle_state="RETIRED",
            recorded_at_ms=1_780_000_002_500,
            source="host_daemon.ended_without_status",
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/live-instances/{sid}/status")

    assert response.status_code == 200
    lifecycle = response.json()["daily_lifecycle"]
    assert lifecycle["display_status"] == "Sick bay"
    assert lifecycle["attention_badge"] == "Sick bay"
    assert lifecycle["conditions"][0] == {
        "scope": "bot",
        "severity": "critical",
        "title": "Bot ended without status",
        "detail": (
            f"{sid} exited without a run-status receipt for run {run_id}. "
            "Retire & Replace is required."
        ),
        "owner_label": f"Bot {sid}",
        "cure_action": "retire_replace",
        "cure_label": "Retire & Replace",
    }
    assert {
        "scope": "account",
        "severity": "warning",
        "title": "Account evidence not yet proven",
        "detail": "No account-level reconciliation receipt exists for DU1234567.",
        "owner_label": "Account DU1234567",
        "cure_action": "reconcile_now",
        "cure_label": "Run account reconcile",
    } in lifecycle["conditions"]


async def test_bot_catalog_authors_closed_lifecycle_status_and_last_run_labels(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-error", "spy_error_bot", 100)
    _write_run_status(root, "run-error", ended_at_ms=200, exit_code=3, exit_reason="exception")
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    row = response.json()["bots"][0]
    assert row["status_label"] in {
        "Off duty",
        "Ready",
        "On duty",
        "Clocking out",
        "Sick bay",
        "Off roster",
        "Retired",
    }
    assert row["status_label"] not in {"Blocked", "Degraded", "Unknown", "Fresh run only"}
    assert row["daily_lifecycle"]["display_status"] == row["status_label"]
    assert row["status_label"] != row["status_detail"]
    assert row["last_run_label"] == "Exited with error"
    assert row["last_run_result"] == "EXITED_WITH_ERROR"
    assert row["last_run_detail"] == "Previous run exited with an error: runtime exception. Exit code 3."


async def test_bot_catalog_labels_status_backed_fatal_halt_as_safety_halt(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-halt", "spy_halt_bot", 100)
    _write_run_status(root, "run-halt", ended_at_ms=200, exit_code=1, exit_reason="fatal_halt")
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    row = response.json()["bots"][0]
    assert row["last_run_label"] == "Safety halt"
    assert row["last_run_result"] == "HALT_TRIGGERED"
    assert row["last_run_detail"] == "Previous run stopped on a safety halt: Safety halt. Exit code 1."


async def test_bot_catalog_does_not_surface_legacy_fresh_run_only_flag(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_repairable_ledger(root, "run-stopped", "stopped_bot", 100)
    DesiredStateRepo(stable_desired_state_path(root.parent, "stopped_bot")).set(
        DesiredState.STOPPED,
        updated_by="operator",
        reason="retired run",
        now_ms=200,
    )
    _set_daemon(
        monkeypatch,
        instances={
            "instances": [
                {
                    "strategy_instance_id": "stopped_bot",
                    "run_id": "run-stopped",
                    "run_dir": str(root / "run-stopped"),
                    "process": {"state": "exited", "run_id": "run-stopped"},
                }
            ],
            "fetched_at_ms": 1,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")
        status_response = await client.get("/api/live-instances/stopped_bot/status")

    assert response.status_code == 200
    row = response.json()["bots"][0]
    assert row["strategy_instance_id"] == "stopped_bot"
    assert row["only_fresh_run_available"] is False
    assert row["status_label"] in {
        "Off duty",
        "Ready",
        "On duty",
        "Clocking out",
        "Sick bay",
        "Off roster",
        "Retired",
    }
    assert status_response.status_code == 200
    assert status_response.json()["lifecycle_chart"]["only_fresh_run_available"] is True


async def test_bot_catalog_does_not_fallback_unknown_symbol_to_instance_id(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-vwap-1", "spy_vwap_shadow", 100)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    row = response.json()["bots"][0]
    assert row["strategy_instance_id"] == "spy_vwap_shadow"
    assert row["symbols"] == []


async def test_bot_catalog_trading_mode_comes_from_configured_mode(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-mode", "spy_live_mode", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="live",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    assert response.json()["bots"][0]["trading_mode"] == "live"


async def test_delete_instance_soft_deletes_bot_from_catalog_list_and_status(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-delete", "delete-me", 100)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        delete_response = await client.request(
            "DELETE",
            "/api/live-instances/delete-me",
            json={"reason": "bad deploy spec"},
        )
        catalog_response = await client.get("/api/live-instances/catalog")
        list_response = await client.get("/api/live-instances")
        status_response = await client.get("/api/live-instances/delete-me/status")

    assert delete_response.status_code == 200
    delete_body = delete_response.json()
    assert delete_body["mode"] == "soft"
    assert delete_body["deleted_run_ids"] == ["run-delete"]
    assert delete_body["reason"] == "bad deploy spec"
    assert (root.parent / "live_state" / "delete-me" / "bot_deletion.json").is_file()
    assert live_instances._scan_runs_by_instance(root)["delete-me"][0]["run_id"] == "run-delete"
    assert catalog_response.json()["bots"] == []
    assert list_response.json() == []
    assert status_response.status_code == 410
    assert status_response.json()["detail"]["reason_code"] == "BOT_SOFT_DELETED"


async def test_delete_instance_refuses_active_process(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-active", "active-bot", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-active", "pid": 42})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request("DELETE", "/api/live-instances/active-bot")

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "BOT_PROCESS_ACTIVE"
    assert not (root.parent / "live_state" / "active-bot" / "bot_deletion.json").exists()


async def test_delete_instance_allows_retired_bot_when_daemon_unreachable(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-retired", "retired-bot", 100)
    BotLifecycleStateRepo(stable_bot_lifecycle_state_path(root.parent, "retired-bot")).retire(
        now_ms=200,
        updated_by="operator",
        reason="Retire & Replace",
    )
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request("DELETE", "/api/live-instances/retired-bot")

    assert response.status_code == 200
    assert response.json()["deleted_run_ids"] == ["run-retired"]
    assert (root.parent / "live_state" / "retired-bot" / "bot_deletion.json").is_file()


async def test_soft_deleted_instance_reappears_after_new_run_id(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-old", "redeployable-bot", 100)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        delete_response = await client.request("DELETE", "/api/live-instances/redeployable-bot")

    assert delete_response.status_code == 200
    _write_ledger(root, "run-new", "redeployable-bot", 200)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog_response = await client.get("/api/live-instances/catalog")
        status_response = await client.get("/api/live-instances/redeployable-bot/status")

    assert catalog_response.status_code == 200
    assert [row["strategy_instance_id"] for row in catalog_response.json()["bots"]] == ["redeployable-bot"]
    assert status_response.status_code == 200
    assert status_response.json()["evidence_binding"]["run_id"] == "run-new"


async def test_start_run_rejects_soft_deleted_run_before_daemon(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-soft-deleted", "soft-deleted-bot", 100)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        delete_response = await client.request("DELETE", "/api/live-instances/soft-deleted-bot")
        start_response = await client.post("/api/live-instances/runs/run-soft-deleted/start", json={})

    assert delete_response.status_code == 200
    assert start_response.status_code == 410
    assert start_response.json()["detail"]["reason_code"] == "BOT_SOFT_DELETED"
    assert start_response.json()["detail"]["run_id"] == "run-soft-deleted"
    assert called is False


async def test_bot_catalog_continues_when_deletion_marker_unreadable(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-unreadable-marker", "marker-bot", 100)
    marker = root.parent / "live_state" / "marker-bot" / "bot_deletion.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def fake_read_text(path: Path, *args, **kwargs):
        if path == marker:
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/catalog")

    assert response.status_code == 200
    assert [row["strategy_instance_id"] for row in response.json()["bots"]] == ["marker-bot"]


async def test_instance_status_rejects_invalid_id(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/evil$/status")

    assert response.status_code == 400


async def test_status_includes_namespace_attributed_broker_slice(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-brk", "spy_ema_paper", 100)
    live_state_dir = root.parent / "live_state" / "spy_ema_paper"
    live_state_dir.mkdir(parents=True)
    (live_state_dir / "live_state.json").write_text(
        json.dumps(
            {
                "strategy_instance_id": "spy_ema_paper",
                "run_id": "run-brk",
                "bot_order_namespace": "spy_ema_ns",
                "ib_client_id": 42,
                "expected_position_by_symbol": {"SPY": 100},
                "pending_intents": [{"symbol": "SPY"}],
                "last_processed_bar_ms": 1,
                "last_artifact_flush_ms": 1,
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    broker = response.json()["broker"]
    assert broker["bot_order_namespace"] == "spy_ema_ns"
    assert broker["owned_positions"] == {"SPY": 100}  # engine's own namespace tally
    assert broker["pending_order_count"] == 1


async def test_status_broker_absent_without_sidecar(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-nobrk", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.json()["broker"] is None


async def test_account_fleet_flags_residual_contamination(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> dict[str, int]:
        return {"SPY": 137}  # 37 unexplained

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    body = response.json()
    assert body["verdict"] == "contaminated"
    assert body["residual"] == {"SPY": 37}
    assert body["explained_total"] == {"SPY": 100}
    assert any(b["strategy_instance_id"] == "spy_ema" for b in body["explained_by_instance"])


async def test_account_fleet_unknown_without_broker(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-ema", "spy_ema", 100)
    _write_live_state(root, "spy_ema", "run-ema", {"SPY": 100})

    async def fake_net() -> None:
        return None

    monkeypatch.setattr(live_instances, "_fetch_net_positions", fake_net)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/account")

    assert response.json()["verdict"] == "unknown"


async def test_instance_commands_returns_bound_run_timeline(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-cmd", "spy_ema_paper", 100)
    commands = root / "run-cmd" / "commands"
    commands.mkdir()
    (commands / "command.1.RECONCILE.pending.json").write_text(
        json.dumps({"seq": 1, "verb": "RECONCILE", "payload": {}}), encoding="utf-8"
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-cmd", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    body = response.json()
    assert body["poll_interval_ms"] == 1000  # server-provided
    assert "pending" not in body and "acks" not in body  # canonical entries[] shape
    assert [e["seq"] for e in body["entries"]] == [1]
    assert body["entries"][0]["status"] == "queued"


async def test_instance_commands_empty_without_live_binding(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/commands")

    assert response.json() == {"entries": [], "poll_interval_ms": 1000}


async def test_issue_one_shot_command_queues_on_bound_run(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/commands", json={"verb": "RECONCILE"})

    assert response.status_code == 200
    assert response.json()["verb"] == "RECONCILE"
    queued = list((root / "run-os" / "commands").glob("command.*.RECONCILE.pending.json"))
    assert len(queued) == 1


async def test_issue_command_rejects_intent_verbs(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-os2", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-os2", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/commands", json={"verb": "PAUSE"})

    assert response.status_code == 400  # PAUSE is the intent knob, not a one-shot command


async def test_issue_command_without_live_binding_conflicts(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/commands", json={"verb": "FLATTEN"})

    assert response.status_code == 409


async def test_status_transports_engine_readiness_when_live(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-rdy", "spy_ema_paper", 100)
    (root / "run-live-rdy" / "readiness.json").write_text(
        json.dumps(
            {
                "kind": "live_readiness",
                "as_of_ms": 5,
                "source": "engine",
                "verdict": "READY",
                "summary": "ready",
                "gates": [{"name": "desired_state", "status": "pass", "severity": "hard", "detail": "RUNNING"}],
            }
        ),
        encoding="utf-8",
    )
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-rdy", "pid": 1})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "live_readiness"
    assert readiness["source"] == "engine"  # engine-authored, transported verbatim
    assert readiness["verdict"] == "READY"


async def test_status_derives_start_readiness_when_dead(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-dead-rdy", "spy_ema_paper", 50)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    readiness = response.json()["readiness"]
    assert readiness["kind"] == "start_readiness"
    assert readiness["source"] == "backend_derived"
    assert readiness["live_readiness_available"] is False


async def test_status_includes_spec_derived_decision_column_descriptors(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.engine.strategy.spec import schema as spec_schema

    fixture = Path(spec_schema.__file__).parent / "fixtures" / "spy_ema_crossover.spec.json"
    app, root = app_with_root
    _write_ledger(root, "run-desc", "spy_ema_paper", 100, spec_path=fixture)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    body = response.json()
    cols = {c["name"]: c for c in body["decision_columns"]}
    assert {"ema5", "ema10", "rsi"} <= set(cols)
    assert cols["rsi"]["label"] == "RSI"
    assert cols["ema5"]["label"] == "EMA 5"
    assert cols["ema5"]["format"] == "decimal"
    # No decisions.parquet written -> latest_decision is None, descriptors still resolve.
    assert body["latest_decision"] is None
    assert body["latest_signal_tone"] == "neutral"


async def test_status_includes_backend_authored_latest_signal_tone(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    app, root = app_with_root
    run_dir = root / "run-signal-tone"
    _write_ledger(root, "run-signal-tone", "spy_ema_paper", 100)
    decisions_dir = run_dir / "decisions.parquet"
    decisions_dir.mkdir()
    first = pa.table(
        {
            "bar_close_ms": pa.array([1_700_000_000_000], type=pa.int64()),
            "signal": pa.array(["HOLD"], type=pa.string()),
        }
    )
    second = pa.table(
        {
            "bar_close_ms": pa.array([1_700_000_060_000], type=pa.int64()),
            "signal": pa.array(["EXIT"], type=pa.string()),
        }
    )
    pq.write_table(first, decisions_dir / "part-000001.parquet")
    pq.write_table(second, decisions_dir / "part-000002.parquet")
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    body = response.json()
    assert body["latest_decision"]["signal"] == "EXIT"
    assert body["latest_signal_tone"] == "warn"


async def test_set_desired_state_actuates_live_binding(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ccc", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ccc", "pid": 7})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause", "updated_by": "operator", "reason": "risk"},
        )

    assert response.status_code == 200
    body = response.json()
    # 1. durable intent written first
    assert body["durable"]["state"] == "PAUSED"
    # 2. live actuation queued on the bound run
    assert body["actuation"]["actuated"] is True
    assert body["actuation"]["run_id"] == "run-live-ccc"
    assert body["actuation"]["command_seq"] is not None
    queued = list((root / "run-live-ccc" / "commands").glob("command.*.PAUSE.pending.json"))
    assert len(queued) == 1


async def test_resume_rejects_when_account_is_frozen(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-freeze", "spy_ema_paper", 100, account_id="DU123456")
    write_account_freeze(
        root.parent,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause", "updated_by": "operator"},
        )
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "resume", "updated_by": "operator"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["disabled_reason_code"] == "ACCOUNT_FROZEN"


async def test_resume_receipt_names_crash_recovery_next_rung(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_resume_crash"
    account_id = "DU123456"
    run_id = "run-resume-crash"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    run_dir = root / run_id
    (run_dir / "verdict_snapshot.json").write_text(json.dumps({"verdict": "paper-only"}), encoding="utf-8")
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": run_id,
                "started_at_ms": 1_700_000_000_000,
                "last_update_ms": 1_700_000_000_000,
                "host_pid": 1,
                "submit_mode_at_start": "live_paper",
                "readonly_at_start": False,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "intent_events.jsonl").write_text("", encoding="utf-8")
    _write_crash_retired_binding(
        root.parent,
        account_id=account_id,
        sid=sid,
        run_id=run_id,
        recorded_at_ms=1_700_000_000_000,
    )
    DesiredStateRepo(stable_desired_state_path(root.parent, sid)).set(
        DesiredState.STOPPED,
        updated_by="test",
        reason="regression",
        now_ms=1_700_000_000_000,
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/desired-state",
            json={"action": "resume", "updated_by": "operator", "reason": "Resume"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "RUNNING"
    receipt = body["rung_receipt"]
    assert receipt["code"] == "mutation.next_blocking_rung"
    assert receipt["tier"] == "critical"
    assert receipt["rung_id"] == "host_process"
    assert receipt["source_codes"] == ["CRASH_RECOVERY_REQUIRED"]
    assert receipt["title"] == (
        "Stop latch cleared. The bot still won't run: previous host runner crashed "
        "— record crash-recovery evidence"
    )
    assert receipt["actionability"] == "actuatable"
    assert receipt["action"] == {
        "kind": "focus_cockpit_action",
        "label": "Record recovery override",
        "target": "crash_recovery_override",
    }


async def test_set_desired_state_without_live_binding_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)  # no live process

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "stop", "updated_by": "op"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "STOPPED"
    assert body["actuation"]["actuated"] is False
    assert "durable only" in body["actuation"]["detail"]


async def test_set_desired_state_live_but_run_dir_not_visible_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon reports a live process but its run dir is not visible under this
    service's root: never claim a phantom actuation (a command written here
    would never be seen by the engine polling its real dir)."""
    app, _root = app_with_root
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-ghost", "pid": 5})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert "not visible locally" in body["actuation"]["detail"]


async def test_set_desired_state_enqueue_failure_is_durable_only(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live-ddd", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live-ddd", "pid": 8})

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(live_instances.CommandChannel, "write_from_operator", fail_write)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/desired-state",
            json={"action": "pause"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["durable"]["state"] == "PAUSED"
    assert body["actuation"]["actuated"] is False
    assert body["actuation"]["run_id"] == "run-live-ddd"
    assert "failed to enqueue live command" in body["actuation"]["detail"]


# ── deploy / create (ADR 0006) ───────────────────────────────────────


def _deploy_body() -> dict:
    return {
        "strategy_spec_path": "PythonDataService/spec.json",
        "qc_audit_copy_path": "references/qc-shadow/A.py",
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1700000000000,
        "strategy_instance_id": "spy_ema_paper",
        # VCR-0001 / Phase 1 — explicit sizing is required at the deploy
        # boundary. Safe canary is the deploy-form default.
        "live_config": {
            "symbol": "SPY",
            "sizing": {"kind": "FixedShares", "value": 1},
        },
    }


def _set_connected_broker_account(
    monkeypatch: pytest.MonkeyPatch,
    account_id: str | None = "DU111",
    *,
    known: bool = True,
) -> None:
    async def fake_connected_account() -> tuple[str | None, bool]:
        return account_id, known

    monkeypatch.setattr(
        live_instances,
        "_fetch_broker_connected_account",
        fake_connected_account,
    )


async def test_deploy_instance_created_returns_201(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root
    captured: dict = {}

    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run-new"
    assert body["created"] is True
    assert captured["account_id"] == "DU111"


async def test_deploy_instance_rejects_when_account_is_frozen(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")
    write_account_freeze(
        root.parent,
        AccountFreezeEvidence(
            account_id="DU111",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    called = False

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ACCOUNT_FROZEN"
    assert called is False


async def test_deploy_and_start_stages_after_legacy_stopped_latch_check(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    DesiredStateRepo(
        stable_desired_state_path(root.parent, "spy_ema_paper")
    ).set(
        DesiredState.STOPPED,
        updated_by="test",
        reason="regression",
        now_ms=1_700_000_000_000,
    )
    captured: dict = {}

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201
    assert captured["start"] is False


async def test_deploy_and_start_rejects_backend_preflight_blocker(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _root = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")
    from app.services.deploy_preflight import DeployPreflightSignals

    async def blocked_preflight(
        _strategy_key: str,
        _account_id: str,
        _instance_id: str,
    ) -> DeployPreflightSignals:
        return DeployPreflightSignals(
            daemon_reachable=True,
            broker_connection_state="disconnected",
            account_frozen=False,
            account_proven=True,
            fleet_blocks_starts=False,
            strategy_deployable=True,
            instance_already_running=False,
        )

    monkeypatch.setattr(
        live_instances.deploy_preflight_service,
        "gather_deploy_preflight_signals",
        blocked_preflight,
    )
    called = False

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "DEPLOY_PREFLIGHT_BLOCKED"
    assert detail["blockers"][0]["id"] == "broker_disconnected"
    assert called is False


def _write_identity_source_run(root: Path, sid: str, symbol: str) -> None:
    run_dir = root / f"run-{symbol.lower()}"
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "strategy_instance_id": sid,
                "created_at_ms": 1_700_000_000_000,
                "live_config": {
                    "symbol": "SPY",
                    "sizing": {"kind": "FixedShares", "value": 1},
                    "action": {
                        "on_enter": [
                            {
                                "leg_id": "leg_1",
                                "instrument": {"kind": "stock", "underlying": symbol},
                                "position": "long",
                                "qty_ratio": 1,
                            }
                        ],
                        "on_exit": [{"kind": "close_leg", "entry_leg_id": "leg_1"}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _write_live_state(root, sid, run_dir.name, {})


async def test_deploy_and_start_rejects_unconfirmed_identity_incoherence(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_identity_source_run(root, "spy_ema_paper", "MU")
    called = False

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "IDENTITY_COHERENCE_UNCONFIRMED"
    assert detail["gate_id"] == "deploy.identity_coherence"
    assert detail["evidence"] == [
        {
            "label": "inherited_symbol",
            "value": "MU",
            "source": "run_ledger.live_config.action stock target",
        },
        {"label": "signal_stream", "value": "SPY", "source": "live_config.symbol"},
    ]
    assert called is False


async def test_deploy_and_start_allows_confirmed_identity_incoherence(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_identity_source_run(root, "spy_ema_paper", "MU")
    captured: dict = {}

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True
    body["identity_coherence_confirmation"] = {
        "inherited_symbol": "MU",
        "signal_stream": "SPY",
        "action_plan_symbol": None,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201
    assert captured["account_id"] == "DU111"
    assert captured["start"] is False
    assert "identity_coherence_confirmation" not in captured


async def test_deploy_and_start_ignores_soft_deleted_inherited_identity(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_identity_source_run(root, "spy_ema_paper", "MU")
    _set_daemon(monkeypatch, instances={"instances": [], "fetched_at_ms": 1}, process={"state": "idle"})
    captured: dict = {}

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        delete_response = await client.request("DELETE", "/api/live-instances/spy_ema_paper")
        deploy_response = await client.post("/api/live-instances", json=body)

    assert delete_response.status_code == 200
    assert deploy_response.status_code == 201
    assert captured["strategy_instance_id"] == "spy_ema_paper"
    assert captured["start"] is False


async def test_deploy_and_start_ignores_request_inherited_symbol_for_new_instance(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    captured: dict = {}

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True
    body["inherited_symbol"] = "MU"
    body["inherited_symbol_source"] = "stale redeploy URL"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201
    assert captured["strategy_instance_id"] == "spy_ema_paper"
    assert captured["start"] is False


async def test_deploy_and_start_rejects_unconfirmed_nonflat_exposure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_ledger(root, "run-exposure", "spy_ema_paper", 1_700_000_000_000)
    _write_live_state(root, "spy_ema_paper", "run-exposure", {"SPY": 5})
    called = False

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXPOSURE_COHERENCE_UNCONFIRMED"
    assert detail["gate_id"] == "deploy.exposure_coherence"
    assert detail["evidence"] == {
        "posture": "LONG",
        "pending_order_count": 0,
        "owned_positions": {"SPY": 5},
        "source": "live_state.expected_position_by_symbol",
        "strategy_instance_id": "spy_ema_paper",
        "run_id": "run-exposure",
    }
    assert called is False


async def test_deploy_and_start_allows_confirmed_nonflat_exposure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_ledger(root, "run-exposure", "spy_ema_paper", 1_700_000_000_000)
    _write_live_state(root, "spy_ema_paper", "run-exposure", {"SPY": -3})
    captured: dict = {}

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True
    body["exposure_coherence_confirmation"] = {
        "posture": "SHORT",
        "pending_order_count": 0,
        "owned_positions": {"SPY": -3},
        "strategy_instance_id": "spy_ema_paper",
        "run_id": "run-exposure",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201
    assert captured["account_id"] == "DU111"
    assert captured["start"] is False
    assert "exposure_coherence_confirmation" not in captured


async def test_deploy_and_start_rejects_unknown_existing_exposure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_ledger(root, "run-exposure", "spy_ema_paper", 1_700_000_000_000)
    called = False

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXPOSURE_COHERENCE_UNCONFIRMED"
    assert detail["evidence"]["posture"] == "UNKNOWN"
    assert detail["evidence"]["owned_positions"] == {}
    assert detail["evidence"]["run_id"] == "run-exposure"
    assert called is False


async def test_deploy_and_start_allows_flat_existing_exposure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _set_startable_now(monkeypatch)
    _set_connected_broker_account(monkeypatch, "DU111")
    _write_ledger(root, "run-exposure", "spy_ema_paper", 1_700_000_000_000)
    _write_live_state(root, "spy_ema_paper", "run-exposure", {"SPY": 0})

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)
    body = _deploy_body()
    body["start"] = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201


async def test_deploy_instance_uses_connected_broker_account_when_request_omits_account(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root
    captured: dict = {}
    body = _deploy_body()
    body.pop("account_id")

    _set_connected_broker_account(monkeypatch, "DU222")

    async def fake_deploy(_base_url: str, payload: dict) -> dict:
        captured.update(payload)
        return {"run_id": "run-new", "run_dir": "/runs/run-new", "created": True, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 201
    assert captured["account_id"] == "DU222"


async def test_deploy_instance_rejects_stale_client_account_id(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    _set_connected_broker_account(monkeypatch, "DU222")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise AssertionError("daemon deploy must not be called on account mismatch")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert "account mismatch" in response.json()["detail"].lower()


async def test_deploy_instance_rejects_blank_legacy_account_id(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root
    body = _deploy_body()
    body["account_id"] = " "

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise AssertionError("daemon deploy must not be called for invalid payload")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 422


async def test_deploy_instance_rejects_unknown_public_deploy_field(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root
    body = _deploy_body()
    body["operator_account_override"] = "DU111"

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise AssertionError("daemon deploy must not be called for invalid payload")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=body)

    assert response.status_code == 422


async def test_deploy_instance_rejects_when_connected_broker_account_unavailable(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    _set_connected_broker_account(monkeypatch, None, known=False)

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise AssertionError("daemon deploy must not be called without broker account")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert "connected broker account unavailable" in response.json()["detail"].lower()


async def test_deploy_instance_idempotent_returns_200(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"run_id": "run-existing", "run_dir": "/runs/run-existing", "created": False, "start": None}

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 200
    assert response.json()["created"] is False


async def test_deploy_instance_dirty_tree_propagates_409(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(409, "Working tree is dirty; commit or stash before deploying.")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    assert "dirty" in response.json()["detail"].lower()


async def test_deploy_instance_daemon_unreachable_propagates_503(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(503, "host daemon unreachable: connection refused")

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 503


async def test_deploy_instance_invalid_payload_returns_502(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A schema-invalid deploy payload from the daemon is an upstream contract
    failure → 502, not a 500 that makes the data plane look broken."""
    app, _ = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        return {"unexpected": "shape"}  # missing run_id/run_dir/created

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 502


async def test_qc_audit_copies_invalid_payload_returns_502(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed (non-None) listing from the daemon must not 500 or silently
    read as an empty list — surface it as a gateway error."""
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get({"scope_root": 123, "entries": "not-a-list"})  # wrong types

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 502


async def test_qc_audit_copies_passthrough(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get({"scope_root": "references/qc-shadow", "entries": ["references/qc-shadow/A.py"]})

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == ["references/qc-shadow/A.py"]


async def test_qc_audit_copies_failclosed_to_empty(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return as_typed_get(None)  # daemon unreachable

    monkeypatch.setattr(host_daemon_client, "fetch_qc_audit_copies", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/qc-audit-copies")

    assert response.status_code == 200
    assert response.json()["entries"] == []


# ── daemon-health proxy (PRD #619-C P2 — /health is auth-gated) ──────


def _idle_health() -> dict:
    """A minimal HostRunnerHealth payload shaped for the schema validator."""
    return {
        "ok": True,
        "repo_root": "/repo",
        "live_runs_root": "/repo/artifacts/live_runs",
        "fetched_at_ms": 1700000000000,
        "process": {
            "state": "idle",
            "run_id": None,
            "pid": None,
            "started_at_ms": None,
            "ended_at_ms": None,
            "exit_code": None,
            "command": [],
            "log_path": None,
            "message": None,
        },
        "daemon_boot_id": "boot-abc",
    }


async def test_daemon_health_forwards_envelope(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser cannot send X-Live-Runner-Token. The data plane probes
    the daemon and forwards the parsed envelope so the cockpit / deploy
    form can render Daemon up = OK."""
    from app.engine.live.daemon_transport import DaemonResult
    from app.schemas.live_runs import HostRunnerHealth

    app, _ = app_with_root
    payload = _idle_health()

    async def fake_fetch(_base_url: str):
        return DaemonResult.connected(daemon_boot_id="boot-abc"), HostRunnerHealth.model_validate(payload)

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["daemon_boot_id"] == "boot-abc"


async def test_daemon_health_auth_failed_surfaces_as_502(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale/rotated token would have silently shown the deploy form
    "Live engine unavailable" before this route existed. Surfacing 502
    lets the connectivity strip distinguish auth from unreachable."""
    from app.engine.live.daemon_transport import DaemonResult

    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return DaemonResult.auth_failed(status=401, detail="bad token"), None

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 502
    assert "token" in response.json()["detail"].lower()


async def test_daemon_health_unreachable_surfaces_as_503(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon process down → 503, matching the existing operation-error
    map's remediation copy for "live engine unavailable"."""
    import httpx

    from app.engine.live.daemon_transport import DaemonResult

    app, _ = app_with_root

    async def fake_fetch(_base_url: str):
        return (
            DaemonResult.from_httpx_exception(httpx.ConnectError("connection refused")),
            None,
        )

    monkeypatch.setattr(host_daemon_client, "fetch_health", fake_fetch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/daemon-health")

    assert response.status_code == 503


async def test_daemon_diagnose_always_200_and_instance_route_projects_report(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.schemas.daemon_diagnostics import (
        DaemonDiagnosticHeadline,
        DaemonDiagnosticReport,
        DaemonInstanceDiagnostic,
    )

    app, _ = app_with_root
    instance_a = DaemonInstanceDiagnostic(
        strategy_instance_id="BOT_A",
        overall_status="fail",
        dominant_condition="not_started",
        headline=DaemonDiagnosticHeadline(
            title="Bot has not been started in the live engine",
            summary="The daemon registry has no managed process for this strategy instance.",
        ),
        checks=[],
    )
    instance_b = instance_a.model_copy(
        update={
            "strategy_instance_id": "BOT_B",
            "dominant_condition": "instance_healthy",
            "overall_status": "pass",
        }
    )
    report = DaemonDiagnosticReport(
        overall_status="fail",
        transport="UNREACHABLE",
        dominant_condition="unreachable",
        headline=DaemonDiagnosticHeadline(
            title="Live engine is not answering",
            summary="The data plane could not reach the host live engine.",
        ),
        checks=[],
        per_instance=[instance_a, instance_b],
        fetched_at_ms=1_700_000_000_000,
    )

    class FakeDiagnosticsService:
        async def report(self, *, strategy_instance_id: str | None = None):
            if strategy_instance_id:
                return report.model_copy(
                    update={
                        "per_instance": [
                            item for item in report.per_instance if item.strategy_instance_id == strategy_instance_id
                        ]
                    }
                )
            return report

    monkeypatch.setattr(
        live_instances,
        "get_daemon_diagnostics_service",
        lambda: FakeDiagnosticsService(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        global_response = await client.get("/api/live-instances/daemon-diagnose")
        instance_response = await client.get("/api/live-instances/BOT_A/daemon-diagnose")

    assert global_response.status_code == 200
    assert global_response.json()["transport"] == "UNREACHABLE"
    assert [item["strategy_instance_id"] for item in global_response.json()["per_instance"]] == [
        "BOT_A",
        "BOT_B",
    ]
    assert instance_response.status_code == 200
    assert instance_response.json()["per_instance"] == [instance_a.model_dump(mode="json")]


async def test_renew_daemon_lease_forwards_to_host_daemon(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root
    payload = {
        **_idle_health(),
        "lease_status": "CONNECTED",
        "last_lease_written_at_ms": 1700000000100,
    }
    calls: list[str] = []

    async def fake_renew(base_url: str):
        calls.append(base_url)
        return payload

    monkeypatch.setattr(host_daemon_client, "renew_control_plane_lease", fake_renew)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/daemon-health/renew-lease")

    assert response.status_code == 200
    body = response.json()
    assert body["lease_status"] == "CONNECTED"
    assert body["last_lease_written_at_ms"] == 1700000000100
    assert calls


# ── start / stop proxy (ADR 0007 — token forwarded server-side) ──────


def _running_process(run_id: str) -> dict:
    return {
        "state": "running",
        "run_id": run_id,
        "strategy_instance_id": "spy_ema_paper",
        "pid": 4242,
        "started_at_ms": 1700000000000,
        "ended_at_ms": None,
        "exit_code": None,
        "command": ["python", "-m", "app.engine.live.run", "start"],
        "log_path": "/runs/host_daemon.log",
        "message": "Host runner process is active.",
    }


async def test_start_run_forwards_and_returns_action(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    seen: dict = {}

    async def fake_start(_base_url: str, run_id: str, payload: dict) -> dict:
        seen["run_id"] = run_id
        seen["payload"] = payload
        return {"accepted": True, "process": _running_process(run_id)}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-abc/start",
            json={"readonly": False, "hydrate_policy": "optional", "strategy": "spy_ema_crossover"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["process"]["state"] == "running"
    # The proxy forwards the run_id and the start knobs verbatim to the daemon.
    assert seen["run_id"] == "run-abc"
    assert seen["payload"]["readonly"] is False
    assert seen["payload"]["hydrate_policy"] == "optional"
    lifecycle = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")
    ).read()
    assert lifecycle is not None
    assert lifecycle.phase == "ON_DUTY"
    assert lifecycle.active_run_id == "run-abc"


async def test_start_run_rejects_when_poisoned(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0013 amendment 2026-06-22: a stale ``start_capability.enabled=true``
    must not bypass the poisoned-flag gate. The data plane re-evaluates."""
    app, root = app_with_root
    _write_ledger(root, "run-poisoned", "spy_ema_paper", 100)
    (root / "run-poisoned" / "poisoned.flag").write_text('{"trigger":"x"}', encoding="utf-8")
    _set_daemon(monkeypatch, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-poisoned/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "STOPPED_REQUIRES_REDEPLOY"
    assert called is False  # daemon never reached


async def test_start_run_ignores_legacy_stopped_latch_when_roll_call_offer_is_current(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-perma", "spy_ema_paper", 100)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-perma")
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})
    repo = DesiredStateRepo(stable_desired_state_path(root.parent, "spy_ema_paper"))
    repo.set(
        DesiredState.STOPPED,
        updated_by="operator",
        reason="legacy_stop_latch",
        now_ms=200,
    )
    forwarded_payload: dict | None = None

    async def fake_start(_base_url: str, run_id: str, payload: dict) -> dict:
        nonlocal forwarded_payload
        forwarded_payload = payload
        return {"accepted": True, "process": _running_process(run_id)}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-perma/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert forwarded_payload is not None
    assert "roll_call_offer_id" not in forwarded_payload
    record = repo.read()
    assert record is not None
    assert record.desired_state is DesiredState.RUNNING
    assert record.reason == "daily_lifecycle.start"
    assert record.updated_by == "system"


async def test_start_run_rejects_retired_bot_even_with_stale_roll_call_offer(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-retired", "spy_ema_paper", 100)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-retired")
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})
    BotLifecycleStateRepo(stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")).retire(
        now_ms=300,
        updated_by="operator",
        reason="machinery replaced",
    )
    called = False

    async def fake_start(*_args, **_kwargs) -> dict:
        nonlocal called
        called = True
        return {"accepted": True, "process": _running_process("run-retired")}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-retired/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "BOT_RETIRED"
    assert called is False


async def test_start_run_rejects_when_account_is_frozen(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-freeze-start", "spy_ema_paper", 100, account_id="DU123456")
    write_account_freeze(
        root.parent,
        AccountFreezeEvidence(
            account_id="DU123456",
            reason="watchdog.flatten_failed",
            source="watchdog_halt_executor",
            recorded_at_ms=1_700_000_000_000,
            operator_next_step="CHECK_IBKR",
        ),
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-freeze-start/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ACCOUNT_FROZEN"
    assert called is False


async def test_start_run_rejects_when_crash_recovery_required(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_crash_start"
    account_id = "DU123456"
    run_id = "run-crash-start"
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    _write_crash_retired_binding(
        root.parent,
        account_id=account_id,
        sid=sid,
        run_id=run_id,
        recorded_at_ms=1_700_000_000_000,
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/live-instances/runs/{run_id}/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "CRASH_RECOVERY_REQUIRED"
    assert called is False


async def test_crash_recovery_override_records_later_audited_override(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_crash_override"
    account_id = "DU123456"
    run_id = "run-crash-override"
    crash_recorded_at_ms = 1_700_000_000_000
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    _write_crash_retired_binding(
        root.parent,
        account_id=account_id,
        sid=sid,
        run_id=run_id,
        recorded_at_ms=crash_recorded_at_ms,
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/crash-recovery-override",
            json={"confirm_account_flat": True, "approved_by": "operator"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["event_type"] == "account_audited_override_recorded"
    assert body["account_id"] == account_id
    assert body["strategy_instance_id"] == sid
    assert body["run_id"] == run_id
    assert body["recorded_at_ms"] > crash_recorded_at_ms

    events = read_account_events(root.parent, account_id)
    override_events = [event for event in events if event["event_type"] == "account_audited_override_recorded"]
    assert override_events
    assert override_events[-1]["strategy_instance_id"] == sid
    assert override_events[-1]["ts_ms"] > crash_recorded_at_ms
    assert (
        crash_retired_restart_blocking_binding(
            root.parent,
            account_id=account_id,
            strategy_instance_id=sid,
        )
        is None
    )


async def test_crash_recovery_override_survives_post_commit_receipt_failure(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    sid = "spy_crash_override_degraded"
    account_id = "DU123456"
    run_id = "run-crash-override-degraded"
    crash_recorded_at_ms = 1_700_000_000_000
    _write_ledger(root, run_id, sid, 100, account_id=account_id)
    _write_crash_retired_binding(
        root.parent,
        account_id=account_id,
        sid=sid,
        run_id=run_id,
        recorded_at_ms=crash_recorded_at_ms,
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("daemon unreachable while resolving receipt")

    monkeypatch.setattr(live_instances, "_mutation_rung_receipts_for_instance", _boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/crash-recovery-override",
            json={"confirm_account_flat": True, "approved_by": "operator"},
        )

    # The durable override succeeded; the receipt projection failed. Degrade to a
    # receipt-less 200 rather than 500-ing a completed mutation whose retry 409s.
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["rung_receipt"] is None
    assert body["rung_receipt_warnings"] == []

    override_events = [
        event
        for event in read_account_events(root.parent, account_id)
        if event["event_type"] == "account_audited_override_recorded"
    ]
    assert override_events
    assert (
        crash_retired_restart_blocking_binding(
            root.parent,
            account_id=account_id,
            strategy_instance_id=sid,
        )
        is None
    )


async def test_start_run_rejects_when_daemon_already_running(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-live", "pid": 7})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-live/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ALREADY_RUNNING"


async def test_start_run_rejects_when_host_service_unreachable(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-x", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process=None)  # daemon unreachable

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-x/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "HOST_SERVICE_OFFLINE"


async def test_start_run_proceeds_when_idle_and_not_poisoned(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must not block a legitimately startable bot — IDLE, no
    durable STOPPED, no poison, daemon reachable -> proceed to the daemon."""
    app, root = app_with_root
    _write_ledger(root, "run-fresh", "spy_ema_paper", 100)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-fresh")
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async def fake_start(_base_url: str, run_id: str, _payload: dict) -> dict:
        return {"accepted": True, "process": _running_process(run_id)}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-fresh/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True


async def test_start_run_requires_current_roll_call_offer(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-no-offer", "spy_ema_paper", 100)
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-no-offer/start", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ROLL_CALL_OFFER_REQUIRED"
    assert called is False


async def test_start_run_rejects_roll_call_offer_for_different_run(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-offered", "spy_ema_paper", 100)
    _write_ledger(root, "run-requested", "spy_ema_paper", 110)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-offered")
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})
    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-requested/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "ROLL_CALL_OFFER_RUN_MISMATCH"
    assert called is False


async def test_start_run_rejects_at_effective_stop_boundary(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-stop-boundary", "spy_ema_paper", 100)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-stop-boundary")
    monkeypatch.setattr(live_instances, "_now_ms", lambda: 1_783_540_500_000)
    _set_daemon(monkeypatch, process={"state": "idle"})

    called = False

    async def fake_start(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"accepted": True, "process": {}}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-stop-boundary/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "SESSION_STOP_REACHED"
    assert called is False


async def test_start_run_rolls_back_desired_state_when_daemon_rejects(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-reject", "spy_ema_paper", 100)
    offer_id = _write_roll_call_offer(root.parent, run_id="run-reject")
    _set_startable_now(monkeypatch)
    _set_daemon(monkeypatch, process={"state": "idle"})
    repo = DesiredStateRepo(stable_desired_state_path(root.parent, "spy_ema_paper"))
    previous = repo.set(
        DesiredState.PAUSED,
        updated_by="operator",
        reason="halt_clear_pending",
        now_ms=200,
    )

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(409, "launch rejected")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/runs/run-reject/start",
            json={"roll_call_offer_id": offer_id},
        )

    assert response.status_code == 409
    assert repo.read() == previous


async def test_stop_run_forwards_and_returns_action(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, root = app_with_root

    async def fake_stop(_base_url: str, run_id: str, _payload: dict) -> dict:
        proc = _running_process(run_id)
        proc["state"] = "stopping"
        return {"accepted": True, "process": proc}

    monkeypatch.setattr(host_daemon_client, "stop_run", fake_stop)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/stop", json={"force": False})

    assert response.status_code == 200
    assert response.json()["process"]["state"] == "stopping"
    lifecycle = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")
    ).read()
    assert lifecycle is not None
    assert lifecycle.phase == "OFF_DUTY"
    assert lifecycle.active_run_id is None


async def test_end_day_now_resolves_live_binding_and_stops_current_run(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-live", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live", "pid": 99, "started_at_ms": 100},
    )
    seen: dict = {}

    async def fake_stop(_base_url: str, run_id: str, payload: dict) -> dict:
        seen["run_id"] = run_id
        seen["payload"] = payload
        proc = _running_process(run_id)
        proc["state"] = "stopping"
        return {"accepted": True, "process": proc}

    monkeypatch.setattr(host_daemon_client, "stop_run", fake_stop)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/end-day-now", json={"force": False})

    assert response.status_code == 200
    assert seen == {"run_id": "run-live", "payload": {"force": False}}
    lifecycle = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")
    ).read()
    assert lifecycle is not None
    assert lifecycle.phase == "OFF_DUTY"


async def test_lifecycle_roster_updates_daily_projection(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-idle", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/lifecycle/roster",
            json={"on_roster": False, "updated_by": "operator", "reason": "rest day"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["strategy_instance_id"] == "spy_ema_paper"
    assert body["lifecycle"]["on_roster"] is False
    assert body["lifecycle"]["display_status"] == "Off roster"


async def test_retire_and_replace_requires_flat_attestation_and_off_duty(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-idle", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "running", "run_id": "run-idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_attestation = await client.post(
            "/api/live-instances/spy_ema_paper/retire-and-replace",
            json={"confirm_account_flat": False},
        )
        running = await client.post(
            "/api/live-instances/spy_ema_paper/retire-and-replace",
            json={"confirm_account_flat": True},
        )

    assert missing_attestation.status_code == 409
    assert missing_attestation.json()["detail"]["reason_code"] == "ACCOUNT_FLAT_ATTESTATION_REQUIRED"
    assert running.status_code == 409
    assert running.json()["detail"]["reason_code"] == "BOT_ON_DUTY"


async def test_retire_and_replace_rejects_unreachable_daemon(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-idle", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/retire-and-replace",
            json={"confirm_account_flat": True},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["reason_code"] == "HOST_SERVICE_OFFLINE"
    assert (
        BotLifecycleStateRepo(
            stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")
        ).read()
        is None
    )


async def test_retire_and_replace_marks_bot_retired(
    app_with_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, root = app_with_root
    _write_ledger(root, "run-idle", "spy_ema_paper", 100)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/spy_ema_paper/retire-and-replace",
            json={"confirm_account_flat": True, "updated_by": "operator", "reason": "machinery replaced"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle"]["phase"] == "RETIRED"
    assert body["lifecycle"]["display_status"] == "Retired"
    lifecycle = BotLifecycleStateRepo(
        stable_bot_lifecycle_state_path(root.parent, "spy_ema_paper")
    ).read()
    assert lifecycle is not None
    assert lifecycle.phase == "RETIRED"
    assert lifecycle.retired_reason == "machinery replaced"


async def test_start_run_propagates_daemon_404(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(404, "Run 'run-missing' not found")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-missing/start", json={})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_start_run_propagates_daemon_unreachable_503(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise host_daemon_client.HostDaemonError(503, "host daemon unreachable: connection refused")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 503


async def test_start_run_invalid_daemon_payload_returns_502(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        return {"unexpected": "shape"}  # missing accepted/process

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# PRD #619-C5 — single-shot mutation OUTCOME_UNKNOWN surfacing
# ---------------------------------------------------------------------------


def _outcome_unknown_exc(
    *, category: str = "read_timeout", detail: str = "response lost"
) -> host_daemon_client.HostDaemonOutcomeUnknownError:
    return host_daemon_client.HostDaemonOutcomeUnknownError(error_category=category, detail=detail)


def _assert_outcome_unknown_body(body: dict, *, endpoint: str, category: str = "read_timeout") -> None:
    """Shared assertions for the 619-C5 typed 409 response body."""
    assert body["outcome"] == "UNKNOWN"
    assert body["reason_code"] == "OUTCOME_UNKNOWN"
    assert body["error_category"] == category
    assert body["endpoint"] == endpoint
    assert isinstance(body["occurred_at_ms"], int)
    assert body["occurred_at_ms"] > 0
    assert isinstance(body["runbook_hint"], str)
    assert body["runbook_hint"]  # non-empty


async def test_deploy_outcome_unknown_returns_typed_409(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ReadTimeout-after-send during deploy must surface as 409 +
    OUTCOME_UNKNOWN — the run may or may not have been created on the
    daemon side."""
    app, _ = app_with_root
    _set_connected_broker_account(monkeypatch, "DU111")

    async def fake_deploy(_base_url: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc()

    monkeypatch.setattr(host_daemon_client, "deploy", fake_deploy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances", json=_deploy_body())

    assert response.status_code == 409
    _assert_outcome_unknown_body(response.json()["detail"], endpoint="deploy")


async def test_start_run_outcome_unknown_returns_typed_409(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc(category="write_timeout")

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/start", json={})

    assert response.status_code == 409
    _assert_outcome_unknown_body(response.json()["detail"], endpoint="start_run", category="write_timeout")


async def test_stop_run_outcome_unknown_returns_typed_409(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = app_with_root

    async def fake_stop(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc()

    monkeypatch.setattr(host_daemon_client, "stop_run", fake_stop)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/runs/run-abc/stop", json={})

    assert response.status_code == 409
    _assert_outcome_unknown_body(response.json()["detail"], endpoint="stop_run")


async def test_emergency_flatten_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Emergency-flatten has a 130s timeout; an ambiguous outcome here
    means broker positions may be in an intermediate state — the highest
    stakes case for 619-C5."""
    app, root = app_with_root
    sid = "strategy-of-flatten"
    _write_ledger(root, "run-flatten", sid, created_at_ms=1_700_000_000_000)

    async def fake_flatten(_base_url: str, _run_id: str, _payload: dict) -> dict:
        raise _outcome_unknown_exc(category="remote_protocol_error")

    monkeypatch.setattr(host_daemon_client, "emergency_flatten_run", fake_flatten)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/live-instances/{sid}/emergency-flatten",
            json={"account": "DU123", "confirm": True},
        )

    assert response.status_code == 409
    _assert_outcome_unknown_body(
        response.json()["detail"],
        endpoint="emergency_flatten",
        category="remote_protocol_error",
    )


async def test_renew_daemon_lease_outcome_unknown_returns_typed_409(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = app_with_root

    async def fake_renew(_base_url: str) -> dict:
        raise _outcome_unknown_exc(category="read_timeout")

    monkeypatch.setattr(host_daemon_client, "renew_control_plane_lease", fake_renew)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/daemon-health/renew-lease")

    assert response.status_code == 409
    _assert_outcome_unknown_body(
        response.json()["detail"],
        endpoint="renew_daemon_lease",
        category="read_timeout",
    )


def test_outcome_unknown_reason_code_is_in_documented_vocabulary() -> None:
    """The reason code must be present in the closed REASON_CODES set
    so the Frontend's typed lookup ships the operator copy alongside C5."""
    from app.services.operator_capability import REASON_CODES

    assert "OUTCOME_UNKNOWN" in REASON_CODES


async def test_start_run_rejects_unsafe_run_id_400(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unsafe run_id is rejected at the boundary before any forward."""
    app, _ = app_with_root
    called = False

    async def fake_start(_base_url: str, _run_id: str, _payload: dict) -> dict:
        nonlocal called
        called = True
        return {"accepted": True, "process": _running_process("x")}

    monkeypatch.setattr(host_daemon_client, "start_run", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Leading whitespace reaches the handler as a single segment and is
        # rejected by _validate_path_segment; the daemon is never called.
        response = await client.post("/api/live-instances/runs/ bad/start", json={})

    assert response.status_code == 400
    assert called is False


def _write_run_status(
    root: Path,
    run_id: str,
    *,
    ended_at_ms: int | None,
    exit_code: int | None,
    exit_reason: str | None,
) -> None:
    (root / run_id / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "started_at_ms": 1,
                "last_update_ms": 2,
                "ended_at_ms": ended_at_ms,
                "exit_code": exit_code,
                "exit_reason": exit_reason,
                "host_pid": 4242,
            }
        ),
        encoding="utf-8",
    )


def _write_hydration(root: Path, run_id: str, *, accepted: bool, failure_reason: str) -> None:
    (root / run_id / "indicator_state_hydration.json").write_text(
        json.dumps({"schema_version": 1, "accepted": accepted, "validation": {"failure_reason": failure_reason}}),
        encoding="utf-8",
    )


async def test_status_last_exit_surfaces_cold_start_hydration_failure(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STOPPED instance must explain *why* it stopped. A cold start that exits 4
    under hydrate_policy=require carries the hydration receipt's failure_reason so
    the console can render seed-day guidance instead of a bare 'STOPPED'."""
    app, root = app_with_root
    _write_ledger(root, "run-coldstart", "spy_ema_paper", 100)
    _write_run_status(root, "run-coldstart", ended_at_ms=200, exit_code=4, exit_reason="exception")
    _write_hydration(root, "run-coldstart", accepted=False, failure_reason="missing")
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit is not None
    assert last_exit["exit_code"] == 4
    assert last_exit["exit_reason"] == "exception"
    assert last_exit["hydration_accepted"] is False
    assert last_exit["hydration_failure_reason"] == "missing"


async def test_status_last_exit_absent_while_run_is_live(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live run (no terminal ended_at_ms) must not surface a stale last_exit —
    that would contradict the RUNNING badge."""
    app, root = app_with_root
    _write_ledger(root, "run-live-ccc", "spy_ema_paper", 100)
    _write_run_status(root, "run-live-ccc", ended_at_ms=None, exit_code=None, exit_reason=None)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-live-ccc", "pid": 7, "started_at_ms": 100},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["last_exit"] is None


async def test_status_last_exit_tolerates_malformed_hydration_receipt(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt/hand-edited receipt (non-bool ``accepted``, non-str
    ``failure_reason``) must not 500 the status endpoint — the hydration fields
    degrade to None while the run's exit is still reported."""
    app, root = app_with_root
    _write_ledger(root, "run-badreceipt", "spy_ema_paper", 100)
    _write_run_status(root, "run-badreceipt", ended_at_ms=200, exit_code=4, exit_reason="exception")
    (root / "run-badreceipt" / "indicator_state_hydration.json").write_text(
        json.dumps({"accepted": "nope", "validation": {"failure_reason": 123}}), encoding="utf-8"
    )
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    last_exit = response.json()["last_exit"]
    assert last_exit["exit_code"] == 4
    assert last_exit["hydration_accepted"] is None
    assert last_exit["hydration_failure_reason"] is None


async def test_start_defaults_readonly_false_in_paper_mode(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper mode with orders allowed defaults the Start card to place (paper)
    orders — readonly=False — so the operator doesn't re-enable trading on every
    start. Orders are paper, so trading-by-default is safe."""
    app, root = app_with_root
    _write_ledger(root, "run-paper", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is False


async def test_start_defaults_readonly_true_in_live_mode(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live mode keeps the Start card in shadow (no-orders) by default — a
    real-money run never auto-trades from a server-authored default."""
    app, root = app_with_root
    _write_ledger(root, "run-live-mode", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="live",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


async def test_start_defaults_honors_ibkr_readonly_in_paper_mode(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IBKR_READONLY=true keeps the Start card in shadow even in paper mode — the
    engine refuses orders under operator lockdown, so the UI must not promise
    them. (CodeRabbit #436.)"""
    app, root = app_with_root
    _write_ledger(root, "run-lockdown", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=True,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


async def test_start_defaults_fail_closed_when_mode_missing(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing/unknown ``mode`` (config drift, partial rollout) must fail closed
    to shadow — never default to placing orders on a possibly-live account.
    (CodeRabbit #436.)"""
    app, root = app_with_root
    _write_ledger(root, "run-nomode", "spy_ema_paper", 100)
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        readonly=False,  # even with orders allowed, an absent mode stays shadow
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    _set_daemon(monkeypatch, process={"state": "idle"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/live-instances/spy_ema_paper/status")

    assert response.status_code == 200
    assert response.json()["start_defaults"]["readonly"] is True


# ─────────────────────── VCR-P3-I — NY-tz trading day ───────────────────


def test_today_ny_uses_america_new_york_not_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """VCR-P3-I — ``_today_ny()`` returns the trading-day date in
    ``America/New_York``, NOT the UTC calendar date. At the UTC
    boundary (~00:00 UTC = ~19:00 ET winter / ~20:00 ET summer) these
    two dates differ, and the chart-snapshot ``today`` reference must
    follow the trading day, not the UTC day."""
    from datetime import UTC, date, datetime

    fixed_utc_instant = datetime(2026, 3, 6, 2, 30, tzinfo=UTC)
    # 2026-03-06 02:30 UTC == 2026-03-05 21:30 America/New_York (EST).
    # Trading day is 2026-03-05; UTC calendar says 2026-03-06.

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return fixed_utc_instant.replace(tzinfo=None)
            return fixed_utc_instant.astimezone(tz)

    monkeypatch.setattr(live_instances, "datetime", _FixedDatetime)
    assert live_instances._today_ny() == date(2026, 3, 5)
    # Sanity: the NY-tz "today" is NOT the same as the UTC "today" at
    # this instant — otherwise the test isn't actually exercising the
    # bug it covers.
    assert live_instances._today_ny() != fixed_utc_instant.date()


# ── reconciliation PR 2 — runtime reconcile endpoint ────────────────────


async def test_reconcile_endpoint_enqueues_command_when_bound(app_with_root, monkeypatch: pytest.MonkeyPatch) -> None:
    """Daemon reports a live binding + bound run_dir exists on disk →
    POST /reconcile returns 200 with a request_id + accepted_at_ms and
    a pending RECONCILE command file appears under the run's commands dir.
    """
    app, root = app_with_root
    _write_ledger(root, "run-reconcile", "spy_ema_paper", 100)
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-reconcile", "pid": 9},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/reconcile")

    assert response.status_code == 200
    body = response.json()
    # The request_id is a 22-char base64url token (mint_intent_id).
    assert isinstance(body["request_id"], str) and len(body["request_id"]) == 22
    assert isinstance(body["accepted_at_ms"], int) and body["accepted_at_ms"] > 0

    queued = list((root / "run-reconcile" / "commands").glob("command.*.RECONCILE.pending.json"))
    assert len(queued) == 1, "RECONCILE command must be persisted to the bound run"


async def test_reconcile_endpoint_returns_409_when_no_live_binding(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live binding → 409 NO_LIVE_BINDING.

    Runtime reconciliation requires a live engine to acquire the submit
    lock and probe the broker. A durable-only enqueue would never be
    acted on, so surface the gap honestly rather than pretend.
    """
    app, _root = app_with_root
    _set_daemon(monkeypatch, process=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/reconcile")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason_code"] == "NO_LIVE_BINDING"
    assert "live engine" in detail["message"]


async def test_reconcile_endpoint_returns_404_when_run_dir_missing(
    app_with_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon reports a live binding whose run_dir is not visible under
    this service's root → 404. A command written here would not be seen
    by the engine polling its real dir.
    """
    app, _root = app_with_root
    _set_daemon(
        monkeypatch,
        process={"state": "running", "run_id": "run-ghost", "pid": 5},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live-instances/spy_ema_paper/reconcile")

    assert response.status_code == 404
    assert "not visible" in response.json()["detail"]
