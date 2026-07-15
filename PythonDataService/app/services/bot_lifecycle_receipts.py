"""Receipt authoring for lifecycle chart facts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from app.schemas.live_runs import (
    ActionCapability,
    BotLifecycleEvent,
    DesiredStateView,
    GateResult,
    InstanceLastExit,
    InstanceProvenance,
    InstanceSizing,
    InstanceStartDefaults,
    LifecycleChartReceipt,
    OperatorGate,
    OperatorSurface,
)
from app.services.bot_lifecycle_receipt_copy import (
    default_receipt_copy,
    desired_state_detail,
    headline,
    reconciliation_next_step_value,
)


@dataclass(frozen=True)
class LifecycleReceiptContext:
    """Status-level facts that are intentionally outside OperatorSurface."""

    symbol: str | None = None
    action_plan: Mapping[str, object] | None = None
    instrument_surface: str | None = None
    start_defaults: InstanceStartDefaults | None = None
    provenance: InstanceProvenance | None = None
    sizing: InstanceSizing | None = None
    last_exit: InstanceLastExit | None = None


def chart_receipt(
    label: str,
    value: object,
    *,
    headline: str | None = None,
    detail: str | None = None,
    unit: str | None = None,
    source: str | None = None,
    gate_id: str | None = None,
    ts_ms: int | None = None,
    ts_ms_resolved: bool | None = None,
) -> LifecycleChartReceipt:
    resolved = ts_ms is not None if ts_ms_resolved is None else ts_ms_resolved
    value_text = str(value)
    default_headline, default_detail = default_receipt_copy(label, value_text, unit)
    return LifecycleChartReceipt(
        label=label,
        value=value_text,
        headline=headline or default_headline,
        detail=detail or default_detail,
        unit=unit,
        source=source,
        gate_id=gate_id,
        ts_ms=ts_ms,
        ts_ms_resolved=resolved,
    )


def honest_empty_receipt(
    label: str,
    *,
    headline: str,
    detail: str | None = None,
    source: str = "lifecycle_chart",
    gate_id: str | None = None,
) -> LifecycleChartReceipt:
    return chart_receipt(
        label,
        "not_available",
        headline=headline,
        detail=detail,
        source=source,
        gate_id=gate_id,
        ts_ms_resolved=False,
    )


def event_receipts(event: BotLifecycleEvent) -> tuple[LifecycleChartReceipt, ...]:
    receipts = [
        chart_receipt(
            "event_type",
            event.event_type,
            source=event.source,
            ts_ms=event.ts_ms,
            ts_ms_resolved=event.ts_ms_resolved,
        ),
        chart_receipt(
            "source_seq",
            event.source_local_seq,
            source=event.source,
            ts_ms=event.ts_ms,
            ts_ms_resolved=event.ts_ms_resolved,
        ),
    ]
    for key in ("intent_id", "order_ref", "order_id", "perm_id", "drop_reason", "ts_ms_source"):
        value = event.payload.get(key)
        if value is not None:
            receipts.append(
                chart_receipt(
                    key,
                    value,
                    source=event.source,
                    ts_ms=event.ts_ms,
                    ts_ms_resolved=event.ts_ms_resolved,
                )
            )
    return tuple(receipts)


def monitor_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    receipts: list[LifecycleChartReceipt] = []
    if surface.daily_order_cap.used is not None:
        receipts.append(
            chart_receipt(
                "monitor.orders_today",
                surface.daily_order_cap.used,
                unit="orders",
                source="operator_surface.daily_order_cap",
            )
        )
    receipts.append(
        honest_empty_receipt(
            "monitor.pnl_authority",
            headline="P&L is not available yet for this bot.",
            detail="The backend has not folded an authoritative per-bot realized or unrealized P&L source into this node.",
            source="operator_surface.current_risk",
        )
    )
    return tuple(receipts)


def daily_order_cap_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    cap = surface.daily_order_cap
    receipts: list[LifecycleChartReceipt] = []
    if cap.used is not None:
        receipts.append(chart_receipt("daily_order_cap.used", cap.used, unit="orders", source="readiness"))
    if cap.limit is not None:
        receipts.append(chart_receipt("daily_order_cap.limit", cap.limit, unit="orders", source="readiness"))
    return tuple(receipts)


def configuration_receipts(
    surface: OperatorSurface,
    context: LifecycleReceiptContext | None = None,
) -> tuple[LifecycleChartReceipt, ...]:
    receipts: list[LifecycleChartReceipt] = []
    if context is not None:
        receipts.extend(_preflight_context_receipts(surface, context))
    reason_codes = surface.configuration.reason_codes
    receipts.extend(
        chart_receipt(
            "configuration.reason_code",
            code,
            headline=headline("configuration_reason", code),
            detail="This backend configuration check must clear before pre-flight is ready.",
            source="operator_surface.configuration",
        )
        for code in reason_codes
    )
    if not reason_codes:
        receipts.append(
            chart_receipt(
                "configuration.verdict",
                surface.configuration.verdict,
                headline=f"Configuration is {surface.configuration.verdict.lower()}.",
                detail="The backend found no configuration reason codes for this snapshot.",
                source="operator_surface.configuration",
            )
        )
    return tuple(receipts)


def command_loop_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    freshness = surface.runtime_freshness
    if freshness is None:
        return (
            honest_empty_receipt(
                "runtime_freshness.command_loop",
                headline="Command-loop freshness is not available yet.",
                detail="No child runtime is currently bound to author command-loop freshness.",
                source="operator_surface.runtime_freshness",
            ),
        )
    return tuple(
        chart_receipt(
            "runtime_freshness.command_loop.stale_reason_code",
            code,
            headline="The command-loop freshness check is stale.",
            detail="The backend reported a stale command-loop condition for this node.",
            source="operator_surface.runtime_freshness.command_loop",
        )
        for code in freshness.command_loop.stale_reason_codes
    )


def incident_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    notice = surface.incident_headline
    if notice is None:
        return ()
    ts_ms = notice.occurred_at_ms
    receipts = [
        chart_receipt("watchdog.outcome", notice.code, source="operator_incident", ts_ms=ts_ms),
        chart_receipt("watchdog.tier", notice.tier, source="operator_incident", ts_ms=ts_ms),
    ]
    if notice.runbook_slug is not None:
        receipts.append(chart_receipt("watchdog.runbook", notice.runbook_slug, source="operator_incident", ts_ms=ts_ms))
    if ts_ms is not None:
        receipts.append(
            chart_receipt(
                "watchdog.occurred_at_ms",
                ts_ms,
                unit="ms UTC",
                source="operator_incident",
                ts_ms=ts_ms,
            )
        )
    return tuple(receipts)


def reconciliation_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    reconciliation = surface.reconciliation
    if reconciliation is None:
        return ()
    ts_ms = reconciliation.last_reconcile_ms
    receipts = [
        chart_receipt("reconciliation.state", reconciliation.state, source="reconciliation_projection", ts_ms=ts_ms),
        chart_receipt(
            "adopted_intent_count",
            len(reconciliation.adopted_intent_ids),
            source="reconciliation_projection",
            ts_ms=ts_ms,
        ),
    ]
    if reconciliation.last_reconcile_ms is not None:
        receipts.append(
            chart_receipt(
                "last_reconcile_ms",
                reconciliation.last_reconcile_ms,
                unit="ms UTC",
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    if reconciliation.sidecar_wal_seq is not None:
        receipts.append(
            chart_receipt(
                "sidecar_wal_seq",
                reconciliation.sidecar_wal_seq,
                unit="seq",
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    if reconciliation.broker_observed_at_ms is not None:
        receipts.append(
            chart_receipt(
                "broker_observed_at_ms",
                reconciliation.broker_observed_at_ms,
                unit="ms UTC",
                source="reconciliation_projection",
                ts_ms=reconciliation.broker_observed_at_ms,
            )
        )
    if reconciliation.failure_reason:
        receipts.append(
            chart_receipt(
                "failure_reason",
                reconciliation.failure_reason,
                headline="Reconciliation is blocked by a broker and engine mismatch.",
                detail=reconciliation.failure_reason,
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    if reconciliation.state in {"FAILED", "STALE", "NOT_AVAILABLE", "IN_PROGRESS"}:
        receipts.append(
            chart_receipt(
                "reconciliation.next_step",
                reconciliation_next_step_value(reconciliation.state),
                headline=headline("reconciliation_next_step", reconciliation.state),
                detail="The backend keeps this as a reconcile gate until the receipt is clean or adopted.",
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    return tuple(receipts)


def account_safety_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    return (
        *account_identity_receipts(surface),
        *broker_safety_receipts(surface),
        *broker_connection_receipts(surface),
        *current_risk_receipts(surface),
    )


def account_freeze_receipts(gate_result: GateResult | None) -> tuple[LifecycleChartReceipt, ...]:
    if gate_result is None:
        return (
            honest_empty_receipt(
                "account_freeze.gate",
                headline="No account freeze gate is active.",
                detail="The backend found no freeze gate in this account-safety snapshot.",
                source="operator_surface.account_freeze",
            ),
        )
    return (
        chart_receipt(
            "account_freeze.gate_id",
            gate_result.gate_id,
            headline="Account freeze gate evidence is present.",
            detail=gate_result.operator_reason,
            source=gate_result.source,
            gate_id=gate_result.gate_id,
            ts_ms=gate_result.evidence_at_ms,
        ),
        chart_receipt(
            "account_freeze.next_step",
            gate_result.operator_next_step or "review_account_freeze",
            headline="The backend authored the account-freeze next step.",
            detail="The raw next-step token is preserved in the audit payload.",
            source=gate_result.source,
            gate_id=gate_result.gate_id,
            ts_ms=gate_result.evidence_at_ms,
        ),
    )


def account_identity_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    receipts: list[LifecycleChartReceipt] = []
    clerk = surface.account_clerk
    if clerk is not None:
        receipts.append(
            chart_receipt(
                "account.account_id",
                clerk.account_id,
                headline=f"Account Clerk is scoped to account {clerk.account_id}.",
                detail="This is Account Clerk evidence for the bot's account context.",
                source=clerk.source or "operator_surface.account_clerk",
                ts_ms=clerk.recorded_at_ms,
            )
        )
    consistency = surface.broker_observation_consistency
    if consistency is not None:
        receipts.append(
            chart_receipt(
                "account_identity.verdict",
                consistency.verdict,
                headline=headline("account_identity", consistency.verdict),
                detail="This compares the child runtime broker account with the data-plane broker observation.",
                source="operator_surface.broker_observation_consistency",
                ts_ms=consistency.compared_at_ms,
            )
        )
        if consistency.child_account is not None:
            receipts.append(
                chart_receipt(
                    "account_identity.child_account_id",
                    consistency.child_account,
                    headline=f"The child runtime reports account {consistency.child_account}.",
                    source="operator_surface.broker_observation_consistency",
                    ts_ms=consistency.compared_at_ms,
                )
            )
        if consistency.data_plane_account is not None:
            receipts.append(
                chart_receipt(
                    "account_identity.data_plane_account_id",
                    consistency.data_plane_account,
                    headline=f"The data plane reports account {consistency.data_plane_account}.",
                    source="operator_surface.broker_observation_consistency",
                    ts_ms=consistency.compared_at_ms,
                )
            )
    if receipts:
        return tuple(receipts)
    return (
        honest_empty_receipt(
            "account_identity.fold",
            headline="Account identity proof has not been folded into this node yet.",
            detail="The inspector will not guess account cleanliness from a separate endpoint.",
            source="operator_surface.account_clerk",
        ),
    )


def broker_safety_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    receipts = [
        chart_receipt(
            "broker.safety_verdict",
            surface.broker.safety_verdict,
            headline=headline("broker_safety", surface.broker.safety_verdict),
            detail="Paper/live safety is a backend broker-safety verdict, separate from connection state.",
            source="operator_surface.broker",
        )
    ]
    if surface.execution is not None:
        receipts.append(
            chart_receipt(
                "execution.posture",
                surface.execution.posture,
                headline=headline("execution_posture", surface.execution.posture),
                detail="Execution posture is authored by the backend from engine runtime evidence.",
                source="operator_surface.execution",
            )
        )
    return tuple(receipts)


def broker_connection_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    return (
        chart_receipt(
            "broker.connection",
            surface.broker.connection,
            headline=headline("broker_connection", surface.broker.connection),
            detail="Connection state is independent from paper/live safety.",
            source="operator_surface.broker",
        ),
    )


def current_risk_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    risk = surface.current_risk
    receipts = [
        chart_receipt(
            "current_risk.posture",
            risk.posture,
            headline=headline("risk_posture", risk.posture),
            detail="Risk posture is folded from the bot-owned broker position view.",
            source="operator_surface.current_risk",
        )
    ]
    if risk.pending_order_count is None:
        receipts.append(
            honest_empty_receipt(
                "current_risk.pending_order_count",
                headline="Pending-order count is not available yet.",
                detail="The broker state needed to count pending orders is unavailable.",
                source="operator_surface.current_risk",
            )
        )
    else:
        receipts.append(
            chart_receipt(
                "current_risk.pending_order_count",
                risk.pending_order_count,
                unit="orders",
                source="operator_surface.current_risk",
            )
        )
    return tuple(receipts)


def account_clerk_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    clerk = surface.account_clerk
    if clerk is None:
        return ()
    ts_ms = clerk.recorded_at_ms
    return (
        chart_receipt("account_clerk.phase", clerk.phase, source=clerk.source, ts_ms=ts_ms),
        chart_receipt(
            "account_clerk.generation",
            clerk.generation if clerk.generation is not None else "unknown",
            source=clerk.source,
            ts_ms=ts_ms,
        ),
        chart_receipt(
            "account_clerk.lease_active",
            str(clerk.lease_active).lower(),
            source=clerk.source,
            ts_ms=ts_ms,
        ),
    )


def readiness_gate_receipts(gate: OperatorGate) -> tuple[LifecycleChartReceipt, ...]:
    gate_result = gate.gate_result
    receipts = [
        chart_receipt(
            "readiness_gate.status",
            gate_result.status,
            headline=headline("readiness_gate_status", gate_result.status, name=gate.name),
            detail=gate_result.operator_reason or gate.detail,
            source=gate_result.source,
            gate_id=gate_result.gate_id,
            ts_ms=gate_result.evidence_at_ms,
        )
    ]
    if gate_result.operator_next_step:
        receipts.append(
            chart_receipt(
                "readiness_gate.next_step",
                gate_result.operator_next_step,
                headline="The backend authored the next step for this readiness gate.",
                detail="The raw token is kept in the audit payload.",
                source=gate_result.source,
                gate_id=gate_result.gate_id,
                ts_ms=gate_result.evidence_at_ms,
            )
        )
    return tuple(receipts)


def desired_state_receipts(
    desired_state: DesiredStateView | None,
    *,
    effective_state: str | None,
) -> tuple[LifecycleChartReceipt, ...]:
    if desired_state is None:
        return (
            honest_empty_receipt(
                "desired_state.sidecar",
                headline="Desired-state evidence is not available yet.",
                detail="The desired-state sidecar could not be resolved for this snapshot.",
                source="desired_state",
            ),
        )
    state = effective_state or "UNKNOWN"
    receipts = [
        chart_receipt(
            "desired_state.state",
            state,
            headline=f"Durable desired state is {state}.",
            detail=desired_state_detail(str(desired_state.path_status), desired_state.updated_at_ms),
            source="desired_state",
            ts_ms=desired_state.updated_at_ms,
        ),
        chart_receipt(
            "desired_state.path_status",
            desired_state.path_status,
            headline=headline("desired_path_status", desired_state.path_status),
            detail="The backend resolves absence as the documented effective RUNNING default.",
            source="desired_state",
            ts_ms=desired_state.updated_at_ms,
        ),
    ]
    if desired_state.updated_by:
        receipts.append(
            chart_receipt(
                "desired_state.updated_by",
                desired_state.updated_by,
                headline=f"Desired state was last set by {desired_state.updated_by}.",
                source="desired_state",
                ts_ms=desired_state.updated_at_ms,
            )
        )
    if desired_state.reason:
        receipts.append(
            chart_receipt(
                "desired_state.reason",
                desired_state.reason,
                headline="Desired-state reason is recorded.",
                detail=desired_state.reason,
                source="desired_state",
                ts_ms=desired_state.updated_at_ms,
            )
        )
    return tuple(receipts)


def prior_halt_receipts(last_exit: InstanceLastExit | None) -> tuple[LifecycleChartReceipt, ...]:
    if last_exit is None or last_exit.halt_trigger is None:
        return (
            honest_empty_receipt(
                "prior_halt.trigger",
                headline="Prior halt trigger evidence is not available.",
                detail="The prior-run classifier reported a halt, but no specific halt trigger was folded into this snapshot.",
                source="last_exit.poisoned_flag",
            ),
        )
    return (
        chart_receipt(
            "prior_halt.trigger",
            last_exit.halt_trigger,
            headline="The previous run left a safety halt trigger.",
            detail="This comes from the last-exit poison flag; the prior-run field alone is only a coarse classification.",
            source="last_exit.poisoned_flag",
            ts_ms=last_exit.halt_at_ms,
        ),
    )


def capability_receipts(name: str, capability: ActionCapability) -> tuple[LifecycleChartReceipt, ...]:
    receipts = [
        chart_receipt(
            f"action.{name}.enabled",
            capability.enabled,
            headline=headline(
                "capability_enabled",
                str(capability.enabled).lower(),
                label=name.replace("_", " ").capitalize(),
            ),
            detail="Action eligibility is authored by the backend capability evaluator.",
            source=f"operator_surface.actions.{name}",
        )
    ]
    if capability.disabled_reason_code:
        receipts.append(
            chart_receipt(
                f"action.{name}.disabled_reason",
                capability.disabled_reason_code,
                headline=headline("capability_disabled", "default", label=name.replace("_", " ").capitalize()),
                detail="The raw disabled reason is preserved in the audit payload.",
                source=f"operator_surface.actions.{name}",
            )
        )
    return tuple(receipts)


def broker_activity_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    health = surface.broker_activity_health
    if health is None:
        return (
            honest_empty_receipt(
                "broker_activity.publisher",
                headline="Broker-activity publisher evidence is not registered for this bot yet.",
                detail="No live binding was available to fold publisher health into this node.",
                source="operator_surface.broker_activity_health",
            ),
        )
    facts = health.facts
    return (
        chart_receipt(
            "broker_activity.health_state",
            health.state,
            headline=headline("broker_activity_state", health.state),
            detail="This is publisher capture health, not order-execution proof.",
            source="operator_surface.broker_activity_health",
        ),
        chart_receipt(
            "broker_activity.publisher_registered",
            facts.publisher_registered,
            headline="Broker-activity publisher registration was checked.",
            source="operator_surface.broker_activity_health.facts",
        ),
        chart_receipt(
            "broker_activity.publisher_running",
            facts.publisher_running,
            headline="Broker-activity publisher running state was checked.",
            source="operator_surface.broker_activity_health.facts",
        ),
        chart_receipt(
            "broker_activity.latest_row_seq",
            facts.latest_row_seq if facts.latest_row_seq is not None else "not_available",
            source="operator_surface.broker_activity_health.facts",
        ),
    )


def broker_snapshot_receipts() -> tuple[LifecycleChartReceipt, ...]:
    return (
        honest_empty_receipt(
            "broker_snapshot.fold",
            headline="Broker snapshot positions and intents are not folded into this node yet.",
            detail="The full broker snapshot remains outside this lifecycle receipt until the backend fold ships.",
            source="lifecycle_chart.broker_snapshot",
        ),
    )


def signal_gap_receipts() -> tuple[LifecycleChartReceipt, ...]:
    return (
        honest_empty_receipt(
            "strategy_signal.evidence",
            headline="No signal evidence emitted yet.",
            detail="The strategy-signal node has no direct event emitter in this lifecycle projection.",
            source="lifecycle_projection.signal",
        ),
    )


def broker_ack_gap_receipts() -> tuple[LifecycleChartReceipt, ...]:
    return (
        honest_empty_receipt(
            "broker_acknowledgment.evidence",
            headline="No direct broker acknowledgment evidence emitted yet.",
            detail="Broker execution rows are not folded into this lifecycle node yet.",
            source="lifecycle_projection.broker_ack",
        ),
    )


def _preflight_context_receipts(
    surface: OperatorSurface,
    context: LifecycleReceiptContext,
) -> list[LifecycleChartReceipt]:
    receipts: list[LifecycleChartReceipt] = []
    if context.symbol:
        receipts.append(
            chart_receipt(
                "run.symbol",
                context.symbol,
                headline=f"This bot is configured for {context.symbol}.",
                detail="The backend resolved the symbol from the committed run context.",
                source="status.symbol",
            )
        )
    if context.instrument_surface:
        receipts.append(
            chart_receipt(
                "instrument_surface.plan",
                context.instrument_surface,
                headline=headline("instrument_surface", context.instrument_surface),
                detail="This comes from the strategy registry instrument surface.",
                source="strategy_registry.instrument_surface",
            )
        )
    receipts.append(
        chart_receipt(
            "action_plan.consumption",
            surface.action_plan.consumption,
            headline=headline("action_plan_consumption", surface.action_plan.consumption),
            detail="The backend folds the declared action plan into an operator-surface verdict.",
            source="operator_surface.action_plan",
        )
    )
    if context.action_plan is not None:
        receipts.append(
            chart_receipt(
                "action_plan.declared",
                "present",
                headline="A committed action plan is present for this run.",
                detail=f"Action plan keys: {', '.join(sorted(str(key) for key in context.action_plan))}.",
                source="run_ledger.live_config.action",
            )
        )
    else:
        receipts.append(
            honest_empty_receipt(
                "action_plan.declared",
                headline="No committed action plan is available for this run.",
                detail="The backend will not infer an action plan for the inspector.",
                source="run_ledger.live_config.action",
            )
        )
    if context.start_defaults is not None and context.start_defaults.strategy:
        receipts.append(
            chart_receipt(
                "run.strategy_key",
                context.start_defaults.strategy,
                headline=f"This run starts the {context.start_defaults.strategy} strategy module.",
                detail="The strategy key is read from the committed run ledger.",
                source="run_ledger.strategy_key",
            )
        )
    if context.provenance is not None:
        receipts.append(
            chart_receipt(
                "run.provenance.run_id",
                context.provenance.run_id,
                headline="Committed run identity is present.",
                detail="The run id fingerprints the committed deploy inputs.",
                source="run_ledger",
                ts_ms=context.provenance.created_at_ms,
            )
        )
        receipts.extend(_live_config_receipts(context.provenance.live_config))
    if context.sizing is not None and context.sizing.preset is not None:
        receipts.append(
            chart_receipt(
                "sizing.preset",
                context.sizing.preset,
                headline=f"Sizing preset is {context.sizing.preset}.",
                detail="Sizing is part of the committed run configuration.",
                source="run_ledger.live_config.sizing",
            )
        )
    return receipts


def _live_config_receipts(live_config: Mapping[str, object]) -> list[LifecycleChartReceipt]:
    receipts: list[LifecycleChartReceipt] = []
    candidates = (
        ("bar_resolution", "Bar resolution"),
        ("resolution", "Bar resolution"),
        ("consolidator_period_min", "Bar period"),
        ("session", "Trading session"),
        ("session_policy", "Trading session policy"),
        ("warmup_bars", "Warmup bars"),
        ("warmup_period", "Warmup period"),
    )
    for key, label in candidates:
        value = live_config.get(key)
        if value is None:
            continue
        receipts.append(
            chart_receipt(
                f"live_config.{key}",
                _stable_config_value(value),
                headline=f"{label} is recorded in the committed live config.",
                detail="This field is shown only because the backend found it in live_config.",
                source="run_ledger.live_config",
            )
        )
    return receipts


def _stable_config_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


__all__ = [
    "LifecycleReceiptContext",
    "account_clerk_receipts",
    "account_freeze_receipts",
    "account_safety_receipts",
    "broker_ack_gap_receipts",
    "broker_activity_receipts",
    "broker_connection_receipts",
    "broker_safety_receipts",
    "broker_snapshot_receipts",
    "capability_receipts",
    "chart_receipt",
    "command_loop_receipts",
    "configuration_receipts",
    "daily_order_cap_receipts",
    "desired_state_receipts",
    "event_receipts",
    "honest_empty_receipt",
    "incident_receipts",
    "monitor_receipts",
    "prior_halt_receipts",
    "readiness_gate_receipts",
    "reconciliation_receipts",
    "signal_gap_receipts",
]
