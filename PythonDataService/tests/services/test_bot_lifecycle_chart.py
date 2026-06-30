from __future__ import annotations

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.intent_events import IntentEvent, IntentEventType
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
    OperatorSurfaceAccountOwner,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
)
from app.services.bot_lifecycle_chart import compose_bot_lifecycle_chart
from app.services.bot_lifecycle_projection import project_intent_events
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


def _desired_absent() -> DesiredStateView:
    return DesiredStateView(state=None, path_status="absent")


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


def _intent(seq: int, event_type: IntentEventType) -> IntentEvent:
    intent_id = f"intent-{seq}"
    return IntentEvent(
        seq=seq,
        event_type=event_type,
        intent_id=intent_id,
        bot_order_namespace=_NAMESPACE,
        order_ref=f"{_NAMESPACE}:{intent_id}",
        appended_at_ms=_NOW_MS + seq,
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


def _account_owner(
    *,
    generation: int | None = 4,
    phase: str = "accepting",
) -> OperatorSurfaceAccountOwner:
    return OperatorSurfaceAccountOwner(
        account_id="DU123",
        generation=generation,
        phase=phase,  # type: ignore[arg-type]
        recorded_at_ms=_NOW_MS - 1_000,
        source="test",
    )


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
    broker_writer = next(node for node in chart.global_graph.nodes if node.id == "broker_writer")
    assert broker_writer.label == "Broker activity"
    assert broker_writer.technical_label == "Publisher health"
    assert "capture health" in (broker_writer.summary or "")
    assert "not proof that R3 AccountOwner daemon/IPC single-writer authority is shipped" in (
        broker_writer.summary or ""
    )
    assert chart.subgraphs["submit_order"].nodes[2].technical_label == "Broker submit boundary"


def test_chart_absent_desired_state_uses_effective_running_default() -> None:
    surface = _surface()
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired_absent())
    activate_nodes = {node.id: node for node in chart.subgraphs["activate"].nodes}

    assert chart.global_graph.primary_node_id == "active"
    assert _node_status(chart, "activate") == "passed"
    assert _node_status(chart, "active") == "active"
    assert activate_nodes["desired_state"].status == "passed"
    assert activate_nodes["desired_state"].technical_label == "RUNNING default"
    assert activate_nodes["desired_state"].summary == "Desired-state sidecar is absent; effective state is RUNNING."


def test_submit_subgraph_is_unknown_without_durable_submit_evidence() -> None:
    surface = _surface()
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}

    assert nodes["intent_wal"].status == "unknown"
    assert nodes["intent_wal"].status_label == "Unknown"
    assert "No Intent WAL row" in (nodes["intent_wal"].why or "")
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    assert broker_nodes["writer_guard"].status == "unknown"
    assert "R3 daemon/IPC single-writer authority is not shipped" in (broker_nodes["writer_guard"].summary or "")


def test_writer_guard_surfaces_account_owner_generation_without_claiming_r3_daemon() -> None:
    surface = _surface(account_owner=_account_owner())
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    receipts = {receipt.label: receipt for receipt in broker_nodes["writer_guard"].receipts}

    assert broker_nodes["writer_guard"].label == "Owner generation"
    assert broker_nodes["writer_guard"].technical_label == "accepting gen 4"
    assert broker_nodes["writer_guard"].status == "passed"
    assert broker_nodes["writer_guard"].ts_ms == _NOW_MS - 1_000
    assert broker_nodes["writer_guard"].ts_ms_resolved is True
    assert receipts["account_owner.phase"].value == "accepting"
    assert receipts["account_owner.phase"].source == "test"
    assert receipts["account_owner.generation"].value == "4"
    assert receipts["account_owner.generation"].ts_ms == _NOW_MS - 1_000
    assert receipts["account_owner.generation"].ts_ms_resolved is True
    assert "generation is 4" in (broker_nodes["writer_guard"].summary or "")
    assert "not proof that R3 daemon/IPC single-writer authority is shipped" in (
        broker_nodes["writer_guard"].summary or ""
    )


def test_submit_uncertainty_reaches_submit_subgraph() -> None:
    surface = _surface()
    lifecycle_events = project_intent_events(
        [_intent(1, IntentEventType.ACK_FAILED_UNCERTAIN)],
        bot_id=_SID,
        account_id="DU123",
        run_id=_RUN_ID,
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        surface,
        desired_state=_desired("RUNNING"),
        lifecycle_events=lifecycle_events,
    )
    nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}
    ack_receipts = {receipt.label: receipt for receipt in nodes["ack_or_reconcile"].receipts}

    assert nodes["ack_or_reconcile"].status == "blocked"
    assert nodes["ack_or_reconcile"].ts_ms == _NOW_MS + 1
    assert nodes["ack_or_reconcile"].ts_ms_resolved is True
    assert nodes["ack_or_reconcile"].summary == "Broker acknowledgement failed; submit outcome is uncertain."
    assert nodes["ack_or_reconcile"].operator_next_step == "PROBE_BROKER_BEFORE_RETRY"
    assert ack_receipts["event_type"].value == "BrokerOrderUncertain"
    assert ack_receipts["source_seq"].value == "1"
    assert ack_receipts["intent_id"].value == "intent-1"
    assert ack_receipts["order_ref"].value == f"{_NAMESPACE}:intent-1"
    assert ack_receipts["ts_ms_source"].value == "appended_at_ms"


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
    assert (
        _subgraph_edge_status(
            chart,
            "account_safety",
            "broker_connection_to_risk_posture",
        )
        == "blocked"
    )


def test_chart_failed_reconciliation_blocks_at_reconciliation_edge() -> None:
    surface = _surface(reconciliation_receipt=_receipt("failed"))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    reconcile_node = next(node for node in chart.global_graph.nodes if node.id == "reconcile")
    receipt_node = next(node for node in chart.subgraphs["reconcile"].nodes if node.id == "receipt")
    reconcile_receipts = {receipt.label: receipt for receipt in reconcile_node.receipts}
    receipt_receipts = {receipt.label: receipt for receipt in receipt_node.receipts}

    assert chart.global_graph.primary_node_id == "reconcile"
    assert _node_status(chart, "reconcile") == "blocked"
    assert reconcile_node.ts_ms == _NOW_MS - 4_000
    assert reconcile_node.ts_ms_resolved is True
    assert reconcile_receipts["reconciliation.state"].value == "FAILED"
    assert reconcile_receipts["last_reconcile_ms"].value == str(_NOW_MS - 4_000)
    assert reconcile_receipts["last_reconcile_ms"].unit == "ms UTC"
    assert reconcile_receipts["failure_reason"].value == "Broker snapshot disagrees with the intent WAL."
    assert receipt_node.ts_ms == _NOW_MS - 4_000
    assert receipt_node.ts_ms_resolved is True
    assert receipt_receipts["reconciliation.state"].value == "FAILED"
    assert _edge_status(chart, "account_safety_to_reconcile") == "blocked"
    assert _edge_status(chart, "reconcile_to_activate") == "inactive"


# ---------------------------------------------------------------------------
# Receipts: reconciliation node when no reconciliation surface is present

def test_reconcile_node_has_no_ts_ms_when_reconciliation_absent() -> None:
    """When no reconciliation receipt exists, ts_ms and receipts must be absent."""
    surface = _surface(reconciliation_receipt=None)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    reconcile_node = next(node for node in chart.global_graph.nodes if node.id == "reconcile")

    assert reconcile_node.ts_ms is None
    assert reconcile_node.ts_ms_resolved is False
    assert reconcile_node.receipts == []


def test_reconcile_subgraph_receipt_node_has_no_ts_ms_when_reconciliation_absent() -> None:
    """The 'receipt' node inside the reconcile subgraph inherits no ts_ms when absent."""
    surface = _surface(reconciliation_receipt=None)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    receipt_node = next(node for node in chart.subgraphs["reconcile"].nodes if node.id == "receipt")

    assert receipt_node.ts_ms is None
    assert receipt_node.ts_ms_resolved is False
    assert receipt_node.receipts == []


def test_passed_reconciliation_node_carries_ts_ms_and_receipts() -> None:
    """A passing reconciliation exposes ts_ms + state/adopted_intent_count/last_reconcile_ms receipts."""
    surface = _surface(reconciliation_receipt=_receipt("passed"))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    reconcile_node = next(node for node in chart.global_graph.nodes if node.id == "reconcile")
    receipts = {r.label: r for r in reconcile_node.receipts}

    assert reconcile_node.ts_ms == _NOW_MS - 4_000
    assert reconcile_node.ts_ms_resolved is True
    assert "reconciliation.state" in receipts
    assert receipts["reconciliation.state"].value == "PASSED"
    assert receipts["reconciliation.state"].source == "reconciliation_projection"
    assert "adopted_intent_count" in receipts
    assert "last_reconcile_ms" in receipts
    assert receipts["last_reconcile_ms"].unit == "ms UTC"
    # No failure_reason receipt for a passing reconciliation.
    assert "failure_reason" not in receipts


def test_reconciliation_receipts_adopted_intent_count_value() -> None:
    """adopted_intent_count receipt value reflects the actual intent list length."""
    surface = _surface(reconciliation_receipt=_receipt("passed"))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    reconcile_node = next(node for node in chart.global_graph.nodes if node.id == "reconcile")
    receipts = {r.label: r for r in reconcile_node.receipts}

    # The default _receipt() builds a ReconciliationReceipt with no adopted_intent_ids.
    assert receipts["adopted_intent_count"].value == "0"


# ---------------------------------------------------------------------------
# Receipts: account owner absent / generation unknown

def test_writer_guard_has_no_ts_ms_when_account_owner_absent() -> None:
    """When account_owner is absent, writer_guard must not carry ts_ms or receipts."""
    surface = _surface()  # no account_owner keyword -> None
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}

    assert broker_nodes["writer_guard"].ts_ms is None
    assert broker_nodes["writer_guard"].ts_ms_resolved is False
    assert broker_nodes["writer_guard"].receipts == []


def test_writer_guard_receipts_when_generation_is_none() -> None:
    """Account owner with unknown generation still emits receipts; generation value is 'unknown'."""
    surface = _surface(account_owner=_account_owner(generation=None))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    receipts = {r.label: r for r in broker_nodes["writer_guard"].receipts}

    assert broker_nodes["writer_guard"].ts_ms == _NOW_MS - 1_000
    assert broker_nodes["writer_guard"].ts_ms_resolved is True
    assert receipts["account_owner.phase"].value == "accepting"
    assert receipts["account_owner.generation"].value == "unknown"
    assert receipts["account_owner.generation"].ts_ms == _NOW_MS - 1_000


def test_writer_guard_account_owner_receipts_source_matches_owner_source() -> None:
    """Receipt source must be taken from account_owner.source, not hardcoded."""
    owner = OperatorSurfaceAccountOwner(
        account_id="DU999",
        generation=7,
        phase="accepting",  # type: ignore[arg-type]
        recorded_at_ms=_NOW_MS - 500,
        source="custom_source",
    )
    surface = _surface(account_owner=owner)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    receipts = {r.label: r for r in broker_nodes["writer_guard"].receipts}

    assert receipts["account_owner.phase"].source == "custom_source"
    assert receipts["account_owner.generation"].source == "custom_source"
    assert receipts["account_owner.generation"].value == "7"


# ---------------------------------------------------------------------------
# Receipts: event receipts from lifecycle events

def test_event_receipts_include_only_present_payload_fields() -> None:
    """Only non-None payload fields are included in event receipts."""
    lifecycle_events = project_intent_events(
        [_intent(2, IntentEventType.ACK_FAILED_UNCERTAIN)],
        bot_id=_SID,
        account_id="DU123",
        run_id=_RUN_ID,
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        _surface(),
        desired_state=_desired("RUNNING"),
        lifecycle_events=lifecycle_events,
    )
    nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}
    ack_receipts = {r.label: r for r in nodes["ack_or_reconcile"].receipts}

    # These fields are always present.
    assert "event_type" in ack_receipts
    assert "source_seq" in ack_receipts
    # intent_id and order_ref are set by _intent().
    assert "intent_id" in ack_receipts
    assert "order_ref" in ack_receipts
    # order_id and perm_id are not set by _intent(), so must be absent.
    assert "order_id" not in ack_receipts
    assert "perm_id" not in ack_receipts


def test_event_receipts_source_seq_value_matches_event_seq() -> None:
    """source_seq receipt value must match the event sequence number as a string."""
    lifecycle_events = project_intent_events(
        [_intent(5, IntentEventType.ACK_FAILED_UNCERTAIN)],
        bot_id=_SID,
        account_id="DU123",
        run_id=_RUN_ID,
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        _surface(),
        desired_state=_desired("RUNNING"),
        lifecycle_events=lifecycle_events,
    )
    nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}
    ack_receipts = {r.label: r for r in nodes["ack_or_reconcile"].receipts}

    assert ack_receipts["source_seq"].value == "5"
    assert ack_receipts["intent_id"].value == "intent-5"


def test_event_receipts_ts_ms_matches_event_timestamp() -> None:
    """Receipts derived from an event carry the event's ts_ms."""
    lifecycle_events = project_intent_events(
        [_intent(1, IntentEventType.ACK_FAILED_UNCERTAIN)],
        bot_id=_SID,
        account_id="DU123",
        run_id=_RUN_ID,
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        _surface(),
        desired_state=_desired("RUNNING"),
        lifecycle_events=lifecycle_events,
    )
    nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}
    ack_receipts = {r.label: r for r in nodes["ack_or_reconcile"].receipts}

    # _intent(seq=1) uses appended_at_ms=_NOW_MS + 1
    assert ack_receipts["event_type"].ts_ms == _NOW_MS + 1
    assert ack_receipts["event_type"].ts_ms_resolved is True


# ---------------------------------------------------------------------------
# Receipts: failure_reason conditional inclusion

def test_reconciliation_receipts_include_failure_reason_when_present() -> None:
    """failure_reason receipt is only emitted when the reconciliation has a failure."""
    surface_failed = _surface(reconciliation_receipt=_receipt("failed"))
    chart_failed = compose_bot_lifecycle_chart(_SID, surface_failed, desired_state=_desired("RUNNING"))
    reconcile_node_failed = next(node for node in chart_failed.global_graph.nodes if node.id == "reconcile")
    receipts_failed = {r.label: r for r in reconcile_node_failed.receipts}

    surface_passed = _surface(reconciliation_receipt=_receipt("passed"))
    chart_passed = compose_bot_lifecycle_chart(_SID, surface_passed, desired_state=_desired("RUNNING"))
    reconcile_node_passed = next(node for node in chart_passed.global_graph.nodes if node.id == "reconcile")
    receipts_passed = {r.label: r for r in reconcile_node_passed.receipts}

    assert "failure_reason" in receipts_failed
    assert "failure_reason" not in receipts_passed
