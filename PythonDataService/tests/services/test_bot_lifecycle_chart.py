from __future__ import annotations

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.engine.live.intent_events import DropReason, IntentEvent, IntentEventType
from app.operator.notices.schema import OperatorNotice, OperatorNoticeAction
from app.schemas.live_runs import (
    BotLifecycleChartView,
    BotLifecycleEvent,
    DesiredStateView,
    GateResult,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceProvenance,
    InstanceSizing,
    InstanceStartDefaults,
    LiveBinding,
    OperatorSurface,
    OperatorSurfaceAccountClerk,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
)
from app.services.bot_lifecycle_chart import compose_bot_lifecycle_chart
from app.services.bot_lifecycle_projection import (
    account_event_to_lifecycle_event,
    project_account_events,
    project_intent_events,
)
from app.services.bot_lifecycle_receipts import LifecycleReceiptContext
from app.services.operator_surface import compute_operator_surface
from app.services.runtime_freshness import DomainFreshness, RuntimeFreshness

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


def _intent(seq: int, event_type: IntentEventType, *, drop_reason: DropReason | None = None) -> IntentEvent:
    intent_id = f"intent-{seq}"
    return IntentEvent(
        seq=seq,
        event_type=event_type,
        intent_id=intent_id,
        bot_order_namespace=_NAMESPACE,
        order_ref=f"{_NAMESPACE}:{intent_id}",
        appended_at_ms=_NOW_MS + seq,
        drop_reason=drop_reason,
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


def _account_clerk(
    *,
    generation: int | None = 4,
    phase: str = "accepting",
) -> OperatorSurfaceAccountClerk:
    return OperatorSurfaceAccountClerk(
        account_id="DU123",
        generation=generation,
        phase=phase,  # type: ignore[arg-type]
        lease_active=True,
        recorded_at_ms=_NOW_MS - 1_000,
        source="test",
    )


def _watchdog_notice() -> OperatorNotice:
    return OperatorNotice(
        code="watchdog.flatten_failed",
        tier="critical",
        title="Watchdog flatten failed",
        message="The watchdog could not prove the account was flat before disconnect.",
        actionability="routed",
        resolution="Clears after the operator verifies IBKR positions and runs Reconcile.",
        action=OperatorNoticeAction(kind="open_runbook", label="Open runbook", target="watchdog-halt"),
        runbook_slug="watchdog-halt",
        occurred_at_ms=_NOW_MS - 7_000,
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
    branch_edges = {edge.id: edge for edge in chart.global_graph.edges}
    assert branch_edges["active_to_submit_order"].source_handle == "source-east"
    assert branch_edges["active_to_submit_order"].target_handle == "target-west"
    assert branch_edges["active_to_recovery"].source_handle == "source-south"
    assert branch_edges["active_to_recovery"].target_handle == "target-north"
    broker_writer = next(node for node in chart.global_graph.nodes if node.id == "broker_writer")
    assert broker_writer.label == "Broker activity"
    assert broker_writer.technical_label == "Publisher health"
    assert "capture health" in (broker_writer.summary or "")
    assert "separate from Account Clerk write-authority health" in (
        broker_writer.summary or ""
    )


def test_chart_authors_node_receipt_prose_and_actionability() -> None:
    surface = _surface(account_clerk=_account_clerk())
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    submit_nodes = {node.id: node for node in chart.subgraphs["submit_order"].nodes}
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    signal_receipts = {receipt.label: receipt for receipt in submit_nodes["signal"].receipts}
    broker_ack_receipts = {receipt.label: receipt for receipt in broker_nodes["broker_ack"].receipts}
    writer_receipts = {receipt.label: receipt for receipt in broker_nodes["writer_guard"].receipts}

    assert submit_nodes["place_order"].label == "Broker submission"
    assert submit_nodes["ack_or_reconcile"].label == "Acknowledgment or reconcile"
    assert broker_nodes["broker_ack"].label == "Broker acknowledgment"
    assert submit_nodes["signal"].operator_actionability == "system-only"
    assert broker_nodes["broker_ack"].operator_actionability == "system-only"
    assert signal_receipts["strategy_signal.evidence"].headline == "No signal evidence emitted yet."
    assert (
        broker_ack_receipts["broker_acknowledgment.evidence"].headline
        == "No direct broker acknowledgment evidence emitted yet."
    )
    assert writer_receipts["account_clerk.generation"].headline == "Account Clerk generation is 4."


def test_chart_preflight_receipts_use_status_level_context_without_frontend_joining() -> None:
    surface = _surface()
    context = LifecycleReceiptContext(
        symbol="SPY",
        action_plan={"on_enter": [], "on_exit": []},
        instrument_surface="explicit",
        start_defaults=_start_defaults(),
        provenance=InstanceProvenance(
            run_id=_RUN_ID,
            schema_version="2",
            strategy_spec_path="references/specs/spy.json",
            strategy_spec_sha256="abc",
            qc_audit_copy_path="references/qc-shadow/spy.md",
            qc_audit_copy_sha256="def",
            qc_cloud_backtest_id="qc-123",
            account_id="DU123",
            created_at_ms=_NOW_MS - 10_000,
            live_config={"consolidator_period_min": 15, "warmup_bars": 30},
        ),
        sizing=_sizing(),
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        surface,
        desired_state=_desired("RUNNING"),
        receipt_context=context,
    )
    configuration = next(node for node in chart.subgraphs["preflight"].nodes if node.id == "configuration")
    receipts = {receipt.label: receipt for receipt in configuration.receipts}

    assert receipts["run.symbol"].value == "SPY"
    assert receipts["run.symbol"].headline == "This bot is configured for SPY."
    assert receipts["instrument_surface.plan"].value == "explicit"
    assert receipts["action_plan.declared"].headline == "A committed action plan is present for this run."
    assert receipts["run.provenance.run_id"].value == _RUN_ID
    assert receipts["live_config.consolidator_period_min"].value == "15"
    assert receipts["live_config.warmup_bars"].value == "30"
    assert receipts["sizing.preset"].value == "safe_canary"


def test_chart_routes_raw_node_reason_codes_to_receipts() -> None:
    runtime_freshness = RuntimeFreshness(
        command_loop=DomainFreshness(
            state="STALE",
            age_ms=5_000,
            stale_reason_codes=["COMMAND_LOOP_STALE"],
        ),
        broker=DomainFreshness(state="FRESH", age_ms=100),
        bar_loop=DomainFreshness(state="FRESH", age_ms=100),
        control_plane=DomainFreshness(state="FRESH", age_ms=100),
        posture_demoted=True,
    )
    surface = _surface(start_defaults=None, runtime_freshness=runtime_freshness)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    preflight = next(node for node in chart.global_graph.nodes if node.id == "preflight")
    configuration = next(node for node in chart.subgraphs["preflight"].nodes if node.id == "configuration")
    command_loop = next(node for node in chart.subgraphs["activate"].nodes if node.id == "command_loop")

    for node, raw_code in (
        (preflight, "STRATEGY_KEY_MISSING"),
        (configuration, "STRATEGY_KEY_MISSING"),
        (command_loop, "COMMAND_LOOP_STALE"),
    ):
        trader_text = " ".join(
            value or ""
            for value in (node.summary, node.why, node.evidence_summary)
        )
        receipt_text = " ".join(receipt.value for receipt in node.receipts)
        assert raw_code not in trader_text
        assert raw_code in receipt_text
    assert chart.subgraphs["submit_order"].nodes[2].technical_label == "Broker submission boundary"


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
    assert "No Account Clerk generation or active-lease evidence is available" in (
        broker_nodes["writer_guard"].summary or ""
    )


def test_writer_guard_surfaces_account_clerk_generation_and_lease() -> None:
    surface = _surface(account_clerk=_account_clerk())
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_nodes = {node.id: node for node in chart.subgraphs["broker_writer"].nodes}
    receipts = {receipt.label: receipt for receipt in broker_nodes["writer_guard"].receipts}

    assert broker_nodes["writer_guard"].label == "Account Clerk"
    assert broker_nodes["writer_guard"].technical_label == "accepting gen 4; lease active"
    assert broker_nodes["writer_guard"].status == "passed"
    assert broker_nodes["writer_guard"].ts_ms == _NOW_MS - 1_000
    assert broker_nodes["writer_guard"].ts_ms_resolved is True
    assert receipts["account_clerk.phase"].value == "accepting"
    assert receipts["account_clerk.phase"].source == "test"
    assert receipts["account_clerk.generation"].value == "4"
    assert receipts["account_clerk.generation"].ts_ms == _NOW_MS - 1_000
    assert receipts["account_clerk.generation"].ts_ms_resolved is True
    assert "generation is 4" in (broker_nodes["writer_guard"].summary or "")
    assert "its lease is active" in (
        broker_nodes["writer_guard"].summary or ""
    )


def test_writer_guard_legacy_event_does_not_override_current_clerk_health() -> None:
    event = BotLifecycleEvent(
        event_id="account_event:DU123:1:account_owner_generation_recorded",
        account_id="DU123",
        event_type="account_owner_generation_recorded",
        category="lifecycle_transition",
        node_id="writer_guard",
        status="active",
        severity="info",
        ts_ms=_NOW_MS - 1_000,
        source="account_owner",
        source_rank=20,
        source_local_seq=1,
        summary="AccountOwner generation recorded.",
        payload={"phase": "accepting", "generation": 4},
    )

    chart = compose_bot_lifecycle_chart(_SID, _surface(), desired_state=_desired("RUNNING"), lifecycle_events=[event])
    writer_guard = next(node for node in chart.subgraphs["broker_writer"].nodes if node.id == "writer_guard")

    assert writer_guard.status == "unknown"
    assert writer_guard.status_label == "Unknown"


def test_writer_guard_event_preserves_surface_owner_receipts() -> None:
    event = BotLifecycleEvent(
        event_id="account_event:DU123:1:account_owner_generation_recorded",
        account_id="DU123",
        event_type="account_owner_generation_recorded",
        category="lifecycle_transition",
        node_id="writer_guard",
        status="active",
        severity="info",
        ts_ms=_NOW_MS - 1_000,
        source="account_owner",
        source_rank=20,
        source_local_seq=1,
        summary="Account Clerk generation recorded.",
        payload={"phase": "accepting", "generation": 4},
    )

    chart = compose_bot_lifecycle_chart(
        _SID,
        _surface(account_clerk=_account_clerk()),
        desired_state=_desired("RUNNING"),
        lifecycle_events=[event],
    )
    writer_guard = next(node for node in chart.subgraphs["broker_writer"].nodes if node.id == "writer_guard")
    receipts = {receipt.label: receipt for receipt in writer_guard.receipts}

    assert writer_guard.status == "passed"
    assert writer_guard.technical_label == "accepting gen 4; lease active"
    assert receipts["account_clerk.phase"].value == "accepting"
    assert receipts["account_clerk.generation"].value == "4"


def test_legacy_reconnect_event_does_not_mark_writer_guard_passed() -> None:
    account_events = project_account_events(
        [
            {
                "account_id": "DU123",
                "event_type": "account_owner_reconnect_resumed",
                "seq": 1,
                "ts_ms": _NOW_MS - 1_000,
                "phase": "accepting",
                "generation": 4,
            }
        ],
        account_id="DU123",
    )
    lifecycle_events = [
        account_event_to_lifecycle_event(event).model_copy(update={"bot_id": _SID}) for event in account_events
    ]

    chart = compose_bot_lifecycle_chart(
        _SID,
        _surface(),
        desired_state=_desired("RUNNING"),
        lifecycle_events=lifecycle_events,
    )
    writer_guard = next(node for node in chart.subgraphs["broker_writer"].nodes if node.id == "writer_guard")

    assert writer_guard.status == "unknown"
    assert writer_guard.status_label == "Unknown"


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
    assert nodes["ack_or_reconcile"].summary == "Broker acknowledgment failed; submit outcome is uncertain."
    assert nodes["ack_or_reconcile"].operator_next_step == "PROBE_BROKER_BEFORE_RETRY"
    assert ack_receipts["event_type"].value == "BrokerOrderUncertain"
    assert ack_receipts["source_seq"].value == "1"
    assert ack_receipts["intent_id"].value == "intent-1"
    assert ack_receipts["order_ref"].value == f"{_NAMESPACE}:intent-1"
    assert ack_receipts["ts_ms_source"].value == "appended_at_ms"


def test_submit_drop_receipts_include_daily_order_cap_and_drop_reason() -> None:
    readiness = _readiness().model_copy(update={"orders_used": 50, "orders_cap": 50})
    surface = _surface(readiness=readiness)
    lifecycle_events = project_intent_events(
        [_intent(1, IntentEventType.INTENT_DROPPED_BEFORE_SUBMIT, drop_reason="max_orders_per_day")],
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
    submit_node = next(node for node in chart.global_graph.nodes if node.id == "submit_order")
    receipts = {receipt.label: receipt for receipt in submit_node.receipts}

    assert submit_node.status == "blocked"
    assert submit_node.why == "Submission gate dropped the intent: max_orders_per_day."
    assert receipts["drop_reason"].value == "max_orders_per_day"
    assert receipts["daily_order_cap.used"].value == "50"
    assert receipts["daily_order_cap.used"].unit == "orders"
    assert receipts["daily_order_cap.limit"].value == "50"
    assert receipts["daily_order_cap.limit"].unit == "orders"


def test_recovery_placeholders_are_inactive_without_active_incident() -> None:
    surface = _surface()
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    nodes = {node.id: node for node in chart.subgraphs["recovery"].nodes}

    assert nodes["incident"].status == "inactive"
    for node_id in ("flatten", "reconcile_after", "fresh_run"):
        assert nodes[node_id].status == "inactive"
        assert nodes[node_id].why is None
        assert nodes[node_id].operator_next_step is None
        assert nodes[node_id].ts_ms is None
        assert nodes[node_id].ts_ms_resolved is False
        assert "No active recovery incident" in (nodes[node_id].summary or "")


def test_recovery_placeholders_are_unknown_while_process_is_stopping() -> None:
    surface = _surface(process=InstanceProcessView(state="stopping"))
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    nodes = {node.id: node for node in chart.subgraphs["recovery"].nodes}

    assert chart.global_graph.primary_node_id == "recovery"
    assert nodes["incident"].status == "active"
    assert nodes["incident"].summary == "The bot process is currently stopping."
    for node_id in ("flatten", "reconcile_after", "fresh_run"):
        assert nodes[node_id].status == "unknown"
        assert nodes[node_id].operator_next_step == "WAIT_FOR_STOPPING_TO_FINISH"
    assert "no flatten-proof requirement is active" in (nodes["flatten"].summary or "")


def test_recovery_placeholders_are_unknown_when_recovery_requires_proof() -> None:
    last_exit = InstanceLastExit(
        run_id="prior-run",
        exit_code=1,
        halt_trigger="OUTSIDE_MUTATION",
        halt_at_ms=_NOW_MS - 6_000,
    )
    surface = _surface(
        last_exit=last_exit,
    )
    chart = compose_bot_lifecycle_chart(
        _SID,
        surface,
        desired_state=_desired("RUNNING"),
        receipt_context=LifecycleReceiptContext(last_exit=last_exit),
    )
    nodes = {node.id: node for node in chart.subgraphs["recovery"].nodes}
    recovery_node = next(node for node in chart.global_graph.nodes if node.id == "recovery")
    configuration_node = next(node for node in chart.subgraphs["preflight"].nodes if node.id == "configuration")
    incident_receipts = {receipt.label: receipt for receipt in nodes["incident"].receipts}
    global_receipts = {receipt.label: receipt for receipt in recovery_node.receipts}

    assert chart.global_graph.primary_node_id == "recovery"
    assert nodes["incident"].status == "poison"
    assert incident_receipts["prior_halt.trigger"].value == "OUTSIDE_MUTATION"
    assert global_receipts["prior_halt.trigger"].value == "OUTSIDE_MUTATION"
    assert all(receipt.label != "prior_halt.trigger" for receipt in configuration_node.receipts)
    expected = {
        "flatten": (
            "No backend-authored flatten proof is available for this recovery incident.",
            "WAIT_FOR_FLATTEN_PROOF_OR_FREEZE_ACCOUNT",
        ),
        "reconcile_after": (
            "No post-incident reconciliation proof is available for this recovery incident.",
            "RUN_RECONCILIATION_AFTER_RECOVERY",
        ),
        "fresh_run": (
            "No fresh-run or redeploy proof is available for this recovery incident.",
            "WAIT_FOR_REDEPLOY_PROOF",
        ),
    }
    for node_id, (reason, next_step) in expected.items():
        assert nodes[node_id].status == "unknown"
        assert nodes[node_id].summary == reason
        assert nodes[node_id].why == reason
        assert nodes[node_id].operator_next_step == next_step
        assert nodes[node_id].ts_ms is None
        assert nodes[node_id].ts_ms_resolved is False


def test_recovery_lane_surfaces_watchdog_incident_receipts() -> None:
    surface = _surface(incident_headline_notice=_watchdog_notice())
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    recovery_node = next(node for node in chart.global_graph.nodes if node.id == "recovery")
    incident_node = next(node for node in chart.subgraphs["recovery"].nodes if node.id == "incident")
    receipts = {receipt.label: receipt for receipt in incident_node.receipts}

    assert chart.global_graph.primary_node_id == "recovery"
    assert recovery_node.status == "blocked"
    assert recovery_node.ts_ms == _NOW_MS - 7_000
    assert incident_node.status == "blocked"
    assert incident_node.summary == "The watchdog could not prove the account was flat before disconnect."
    assert incident_node.ts_ms == _NOW_MS - 7_000
    assert receipts["watchdog.outcome"].value == "watchdog.flatten_failed"
    assert receipts["watchdog.tier"].value == "critical"
    assert receipts["watchdog.runbook"].value == "watchdog-halt"
    assert receipts["watchdog.occurred_at_ms"].value == str(_NOW_MS - 7_000)
    assert receipts["watchdog.occurred_at_ms"].unit == "ms UTC"


def test_recovery_lane_does_not_mix_prior_halt_with_watchdog_incident() -> None:
    surface = _surface(
        last_exit=InstanceLastExit(
            run_id="prior-run",
            exit_code=1,
            halt_trigger="OUTSIDE_MUTATION",
            halt_at_ms=_NOW_MS - 6_000,
        ),
        incident_headline_notice=_watchdog_notice(),
    )
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    recovery_node = next(node for node in chart.global_graph.nodes if node.id == "recovery")
    incident_node = next(node for node in chart.subgraphs["recovery"].nodes if node.id == "incident")
    receipts = {receipt.label: receipt for receipt in incident_node.receipts}

    assert recovery_node.status == "blocked"
    assert incident_node.status == "blocked"
    assert incident_node.summary == "The watchdog could not prove the account was flat before disconnect."
    assert incident_node.ts_ms == _NOW_MS - 7_000
    assert receipts["watchdog.outcome"].value == "watchdog.flatten_failed"


def test_recovery_lane_surfaces_watchdog_incident_even_when_account_is_frozen() -> None:
    freeze = AccountFreezeEvidence(
        account_id="DU123",
        reason="Unresolved exposure exists after a restart.",
        source="test",
        recorded_at_ms=_NOW_MS,
        operator_next_step="Flatten or reconcile before restarting.",
    )
    surface = _surface(account_freeze=freeze, incident_headline_notice=_watchdog_notice())

    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    recovery_node = next(node for node in chart.global_graph.nodes if node.id == "recovery")
    incident_node = next(node for node in chart.subgraphs["recovery"].nodes if node.id == "incident")

    assert chart.global_graph.primary_node_id == "recovery"
    assert recovery_node.status == "blocked"
    assert incident_node.status == "blocked"
    assert incident_node.summary == "The watchdog could not prove the account was flat before disconnect."


def test_chart_missing_readiness_keeps_preflight_unknown() -> None:
    surface = _surface(readiness=None)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))

    assert chart.global_graph.primary_node_id == "preflight"
    assert _node_status(chart, "preflight") == "unknown"
    assert chart.subgraphs["preflight"].primary_node_id == "readiness_1"
    readiness_node = next(node for node in chart.subgraphs["preflight"].nodes if node.id == "readiness_1")
    assert readiness_node.operator_actionability == "system-only"


def test_blocking_readiness_gate_without_structured_action_stays_operator_actionable() -> None:
    readiness = ReadinessVector(
        kind="live_readiness",
        as_of_ms=_NOW_MS,
        source="engine",
        verdict="ATTENTION",
        summary="Blocked",
        gates=[
            ReadinessGate(
                name="manual_remediation",
                status="fail",
                severity="hard",
                detail="Manual remediation is required.",
                gate_result=GateResult(
                    gate_id="manual_remediation",
                    status="block",
                    source="test",
                    operator_reason="Manual remediation is required.",
                    operator_next_step=None,
                    evidence_at_ms=_NOW_MS,
                ),
            )
        ],
    )
    surface = _surface(readiness=readiness)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    readiness_node = next(node for node in chart.subgraphs["preflight"].nodes if node.id == "readiness_1")

    assert readiness_node.status == "blocked"
    assert readiness_node.operator_actionability == "operator-actionable"


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
    account_safety_node = next(node for node in chart.global_graph.nodes if node.id == "account_safety")
    account_freeze_node = next(node for node in chart.subgraphs["account_safety"].nodes if node.id == "account_freeze")
    global_receipts = {receipt.label: receipt for receipt in account_safety_node.receipts}
    freeze_receipts = {receipt.label: receipt for receipt in account_freeze_node.receipts}

    assert chart.global_graph.primary_node_id == "account_safety"
    assert _node_status(chart, "account_safety") == "freeze"
    assert global_receipts["account_freeze.gate_id"].value == "account.unresolved_exposure"
    assert freeze_receipts["account_freeze.gate_id"].value == "account.unresolved_exposure"
    assert freeze_receipts["account_freeze.next_step"].value == "Flatten or reconcile before restarting."
    assert _node_status(chart, "active") == "inactive"
    assert _edge_status(chart, "preflight_to_account_safety") == "freeze"
    start = next(action for action in chart.actions if action.id == "start_process")
    resume = next(action for action in chart.actions if action.id == "resume")
    assert start.enabled is False
    assert start.reason_code == "ACCOUNT_FROZEN"
    assert start.reason_headline == "Account frozen"
    assert "account-wide freeze" in start.reason_detail
    assert resume.enabled is False
    assert resume.reason_code == "ACCOUNT_FROZEN"
    assert resume.reason_headline == "Account frozen"
    assert "account-wide freeze" in resume.reason_detail


def test_global_activate_node_mirrors_desired_state_receipts() -> None:
    desired_state = DesiredStateView(state="STOPPED", path_status="ok")
    surface = _surface(desired_state=desired_state)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=desired_state)
    activate_node = next(node for node in chart.global_graph.nodes if node.id == "activate")
    desired_node = next(node for node in chart.subgraphs["activate"].nodes if node.id == "desired_state")
    global_receipts = {receipt.label: receipt for receipt in activate_node.receipts}
    desired_receipts = {receipt.label: receipt for receipt in desired_node.receipts}

    assert chart.global_graph.primary_node_id == "activate"
    assert activate_node.status == "blocked"
    assert global_receipts["desired_state.state"].value == "STOPPED"
    assert desired_receipts["desired_state.state"].value == "STOPPED"


def test_global_broker_writer_node_mirrors_publisher_receipts() -> None:
    surface = _surface(activity_publisher=None)
    chart = compose_bot_lifecycle_chart(_SID, surface, desired_state=_desired("RUNNING"))
    broker_writer_node = next(node for node in chart.global_graph.nodes if node.id == "broker_writer")
    publisher_node = next(node for node in chart.subgraphs["broker_writer"].nodes if node.id == "publisher")
    global_receipts = {receipt.label: receipt for receipt in broker_writer_node.receipts}
    publisher_receipts = {receipt.label: receipt for receipt in publisher_node.receipts}

    assert chart.global_graph.primary_node_id == "broker_writer"
    assert broker_writer_node.status == "blocked"
    assert global_receipts["broker_activity.health_state"].value == "unavailable"
    assert publisher_receipts["broker_activity.health_state"].value == "unavailable"


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
    assert reconcile_receipts["sidecar_wal_seq"].value == "7"
    assert reconcile_receipts["sidecar_wal_seq"].unit == "seq"
    assert reconcile_receipts["broker_observed_at_ms"].value == str(_NOW_MS - 4_000)
    assert reconcile_receipts["broker_observed_at_ms"].unit == "ms UTC"
    assert reconcile_receipts["failure_reason"].value == "Broker snapshot disagrees with the intent WAL."
    assert receipt_node.ts_ms == _NOW_MS - 4_000
    assert receipt_node.ts_ms_resolved is True
    assert receipt_receipts["reconciliation.state"].value == "FAILED"
    assert receipt_receipts["sidecar_wal_seq"].value == "7"
    assert receipt_receipts["broker_observed_at_ms"].value == str(_NOW_MS - 4_000)
    assert _edge_status(chart, "account_safety_to_reconcile") == "blocked"
    assert _edge_status(chart, "reconcile_to_activate") == "inactive"
