from __future__ import annotations

from collections.abc import Iterable

from app.schemas.live_runs import (
    ActionCapability,
    BotLifecycleEvent,
    BrokerObservationConsistency,
    DesiredStateView,
    GateResult,
    InstanceBrokerView,
    InstanceProcessView,
    InstanceSizing,
    InstanceStartDefaults,
    LifecycleChartReceipt,
    LiveBinding,
    OperatorGate,
    OperatorSurface,
    OperatorSurfaceAccountOwner,
    ReadinessGate,
    ReadinessVector,
    ReconciliationReceipt,
)
from app.services.bot_lifecycle_receipts import (
    LifecycleReceiptContext,
    account_freeze_receipts,
    account_identity_receipts,
    account_owner_receipts,
    broker_ack_gap_receipts,
    broker_activity_receipts,
    broker_snapshot_receipts,
    capability_receipts,
    command_loop_receipts,
    configuration_receipts,
    current_risk_receipts,
    desired_state_receipts,
    event_receipts,
    readiness_gate_receipts,
    reconciliation_receipts,
    signal_gap_receipts,
)
from app.services.operator_surface import compute_operator_surface
from app.services.runtime_freshness import DomainFreshness, RuntimeFreshness

_NOW_MS = 1_700_000_000_000
_RUN_ID = "run-receipts-x"
_SID = "bot-receipts"
_NAMESPACE = "learn-ai/bot-receipts/v1"


class _Publisher:
    is_running = True
    latest_row_ms = _NOW_MS - 1_000

    def last_persisted_seq(self) -> int:
        return 7


def _receipts_by_label(receipts: Iterable[LifecycleChartReceipt]) -> dict[str, LifecycleChartReceipt]:
    return {receipt.label: receipt for receipt in receipts}


def _desired(state: str = "RUNNING") -> DesiredStateView:
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


def _reconciliation_receipt(status: str = "passed") -> ReconciliationReceipt:
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
        "desired_state": _desired(),
        "reconciliation_receipt": _reconciliation_receipt(),
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


def _runtime_freshness() -> RuntimeFreshness:
    return RuntimeFreshness(
        command_loop=DomainFreshness(
            state="STALE",
            age_ms=5_000,
            stale_reason_codes=["COMMAND_LOOP_STALE"],
        ),
        broker=DomainFreshness(state="FRESH", age_ms=100),
        bar_loop=DomainFreshness(state="FRESH", age_ms=100),
        control_plane=DomainFreshness(state="FRESH", age_ms=100),
        posture_demoted=True,
        effective_posture="UNKNOWN",
    )


def test_desired_state_receipts_use_canonical_effective_state() -> None:
    corrupt_view = DesiredStateView(state="PAUSED", path_status="corrupt")

    receipts = _receipts_by_label(desired_state_receipts(corrupt_view, effective_state=None))

    assert receipts["desired_state.state"].value == "UNKNOWN"
    assert receipts["desired_state.state"].headline == "Durable desired state is UNKNOWN."
    assert receipts["desired_state.path_status"].headline == "Desired-state sidecar is corrupt."


def test_desired_state_receipts_cover_absent_and_missing_sidecars() -> None:
    missing = _receipts_by_label(desired_state_receipts(None, effective_state=None))
    absent_view = DesiredStateView(state=None, path_status="absent")
    absent = _receipts_by_label(desired_state_receipts(absent_view, effective_state="RUNNING"))

    assert missing["desired_state.sidecar"].value == "not_available"
    assert missing["desired_state.sidecar"].headline == "Desired-state evidence is not available yet."
    assert absent["desired_state.state"].value == "RUNNING"
    assert absent["desired_state.path_status"].headline == "Desired-state sidecar is absent; effective state is RUNNING."


def test_account_identity_receipts_cover_fold_gap_and_populated_evidence() -> None:
    empty = _receipts_by_label(account_identity_receipts(_surface()))
    populated_surface = _surface(
        account_owner=OperatorSurfaceAccountOwner(
            account_id="DU123",
            generation=4,
            phase="accepting",
            recorded_at_ms=_NOW_MS - 1_000,
            source="test",
        ),
        broker_observation_consistency=BrokerObservationConsistency(
            verdict="CONSISTENT",
            child_account="DU123",
            data_plane_account="DU123",
            compared_at_ms=_NOW_MS - 1_000,
        ),
    )
    populated = _receipts_by_label(account_identity_receipts(populated_surface))

    assert empty["account_identity.fold"].headline == "Account identity proof has not been folded into this node yet."
    assert populated["account.account_id"].value == "DU123"
    assert populated["account_identity.verdict"].headline == "Broker account observations agree."
    assert populated["account_identity.child_account_id"].value == "DU123"
    assert populated["account_identity.data_plane_account_id"].value == "DU123"


def test_account_freeze_receipts_cover_gate_and_empty_state() -> None:
    empty = _receipts_by_label(account_freeze_receipts(None))
    frozen = _receipts_by_label(
        account_freeze_receipts(
            GateResult(
                gate_id="account.unresolved_exposure",
                status="freeze",
                source="account_artifacts",
                operator_reason="Unresolved exposure exists after a restart.",
                operator_next_step="Flatten or reconcile before restarting.",
                evidence_at_ms=_NOW_MS,
            )
        )
    )

    assert empty["account_freeze.gate"].headline == "No account freeze gate is active."
    assert frozen["account_freeze.gate_id"].value == "account.unresolved_exposure"
    assert frozen["account_freeze.gate_id"].gate_id == "account.unresolved_exposure"
    assert frozen["account_freeze.next_step"].value == "Flatten or reconcile before restarting."


def test_current_risk_receipts_distinguish_unknown_pending_orders_from_zero() -> None:
    unknown = _receipts_by_label(current_risk_receipts(_surface(broker=None)))
    flat = _receipts_by_label(current_risk_receipts(_surface()))

    assert unknown["current_risk.pending_order_count"].value == "not_available"
    assert unknown["current_risk.pending_order_count"].headline == "Pending-order count is not available yet."
    assert flat["current_risk.pending_order_count"].value == "0"
    assert flat["current_risk.pending_order_count"].headline == "0 pending order(s) are currently attributed to this bot."


def test_command_loop_receipts_cover_missing_runtime_and_stale_codes() -> None:
    missing = _receipts_by_label(command_loop_receipts(_surface(runtime_freshness=None)))
    stale = _receipts_by_label(command_loop_receipts(_surface(runtime_freshness=_runtime_freshness())))

    assert missing["runtime_freshness.command_loop"].value == "not_available"
    assert missing["runtime_freshness.command_loop"].headline == "Command-loop freshness is not available yet."
    assert stale["runtime_freshness.command_loop.stale_reason_code"].value == "COMMAND_LOOP_STALE"
    assert stale["runtime_freshness.command_loop.stale_reason_code"].headline == "The command-loop freshness check is stale."


def test_broker_activity_receipts_cover_missing_and_registered_publisher() -> None:
    missing = _receipts_by_label(broker_activity_receipts(_surface(live_binding=None)))
    ready = _receipts_by_label(broker_activity_receipts(_surface()))

    assert missing["broker_activity.publisher"].value == "not_available"
    assert missing["broker_activity.publisher"].headline == "Broker-activity publisher evidence is not registered for this bot yet."
    assert ready["broker_activity.health_state"].value == "ready"
    assert ready["broker_activity.health_state"].headline == "Broker-activity publisher is ready."
    assert ready["broker_activity.latest_row_seq"].value == "7"
    assert ready["broker_activity.latest_row_seq"].headline == "Latest broker-activity row sequence is 7."


def test_broker_activity_receipts_cover_no_latest_row_sequence() -> None:
    class PublisherWithoutRows:
        is_running = True
        latest_row_ms = None

        def last_persisted_seq(self) -> int:
            return 0

    receipts = _receipts_by_label(
        broker_activity_receipts(
            _surface(
                activity_publisher=PublisherWithoutRows(),
                activity_publisher_registered_at_ms=_NOW_MS - 10_000,
            )
        )
    )

    assert receipts["broker_activity.latest_row_seq"].value == "not_available"
    assert receipts["broker_activity.latest_row_seq"].headline == "No broker-activity row sequence is available yet."


def test_reconciliation_receipts_cover_failure_next_step() -> None:
    surface = _surface(reconciliation_receipt=_reconciliation_receipt("failed"))

    receipts = _receipts_by_label(reconciliation_receipts(surface))

    assert receipts["reconciliation.state"].headline == "Reconciliation failed."
    assert receipts["failure_reason"].detail == "Broker snapshot disagrees with the intent WAL."
    assert receipts["reconciliation.next_step"].value == "run_reconciliation_before_resume"
    assert receipts["reconciliation.next_step"].headline == "Run reconciliation before resuming."


def test_configuration_receipts_cover_verdict_and_reason_code() -> None:
    ready = _receipts_by_label(configuration_receipts(_surface()))
    ready_with_context = _receipts_by_label(
        configuration_receipts(_surface(), LifecycleReceiptContext(symbol="SPY"))
    )
    missing_strategy = configuration_receipts(_surface(start_defaults=None))
    reason_codes = {receipt.value for receipt in missing_strategy if receipt.label == "configuration.reason_code"}

    assert ready["configuration.verdict"].headline == "Configuration is ready."
    assert ready_with_context["configuration.verdict"].headline == "Configuration is ready."
    assert ready_with_context["run.symbol"].value == "SPY"
    assert "STRATEGY_KEY_MISSING" in reason_codes
    assert "MAX_ORDERS_CAP_UNSET" in reason_codes
    assert {
        receipt.headline for receipt in missing_strategy if receipt.value == "STRATEGY_KEY_MISSING"
    } == {"The run is missing its strategy key."}


def test_event_account_owner_capability_and_readiness_receipts_have_prose() -> None:
    event = BotLifecycleEvent(
        event_id="event-12",
        bot_id=_SID,
        run_id=_RUN_ID,
        node_id="intent_wal",
        event_type="INTENT_ACCEPTED",
        category="order",
        status="passed",
        source="intent_events",
        source_rank=10,
        source_local_seq=12,
        summary="Intent accepted.",
        payload={"intent_id": "intent-12"},
        ts_ms=_NOW_MS,
    )
    owner_surface = _surface(
        account_owner=OperatorSurfaceAccountOwner(
            account_id="DU123",
            generation=4,
            phase="accepting",
            recorded_at_ms=_NOW_MS,
            source="test",
        )
    )
    disabled = ActionCapability(
        enabled=False,
        effect="DURABLE_ONLY",
        disabled_reason_code="NO_LIVE_BINDING",
    )
    gate = OperatorGate(
        name="engine_ready",
        status="fail",
        severity="hard",
        detail="Engine readiness failed.",
        gate_result=GateResult(
            gate_id="engine_ready",
            status="block",
            source="test",
            operator_reason="Engine readiness failed.",
            operator_next_step="FIX_ENGINE_READY",
            evidence_at_ms=_NOW_MS,
        ),
    )

    event_by_label = _receipts_by_label(event_receipts(event))
    owner_by_label = _receipts_by_label(account_owner_receipts(owner_surface))
    capability_by_label = _receipts_by_label(capability_receipts("resume", disabled))
    gate_by_label = _receipts_by_label(readiness_gate_receipts(gate))

    assert event_by_label["intent_id"].headline == "Order intent intent-12 was recorded."
    assert owner_by_label["account_owner.generation"].headline == "AccountOwner generation is 4."
    assert capability_by_label["action.resume.enabled"].headline == "Resume is currently blocked."
    assert capability_by_label["action.resume.disabled_reason"].headline == "Resume has a backend-authored disabled reason."
    assert gate_by_label["readiness_gate.status"].headline == "engine_ready is blocking this lifecycle step."
    assert gate_by_label["readiness_gate.next_step"].value == "FIX_ENGINE_READY"


def test_gap_receipts_are_honest_empty_receipts() -> None:
    broker_snapshot = _receipts_by_label(broker_snapshot_receipts())
    signal = _receipts_by_label(signal_gap_receipts())
    broker_ack = _receipts_by_label(broker_ack_gap_receipts())

    assert broker_snapshot["broker_snapshot.fold"].value == "not_available"
    assert signal["strategy_signal.evidence"].headline == "No signal evidence emitted yet."
    assert broker_ack["broker_acknowledgment.evidence"].headline == "No direct broker acknowledgment evidence emitted yet."
