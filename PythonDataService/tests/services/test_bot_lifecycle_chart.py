from __future__ import annotations

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.schemas.live_runs import (
    BotLifecycleChartView,
    DesiredStateView,
    GateResult,
    InstanceBrokerView,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    LiveBinding,
    OperatorSurface,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
)
from app.services.bot_lifecycle_chart import compose_bot_lifecycle_chart
from app.services.operator_surface import compute_operator_surface

_NOW_MS = 1_700_000_000_000
_RUN_ID = "run-clean-x"
_SID = "bot-a"
_NAMESPACE = "learn-ai/bot-a/v1"


class _Publisher:
    is_running = True
    latest_row_ms = _NOW_MS - 1_000

    def last_persisted_seq(self) -> int:
        return 7


def _desired(state: str) -> DesiredStateView:
    return DesiredStateView(state=state, path_status="ok")


def _start_defaults() -> InstanceStartDefaults:
    return InstanceStartDefaults(
        strategy="spy_ema_crossover",
        readonly=True,
        max_orders_per_day=50,
        strategy_spec_path="references/specs/spy.json",
        qc_audit_copy_path="references/qc-shadow/spy.md",
        qc_cloud_backtest_id="qc-123",
    )


def _sizing() -> InstanceSizing:
    return InstanceSizing(
        policy={"kind": "FixedShares", "value": 1},
        preset="safe_canary",
        governed_by="live_config",
        sizing_provenance="live_override",
    )


def _readiness() -> ReadinessVector:
    return ReadinessVector(
        kind="live_readiness",
        as_of_ms=_NOW_MS,
        source="engine",
        verdict="READY",
        summary="Ready",
        gates=[
            ReadinessGate(
                name="engine_ready",
                status="pass",
                severity="hard",
                detail="Engine readiness is passing.",
                gate_result=GateResult(
                    gate_id="engine_ready",
                    status="pass",
                    source="test",
                    operator_reason="Engine readiness is passing.",
                    operator_next_step=None,
                    evidence_at_ms=_NOW_MS,
                ),
            )
        ],
    )


def _receipt(status: str = "passed") -> ReconciliationReceipt:
    return ReconciliationReceipt(
        status=status,  # type: ignore[arg-type]
        outcome="clean" if status == "passed" else None,
        run_id=_RUN_ID,
        strategy_instance_id=_SID,
        namespace=_NAMESPACE,
        started_at_ms=_NOW_MS - 5_000,
        completed_at_ms=_NOW_MS - 4_000 if status != "in_progress" else None,
        last_reconcile_ms=_NOW_MS - 4_000,
        sidecar_wal_seq=7,
        broker_observed_at_ms=_NOW_MS - 4_000,
        failure_reason="Broker snapshot disagrees with the intent WAL." if status == "failed" else None,
    )


def _surface(**overrides: object) -> OperatorSurface:
    kwargs = {
        "process": InstanceProcessView(state="running"),
        "safety_verdict_final": "paper-only",
        "broker_connection_state": "connected",
        "broker": InstanceBrokerView(
            bot_order_namespace=_NAMESPACE,
            owned_positions={},
            pending_order_count=0,
        ),
        "start_defaults": _start_defaults(),
        "sizing": _sizing(),
        "readiness": _readiness(),
        "instance_broker_self_consistent": True,
        "live_binding": LiveBinding(run_id=_RUN_ID),
        "desired_state": _desired("RUNNING"),
        "reconciliation_receipt": _receipt(),
        "current_wal_seq": 7,
        "current_run_id": _RUN_ID,
        "current_namespace": _NAMESPACE,
        "latest_broker_event_ms": _NOW_MS - 5_000,
        "latest_mutation_ms": _NOW_MS - 5_000,
        "activity_publisher": _Publisher(),
        "activity_publisher_registered_at_ms": _NOW_MS - 10_000,
        "now_ms": _NOW_MS,
    }
    kwargs.update(overrides)
    return compute_operator_surface(**kwargs)


def _edge_status(chart: BotLifecycleChartView, edge_id: str) -> str:
    edge = next(edge for edge in chart.global_graph.edges if edge.id == edge_id)
    return edge.status


def _subgraph_edge_status(chart: BotLifecycleChartView, graph_id: str, edge_id: str) -> str:
    edge = next(edge for edge in chart.subgraphs[graph_id].edges if edge.id == edge_id)
    return edge.status


def _node_status(chart: BotLifecycleChartView, node_id: str) -> str:
    node = next(node for node in chart.global_graph.nodes if node.id == node_id)
    return node.status


def test_chart_clean_running_bot_marks_active_path() -> None:
    surface = _surface()
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "active"
    assert _node_status(chart, "active") == "active"
    assert _edge_status(chart, "deploy_to_preflight") == "passed"
    assert _edge_status(chart, "activate_to_active") == "active"
    assert chart.subgraphs["submit_order"].nodes[2].technical_label == "placeOrder boundary"


def test_chart_missing_readiness_keeps_preflight_unknown() -> None:
    surface = _surface(readiness=None)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "preflight"
    assert _node_status(chart, "preflight") == "unknown"
    assert chart.subgraphs["preflight"].primary_node_id == "readiness_1"


def test_chart_account_freeze_colors_edge_into_account_safety() -> None:
    freeze = AccountFreezeEvidence(
        account_id="DU123",
        reason="Unresolved exposure exists after a restart.",
        source="test",
        recorded_at_ms=_NOW_MS,
        operator_next_step="Flatten or reconcile before restarting.",
    )
    surface = _surface(account_freeze=freeze)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "account_safety"
    assert _node_status(chart, "account_safety") == "freeze"
    assert _node_status(chart, "active") == "inactive"
    assert _edge_status(chart, "preflight_to_account_safety") == "freeze"
    start = next(action for action in chart.actions if action.id == "start_process")
    resume = next(action for action in chart.actions if action.id == "resume")
    assert start.enabled is False
    assert start.reason == "ACCOUNT_FROZEN"
    assert resume.enabled is False
    assert resume.reason == "ACCOUNT_FROZEN"


def test_account_safety_focuses_broker_connection_blocker() -> None:
    surface = _surface(broker_connection_state="disconnected")
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "account_safety"
    assert chart.subgraphs["account_safety"].primary_node_id == "broker_connection"
    assert _subgraph_edge_status(
        chart,
        "account_safety",
        "broker_connection_to_risk_posture",
    ) == "blocked"


def test_chart_failed_reconciliation_blocks_at_reconciliation_edge() -> None:
    surface = _surface(reconciliation_receipt=_receipt("failed"))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "reconcile"
    assert _node_status(chart, "reconcile") == "blocked"
    assert _edge_status(chart, "account_safety_to_reconcile") == "blocked"
    assert _edge_status(chart, "reconcile_to_activate") == "inactive"
