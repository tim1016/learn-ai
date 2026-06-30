"""Backend-authored bot lifecycle chart projection.

The Overview tab renders this graph verbatim.  The operator surface remains the
single source of truth for lifecycle, gate, and action capability facts; this
module only adapts those facts into a visual graph contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from app.schemas.live_runs import (
    ActionCapability,
    BotLifecycleChartView,
    BotLifecycleEvent,
    DesiredStateView,
    GateResult,
    HostProcessStartCapability,
    LifecycleChartAction,
    LifecycleChartActionId,
    LifecycleChartEdge,
    LifecycleChartGraph,
    LifecycleChartLane,
    LifecycleChartNode,
    LifecycleChartReceipt,
    LifecycleChartStatus,
    OperatorGate,
    OperatorSurface,
)
from app.services.bot_lifecycle_projection import latest_event_for_node, lifecycle_status_label

_BLOCKING_PRIORITY: tuple[LifecycleChartStatus, ...] = (
    "freeze",
    "poison",
    "blocked",
    "unknown",
)
_MAIN_PATH = ("deploy", "preflight", "account_safety", "reconcile", "activate", "active")


@dataclass(frozen=True)
class NodeDef:
    id: str
    label: str
    lane: LifecycleChartLane
    technical_label: str | None = None
    fact_id: str | None = None


@dataclass(frozen=True)
class EdgeDef:
    source: str
    target: str
    label: str | None = None
    kind: Literal["main", "branch", "linear"] = "linear"


@dataclass(frozen=True)
class GraphDef:
    graph_id: str
    title: str
    nodes: tuple[NodeDef, ...]
    primary_node_id: str | None = None
    edges: tuple[EdgeDef, ...] = ()


@dataclass(frozen=True)
class NodeFact:
    status: LifecycleChartStatus
    evidence: str | None
    technical_label: str | None = None
    status_label: str | None = None
    why: str | None = None
    operator_next_step: str | None = None
    ts_ms: int | None = None
    ts_ms_resolved: bool = False
    receipts: tuple[LifecycleChartReceipt, ...] = ()


@dataclass(frozen=True)
class LifecycleFacts:
    primary_node_id: str
    base_statuses: Mapping[str, LifecycleChartStatus]
    node_facts: Mapping[str, NodeFact]


GLOBAL_GRAPH = GraphDef(
    graph_id="global",
    title="Bot lifecycle overview",
    nodes=(
        NodeDef("deploy", "Deploy or start", "bot", "Host process"),
        NodeDef("preflight", "Pre-flight gates", "bot", "Config and readiness"),
        NodeDef("account_safety", "Account safety", "account", "Freeze, safety, connection"),
        NodeDef("reconcile", "Reconcile broker state", "broker", "Cold-start receipt"),
        NodeDef("activate", "Activate bot", "bot", "Durable desired state"),
        NodeDef("active", "Monitor live bot", "bot", "Running loop"),
        NodeDef("submit_order", "Submit order path", "broker", "Signal -> WAL -> order"),
        NodeDef("broker_writer", "Broker activity", "broker", "Publisher health"),
        NodeDef("recovery", "Recovery lane", "recovery", "Flatten, halt, redeploy"),
    ),
    edges=(
        EdgeDef("deploy", "preflight", kind="main"),
        EdgeDef("preflight", "account_safety", kind="main"),
        EdgeDef("account_safety", "reconcile", kind="main"),
        EdgeDef("reconcile", "activate", kind="main"),
        EdgeDef("activate", "active", kind="main"),
        EdgeDef("active", "submit_order", "Signal arrives", "branch"),
        EdgeDef("submit_order", "broker_writer", "Order reaches broker", "branch"),
        EdgeDef("active", "recovery", "Safety incident", "branch"),
    ),
)


SUBGRAPH_DEFS: Mapping[str, GraphDef] = {
    "deploy": GraphDef(
        graph_id="deploy",
        title="Deploy and start internals",
        primary_node_id="host_state",
        nodes=(
            NodeDef("host_state", "Host state", "bot", fact_id="deploy.host_state"),
            NodeDef("start_settings", "Start settings", "bot", "Backend-authored request"),
            NodeDef("start_or_redeploy", "Start or redeploy", "bot", "Fresh run when retired"),
        ),
    ),
    "preflight": GraphDef(
        graph_id="preflight",
        title="Pre-flight internals",
        primary_node_id="configuration",
        nodes=(NodeDef("configuration", "Configuration", "bot", "Deploy proof"),),
    ),
    "account_safety": GraphDef(
        graph_id="account_safety",
        title="Account-safety internals",
        primary_node_id="account_freeze",
        nodes=(
            NodeDef("account_freeze", "Account freeze", "account", "Account-wide stop sign"),
            NodeDef("broker_safety", "Broker safety", "account"),
            NodeDef("broker_connection", "Broker connection", "broker"),
            NodeDef("risk_posture", "Current risk", "account"),
        ),
    ),
    "reconcile": GraphDef(
        graph_id="reconcile",
        title="Reconciliation internals",
        primary_node_id="receipt",
        nodes=(
            NodeDef("receipt", "Receipt state", "broker"),
            NodeDef("broker_snapshot", "Broker snapshot", "broker", "Positions and intents"),
            NodeDef("continue_or_block", "Continue or block", "bot", "Backend verdict", "receipt"),
        ),
    ),
    "activate": GraphDef(
        graph_id="activate",
        title="Activation internals",
        primary_node_id="desired_state",
        nodes=(
            NodeDef("desired_state", "Desired state", "bot"),
            NodeDef("resume_gate", "Resume capability", "bot", "Shared action gate"),
            NodeDef("command_loop", "Command loop", "bot", "Child runtime freshness"),
        ),
    ),
    "submit_order": GraphDef(
        graph_id="submit_order",
        title="Order-submission internals",
        primary_node_id="signal",
        nodes=(
            NodeDef("signal", "Strategy signal", "bot", "Future engine state"),
            NodeDef("intent_wal", "Intent WAL", "broker", "Durable order intent"),
            NodeDef("place_order", "Broker submit", "broker", "Broker submit boundary"),
            NodeDef("ack_or_reconcile", "Ack or reconcile", "recovery", "Uncertain outcome handling"),
        ),
    ),
    "broker_writer": GraphDef(
        graph_id="broker_writer",
        title="Broker activity internals",
        primary_node_id="publisher",
        nodes=(
            NodeDef("publisher", "Activity publisher", "broker"),
            NodeDef("writer_guard", "Owner generation", "broker", "R2 generation evidence"),
            NodeDef("broker_ack", "Broker ack", "broker", "Execution evidence"),
        ),
    ),
    "recovery": GraphDef(
        graph_id="recovery",
        title="Recovery internals",
        primary_node_id="incident",
        nodes=(
            NodeDef("incident", "Incident", "recovery", "Watchdog or poison"),
            NodeDef("flatten", "Flatten safely", "recovery", "Proof before disconnect"),
            NodeDef("reconcile_after", "Reconcile after incident", "broker", "Broker proof"),
            NodeDef("fresh_run", "Fresh run", "bot", "Redeploy when retired"),
        ),
    ),
}


def compose_bot_lifecycle_chart(
    strategy_instance_id: str,
    surface: OperatorSurface,
    *,
    desired_state: DesiredStateView | None = None,
    redeploy_available: bool = False,
    lifecycle_events: Sequence[BotLifecycleEvent] = (),
) -> BotLifecycleChartView:
    """Compose the Overview-tab lifecycle graph from backend-authored facts."""

    facts = _lifecycle_facts(surface, desired_state, lifecycle_events)
    subgraphs = {graph_id: _build_graph(graph_def, facts) for graph_id, graph_def in SUBGRAPH_DEFS.items()}
    global_graph = _build_graph(GLOBAL_GRAPH, facts, expandable_graph_ids=subgraphs.keys())
    return BotLifecycleChartView(
        chart_id="bot_lifecycle_v1",
        selected_bot_id=strategy_instance_id,
        title=GLOBAL_GRAPH.title,
        global_graph=global_graph,
        subgraphs=subgraphs,
        actions=_actions(surface, redeploy_available=redeploy_available),
    )


def _lifecycle_facts(
    surface: OperatorSurface,
    desired_state: DesiredStateView | None,
    lifecycle_events: Sequence[BotLifecycleEvent],
) -> LifecycleFacts:
    base_statuses = {
        "deploy": _deploy_status(surface),
        "preflight": _preflight_status(surface),
        "account_safety": _account_status(surface),
        "reconcile": _reconcile_status(surface),
        "activate": _activate_status(surface, desired_state),
        "active": "active",
        "broker_writer": _broker_writer_status(surface),
        "recovery": _recovery_status(surface),
    }
    primary_node_id = _primary_node_id(base_statuses, surface, desired_state)
    active_status = _active_status(primary_node_id)
    submit_order_event = latest_event_for_node(lifecycle_events, "submit_order")
    submit_order_status: LifecycleChartStatus = "passed" if primary_node_id == "broker_writer" else "inactive"
    facts = {
        "deploy": NodeFact(_status_for("deploy", base_statuses["deploy"], primary_node_id), _host_evidence(surface)),
        "preflight": NodeFact(
            _status_for("preflight", base_statuses["preflight"], primary_node_id),
            _preflight_evidence(surface),
        ),
        "account_safety": NodeFact(
            _status_for("account_safety", base_statuses["account_safety"], primary_node_id),
            _account_evidence(surface),
        ),
        "reconcile": NodeFact(
            _status_for("reconcile", base_statuses["reconcile"], primary_node_id),
            _reconcile_evidence(surface),
            ts_ms=_reconcile_ts_ms(surface),
            ts_ms_resolved=_reconcile_ts_resolved(surface),
            receipts=_reconciliation_receipts(surface),
        ),
        "activate": NodeFact(
            _status_for("activate", base_statuses["activate"], primary_node_id),
            _activate_evidence(desired_state),
        ),
        "active": NodeFact(
            active_status,
            _active_evidence(active_status),
            why=_active_reason(surface, desired_state, active_status),
        ),
        "submit_order": _node_fact_from_event(
            submit_order_event,
            fallback_status=submit_order_status,
            fallback_evidence="Order submission waits for an active signal from the running bot.",
        ),
        "broker_writer": NodeFact(
            _status_for("broker_writer", base_statuses["broker_writer"], primary_node_id),
            _broker_writer_evidence(surface),
        ),
        "recovery": NodeFact(
            _status_for("recovery", base_statuses["recovery"], primary_node_id),
            _recovery_evidence(surface),
        ),
        "deploy.host_state": NodeFact(_deploy_status(surface), _host_evidence(surface), surface.host_process.state),
        "start_settings": NodeFact(
            _gate_status(surface.host_process.start_capability.gate_results),
            _start_capability_reason(surface.host_process.start_capability),
        ),
        "start_or_redeploy": NodeFact(
            "active" if surface.host_process.start_capability.enabled else "blocked",
            _start_capability_reason(surface.host_process.start_capability),
        ),
        "configuration": NodeFact(_configuration_status(surface), _configuration_evidence(surface)),
        "account_freeze": NodeFact(
            _freeze_status(surface),
            _freeze_evidence(surface) or "No account freeze gate is active.",
        ),
        "broker_safety": NodeFact(
            _broker_safety_status(surface),
            f"Broker safety verdict is {surface.broker.safety_verdict}.",
            surface.broker.safety_verdict,
        ),
        "broker_connection": NodeFact(
            _broker_connection_status(surface),
            f"Broker connection is {surface.broker.connection}.",
            surface.broker.connection,
        ),
        "risk_posture": NodeFact(
            "passed" if surface.current_risk.verdict == "READY" else "unknown",
            f"Posture is {surface.current_risk.posture}; pending orders: {surface.current_risk.pending_order_count}.",
            surface.current_risk.posture,
        ),
        "receipt": NodeFact(
            _reconcile_status(surface),
            _reconcile_evidence(surface),
            _reconcile_label(surface),
            ts_ms=_reconcile_ts_ms(surface),
            ts_ms_resolved=_reconcile_ts_resolved(surface),
            receipts=_reconciliation_receipts(surface),
        ),
        "broker_snapshot": NodeFact(
            "passed" if _reconcile_status(surface) in {"passed", "active"} else "blocked",
            "Broker evidence is compared against engine intent before resume.",
        ),
        "desired_state": NodeFact(
            _activate_status(surface, desired_state),
            _activate_evidence(desired_state),
            _desired_label(desired_state),
        ),
        "resume_gate": NodeFact(
            _capability_status(surface.actions.resume),
            _capability_reason(surface.actions.resume),
        ),
        "command_loop": NodeFact(
            _command_loop_status(surface),
            _command_loop_evidence(surface),
        ),
        "signal": _event_or_unknown_fact(
            lifecycle_events,
            "signal",
            "No signal evidence is available in this lifecycle projection.",
        ),
        "intent_wal": _event_or_unknown_fact(
            lifecycle_events,
            "intent_wal",
            "No Intent WAL row is available for this node in the selected evidence window.",
        ),
        "place_order": _event_or_unknown_fact(
            lifecycle_events,
            "place_order",
            "No broker submit-boundary event is available in the selected evidence window.",
        ),
        "ack_or_reconcile": _event_or_unknown_fact(
            lifecycle_events,
            "ack_or_reconcile",
            "No broker acknowledgement or reconciliation event is available in this snapshot.",
        ),
        "publisher": NodeFact(
            _broker_writer_status(surface), _broker_writer_evidence(surface), _broker_activity_label(surface)
        ),
        "writer_guard": _writer_guard_fact(surface, lifecycle_events),
        "broker_ack": _event_or_unknown_fact(
            lifecycle_events,
            "broker_ack",
            "No live broker acknowledgement evidence is available in this snapshot.",
        ),
        "incident": NodeFact(_recovery_status(surface), _recovery_evidence(surface)),
        "flatten": NodeFact("inactive", "Flatten is shown only when the backend enables the capability."),
        "reconcile_after": NodeFact("inactive", "Recovery requires broker evidence before a fresh run resumes."),
        "fresh_run": NodeFact("inactive", "Poisoned or stopped runs require a fresh run_id."),
    }
    facts.update(_readiness_gate_facts(surface.readiness_gates))
    return LifecycleFacts(primary_node_id, base_statuses, facts)


def _event_or_unknown_fact(
    lifecycle_events: Sequence[BotLifecycleEvent],
    node_id: str,
    absent_reason: str,
) -> NodeFact:
    return _node_fact_from_event(
        latest_event_for_node(lifecycle_events, node_id),
        fallback_status="unknown",
        fallback_evidence=absent_reason,
        fallback_why=absent_reason,
        include_event_source_label=True,
    )


def _writer_guard_fact(surface: OperatorSurface, lifecycle_events: Sequence[BotLifecycleEvent]) -> NodeFact:
    event = latest_event_for_node(lifecycle_events, "writer_guard")
    if event is not None:
        return _node_fact_from_event(
            event,
            fallback_status="unknown",
            fallback_evidence=_account_owner_evidence(surface),
            fallback_why=_account_owner_evidence(surface),
            include_event_source_label=True,
        )
    account_owner_ts_ms = _account_owner_ts_ms(surface)
    return NodeFact(
        _account_owner_status(surface),
        _account_owner_evidence(surface),
        _account_owner_label(surface),
        status_label=lifecycle_status_label(_account_owner_status(surface)),
        why=_account_owner_evidence(surface),
        ts_ms=account_owner_ts_ms,
        ts_ms_resolved=account_owner_ts_ms is not None,
        receipts=_account_owner_receipts(surface),
    )


def _node_fact_from_event(
    event: BotLifecycleEvent | None,
    *,
    fallback_status: LifecycleChartStatus,
    fallback_evidence: str,
    fallback_why: str | None = None,
    include_event_source_label: bool = False,
) -> NodeFact:
    if event is None:
        return NodeFact(
            fallback_status,
            fallback_evidence,
            status_label=lifecycle_status_label(fallback_status),
            why=fallback_why,
        )
    status = event.status or "unknown"
    return NodeFact(
        status,
        event.summary,
        event.source if include_event_source_label else None,
        status_label=event.status_label or lifecycle_status_label(status),
        why=event.why,
        operator_next_step=event.operator_next_step,
        ts_ms=event.ts_ms,
        ts_ms_resolved=event.ts_ms_resolved,
        receipts=_event_receipts(event),
    )


def _receipt(
    label: str,
    value: object,
    *,
    unit: str | None = None,
    source: str | None = None,
    gate_id: str | None = None,
    ts_ms: int | None = None,
    ts_ms_resolved: bool | None = None,
) -> LifecycleChartReceipt:
    resolved = ts_ms is not None if ts_ms_resolved is None else ts_ms_resolved
    return LifecycleChartReceipt(
        label=label,
        value=str(value),
        unit=unit,
        source=source,
        gate_id=gate_id,
        ts_ms=ts_ms,
        ts_ms_resolved=resolved,
    )


def _event_receipts(event: BotLifecycleEvent) -> tuple[LifecycleChartReceipt, ...]:
    receipts = [
        _receipt(
            "event_type",
            event.event_type,
            source=event.source,
            ts_ms=event.ts_ms,
            ts_ms_resolved=event.ts_ms_resolved,
        ),
        _receipt(
            "source_seq",
            event.source_local_seq,
            source=event.source,
            ts_ms=event.ts_ms,
            ts_ms_resolved=event.ts_ms_resolved,
        ),
    ]
    for key in ("intent_id", "order_ref", "order_id", "perm_id", "ts_ms_source"):
        value = event.payload.get(key)
        if value is not None:
            receipts.append(
                _receipt(
                    key,
                    value,
                    source=event.source,
                    ts_ms=event.ts_ms,
                    ts_ms_resolved=event.ts_ms_resolved,
                )
            )
    return tuple(receipts)


def _build_graph(
    graph_def: GraphDef,
    facts: LifecycleFacts,
    *,
    expandable_graph_ids: Iterable[str] = (),
) -> LifecycleChartGraph:
    node_defs = _node_defs_for(graph_def, facts)
    nodes = [_build_node(node_def, facts) for node_def in node_defs]
    expandable = set(expandable_graph_ids)
    for node in nodes:
        if node.id in expandable:
            node.expandable = True
            node.subgraph_id = node.id
    primary_node_id = _graph_primary_node_id(graph_def, facts, nodes)
    edges = _build_edges(graph_def, facts, nodes)
    return LifecycleChartGraph(
        graph_id=graph_def.graph_id,
        title=graph_def.title,
        primary_node_id=primary_node_id,
        nodes=nodes,
        edges=edges,
    )


def _node_defs_for(graph_def: GraphDef, facts: LifecycleFacts) -> tuple[NodeDef, ...]:
    if graph_def.graph_id != "preflight":
        return graph_def.nodes
    readiness_ids = sorted(
        (fact_id for fact_id in facts.node_facts if fact_id.startswith("readiness_")),
        key=_readiness_sort_key,
    )
    return graph_def.nodes + tuple(
        NodeDef(fact_id, f"Readiness gate {index}", "bot") for index, fact_id in enumerate(readiness_ids, start=1)
    )


def _build_node(node_def: NodeDef, facts: LifecycleFacts) -> LifecycleChartNode:
    fact = facts.node_facts[_fact_id(node_def)]
    return LifecycleChartNode(
        id=node_def.id,
        label=_node_label(node_def, fact),
        technical_label=fact.technical_label if fact.technical_label is not None else node_def.technical_label,
        lane=node_def.lane,
        status=fact.status,
        status_label=fact.status_label or lifecycle_status_label(fact.status),
        summary=fact.evidence,
        why=fact.why,
        operator_next_step=fact.operator_next_step,
        evidence_summary=fact.evidence,
        ts_ms=fact.ts_ms,
        ts_ms_resolved=fact.ts_ms_resolved,
        receipts=list(fact.receipts),
    )


def _build_edges(
    graph_def: GraphDef,
    facts: LifecycleFacts,
    nodes: list[LifecycleChartNode],
) -> list[LifecycleChartEdge]:
    if graph_def.edges:
        return [_edge_from_def(edge_def, facts) for edge_def in graph_def.edges]
    return [
        _edge(source.id, target.id, _linear_edge_status(source.status, target.status))
        for source, target in pairwise(nodes)
    ]


def _edge_from_def(edge_def: EdgeDef, facts: LifecycleFacts) -> LifecycleChartEdge:
    if edge_def.kind == "main":
        status = _main_edge_status(
            edge_def.source,
            edge_def.target,
            facts.primary_node_id,
            facts.base_statuses[edge_def.target],
        )
    elif edge_def.source == "active" and edge_def.target == "submit_order":
        status = "passed" if facts.primary_node_id == "broker_writer" else "inactive"
    elif edge_def.source == "submit_order" and edge_def.target == "broker_writer":
        status = facts.base_statuses["broker_writer"] if facts.primary_node_id == "broker_writer" else "inactive"
    elif edge_def.target == "recovery":
        status = facts.base_statuses["recovery"] if facts.primary_node_id == "recovery" else "inactive"
    else:
        status = "inactive"
    return _edge(edge_def.source, edge_def.target, status, edge_def.label)


def _edge(
    source: str,
    target: str,
    status: LifecycleChartStatus,
    label: str | None = None,
) -> LifecycleChartEdge:
    return LifecycleChartEdge(
        id=f"{source}_to_{target}",
        source=source,
        target=target,
        status=status,
        label=label,
        animated=status not in {"inactive", "unknown"},
    )


def _graph_primary_node_id(
    graph_def: GraphDef,
    facts: LifecycleFacts,
    nodes: list[LifecycleChartNode],
) -> str:
    if graph_def.primary_node_id is None:
        return facts.primary_node_id
    for status in _BLOCKING_PRIORITY:
        for node in nodes:
            if node.status == status:
                return node.id
    for node in nodes:
        if node.status == "active":
            return node.id
    if any(node.id == graph_def.primary_node_id for node in nodes):
        return graph_def.primary_node_id
    return nodes[0].id if nodes else graph_def.primary_node_id


def _linear_edge_status(
    source_status: LifecycleChartStatus,
    target_status: LifecycleChartStatus,
) -> LifecycleChartStatus:
    if source_status in _BLOCKING_PRIORITY:
        return source_status
    return target_status if target_status != "inactive" else "inactive"


def _fact_id(node_def: NodeDef) -> str:
    return node_def.fact_id or node_def.id


def _node_label(node_def: NodeDef, fact: NodeFact) -> str:
    if node_def.id.startswith("readiness_") and fact.technical_label:
        return fact.technical_label
    return node_def.label


def _readiness_gate_facts(readiness_gates: list[OperatorGate]) -> dict[str, NodeFact]:
    if not readiness_gates:
        return {
            "readiness_1": NodeFact(
                "unknown",
                "No readiness vector is available for this bot status snapshot.",
                "No readiness rows",
            )
        }
    return {
        f"readiness_{index}": NodeFact(
            _operator_gate_status(gate),
            gate.gate_result.operator_reason or gate.detail,
            gate.name,
        )
        for index, gate in enumerate(readiness_gates, start=1)
    }


def _readiness_sort_key(fact_id: str) -> int:
    try:
        return int(fact_id.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def _primary_node_id(
    statuses: Mapping[str, LifecycleChartStatus],
    surface: OperatorSurface,
    desired_state: DesiredStateView | None,
) -> str:
    if statuses["recovery"] in {"active", "blocked", "poison", "freeze"}:
        return "recovery"
    for node_id in ("deploy", "preflight", "account_safety", "reconcile", "activate"):
        if statuses[node_id] != "passed":
            return node_id
    if statuses["broker_writer"] in {"active", "blocked"}:
        return "broker_writer"
    if surface.host_process.state == "RUNNING" and _effective_desired_value(desired_state) == "RUNNING":
        return "active"
    return "activate"


def _status_for(
    node_id: str,
    status: LifecycleChartStatus,
    primary_node_id: str,
) -> LifecycleChartStatus:
    if node_id == primary_node_id and status == "passed":
        return "active"
    return status


def _active_status(primary_node_id: str) -> LifecycleChartStatus:
    if primary_node_id == "active":
        return "active"
    if primary_node_id in {"broker_writer", "recovery"}:
        return "passed"
    return "inactive"


def _main_edge_status(
    source: str,
    target: str,
    primary_node_id: str,
    target_status: LifecycleChartStatus,
) -> LifecycleChartStatus:
    if primary_node_id not in _MAIN_PATH:
        return "passed"
    primary_index = _MAIN_PATH.index(primary_node_id)
    source_index = _MAIN_PATH.index(source)
    target_index = _MAIN_PATH.index(target)
    if target_index < primary_index:
        return "passed"
    if target == primary_node_id:
        return "active" if target_status == "passed" else target_status
    if source_index < primary_index:
        return "passed"
    return "inactive"


def _deploy_status(surface: OperatorSurface) -> LifecycleChartStatus:
    if surface.host_process.state == "RUNNING":
        return "passed"
    start_status = _gate_status(surface.host_process.start_capability.gate_results)
    if surface.host_process.start_capability.enabled:
        return "active"
    return start_status if start_status != "passed" else "blocked"


def _preflight_status(surface: OperatorSurface) -> LifecycleChartStatus:
    statuses = [_configuration_status(surface)]
    if not surface.readiness_gates:
        statuses.append("unknown")
    statuses.extend(_operator_gate_status(gate) for gate in surface.readiness_gates)
    return _worst_status(statuses, default="unknown")


def _account_status(surface: OperatorSurface) -> LifecycleChartStatus:
    statuses = [_freeze_status(surface), _broker_safety_status(surface), _broker_connection_status(surface)]
    return _worst_status(statuses, default="unknown")


def _reconcile_status(surface: OperatorSurface) -> LifecycleChartStatus:
    reconciliation = surface.reconciliation
    if reconciliation is None:
        return "unknown" if surface.host_process.state == "RUNNING" else "inactive"
    if reconciliation.state in {"CLEAN", "ADOPTED"}:
        return "passed"
    if reconciliation.state == "IN_PROGRESS":
        return "active"
    return "blocked"


def _activate_status(
    surface: OperatorSurface,
    desired_state: DesiredStateView | None,
) -> LifecycleChartStatus:
    desired = _effective_desired_value(desired_state)
    if desired == "RUNNING" and surface.host_process.state == "RUNNING":
        return "passed"
    if desired == "PAUSED" and surface.host_process.state == "RUNNING":
        return "active"
    if desired == "STOPPED":
        return "blocked"
    return "unknown"


def _broker_writer_status(surface: OperatorSurface) -> LifecycleChartStatus:
    health = surface.broker_activity_health
    if health is None:
        return "inactive"
    if health.state == "ready":
        return "passed"
    if health.state == "starting":
        return "active"
    return "blocked"


def _account_owner_status(surface: OperatorSurface) -> LifecycleChartStatus:
    owner = surface.account_owner
    if owner is None or owner.phase == "unknown" or owner.generation is None:
        return "unknown"
    if owner.phase == "frozen":
        return "freeze"
    if owner.phase == "accepting":
        return "passed"
    return "active"


def _recovery_status(surface: OperatorSurface) -> LifecycleChartStatus:
    if _freeze_status(surface) == "freeze":
        return "inactive"
    if surface.prior_run.classification == "HALT_TRIGGERED":
        return "poison"
    if surface.incident_headline is not None:
        return "blocked"
    if surface.host_process.state == "STOPPING":
        return "active"
    return "inactive"


def _configuration_status(surface: OperatorSurface) -> LifecycleChartStatus:
    if surface.configuration.verdict == "READY":
        return "passed"
    if surface.configuration.verdict == "ATTENTION":
        return "blocked"
    return "unknown"


def _operator_gate_status(gate: OperatorGate) -> LifecycleChartStatus:
    return _gate_to_chart_status(gate.gate_result.status)


def _gate_status(gate_results: Iterable[GateResult]) -> LifecycleChartStatus:
    statuses = [_gate_to_chart_status(gate.status) for gate in gate_results]
    return _worst_status(statuses, default="passed")


def _freeze_status(surface: OperatorSurface) -> LifecycleChartStatus:
    return "freeze" if any(gate.status == "freeze" for gate in _all_gate_results(surface)) else "passed"


def _broker_safety_status(surface: OperatorSurface) -> LifecycleChartStatus:
    if surface.broker.safety_verdict == "PAPER_ONLY":
        return "passed"
    if surface.broker.safety_verdict == "UNSAFE":
        return "blocked"
    return "unknown"


def _broker_connection_status(surface: OperatorSurface) -> LifecycleChartStatus:
    if surface.broker.connection == "CONNECTED":
        return "passed"
    if surface.broker.connection == "DISCONNECTED":
        return "blocked"
    return "unknown"


def _capability_status(capability: ActionCapability) -> LifecycleChartStatus:
    if capability.enabled:
        return "passed"
    status = _gate_status(capability.gate_results)
    return status if status != "passed" else "blocked"


def _command_loop_status(surface: OperatorSurface) -> LifecycleChartStatus:
    freshness = surface.runtime_freshness
    if freshness is None:
        return "unknown"
    command_loop = freshness.command_loop
    if command_loop.state in {"FRESH", "NOT_APPLICABLE"}:
        return "passed"
    if command_loop.state == "UNKNOWN":
        return "unknown"
    return "blocked"


def _worst_status(
    statuses: Iterable[LifecycleChartStatus],
    *,
    default: LifecycleChartStatus,
) -> LifecycleChartStatus:
    collected = list(statuses)
    if not collected:
        return default
    for status in _BLOCKING_PRIORITY:
        if status in collected:
            return status
    if "active" in collected:
        return "active"
    if "passed" in collected:
        return "passed"
    return default


def _gate_to_chart_status(status: str) -> LifecycleChartStatus:
    if status == "pass":
        return "passed"
    if status == "freeze":
        return "freeze"
    if status == "poison":
        return "poison"
    if status == "block":
        return "blocked"
    if status == "not_applicable":
        return "inactive"
    return "unknown"


def _desired_value(desired_state: DesiredStateView | None) -> str | None:
    if desired_state is None or desired_state.path_status != "ok":
        return None
    return desired_state.state


def _effective_desired_value(desired_state: DesiredStateView | None) -> str | None:
    desired = _desired_value(desired_state)
    if desired is not None:
        return desired
    if desired_state is not None and desired_state.path_status == "absent":
        return "RUNNING"
    return None


def _desired_label(desired_state: DesiredStateView | None) -> str:
    desired = _desired_value(desired_state)
    if desired is not None:
        return desired
    if desired_state is not None and desired_state.path_status == "absent":
        return "RUNNING default"
    return "UNKNOWN"


def _all_gate_results(surface: OperatorSurface) -> list[GateResult]:
    gates: list[GateResult] = []
    gates.extend(surface.host_process.start_capability.gate_results)
    for capability in (
        surface.actions.resume,
        surface.actions.pause,
        surface.actions.stop,
        surface.actions.flatten_and_pause,
        surface.actions.mark_poisoned,
    ):
        gates.extend(capability.gate_results)
    gates.extend(gate.gate_result for gate in surface.readiness_gates)
    return gates


def _actions(surface: OperatorSurface, *, redeploy_available: bool) -> list[LifecycleChartAction]:
    start = surface.host_process.start_capability
    return [
        LifecycleChartAction(
            id="start_process",
            label="Start bot process",
            enabled=start.enabled,
            reason=_start_capability_reason(start),
            target_node_id="deploy",
            tone="primary",
        ),
        _action("resume", "Resume trading", surface.actions.resume, "activate", "primary"),
        _action("pause", "Pause trading", surface.actions.pause, "active", "secondary"),
        _action("flatten_and_pause", "Flatten and pause", surface.actions.flatten_and_pause, "recovery", "danger"),
        _action("stop", "Stop bot", surface.actions.stop, "recovery", "danger"),
        _action("mark_poisoned", "Mark poisoned", surface.actions.mark_poisoned, "recovery", "danger"),
        LifecycleChartAction(
            id="redeploy",
            label="Redeploy fresh run",
            enabled=redeploy_available,
            reason=(
                "Redeploy proof is available."
                if redeploy_available
                else "Redeploy requires stored deployment proof for this bot."
            ),
            target_node_id="deploy",
            tone="secondary",
        ),
    ]


def _action(
    action_id: LifecycleChartActionId,
    label: str,
    capability: ActionCapability,
    target_node_id: str,
    tone: Literal["primary", "secondary", "danger"],
) -> LifecycleChartAction:
    return LifecycleChartAction(
        id=action_id,
        label=label,
        enabled=capability.enabled,
        reason=_capability_reason(capability),
        target_node_id=target_node_id,
        tone=tone,
    )


def _capability_reason(capability: ActionCapability) -> str | None:
    if capability.enabled:
        return "Backend gate currently allows this action."
    if capability.disabled_reason_code:
        return capability.disabled_reason_code
    if capability.disabled_reasons:
        return capability.disabled_reasons[0]
    if capability.gate_results:
        return capability.gate_results[0].operator_reason
    return "Backend gate currently blocks this action."


def _start_capability_reason(capability: HostProcessStartCapability) -> str | None:
    if capability.enabled:
        return "Backend-authored start request is ready."
    if capability.disabled_reason_code:
        return capability.disabled_reason_code
    if capability.gate_results:
        return capability.gate_results[0].operator_reason
    return "Backend gate currently blocks start."


def _host_evidence(surface: OperatorSurface) -> str:
    if surface.host_process.state == "RUNNING":
        return "Host daemon reports this bot process is running."
    return (
        surface.host_process.notice
        or _start_capability_reason(surface.host_process.start_capability)
        or "Host state is unavailable."
    )


def _active_evidence(status: LifecycleChartStatus) -> str:
    if status == "active":
        return "Bot process is running and the lifecycle gates before trading have passed."
    if status == "passed":
        return "The live-monitoring stage was reached before the current recovery path."
    return "This stage waits until earlier lifecycle gates pass."


def _active_reason(
    surface: OperatorSurface,
    desired_state: DesiredStateView | None,
    status: LifecycleChartStatus,
) -> str:
    desired = _effective_desired_value(desired_state)
    if status == "active":
        return "Host process is RUNNING and effective desired state is RUNNING."
    if surface.host_process.state != "RUNNING":
        return f"Host process is {surface.host_process.state}; active requires RUNNING."
    if desired != "RUNNING":
        return f"Effective desired state is {_desired_label(desired_state)}; active requires RUNNING."
    return "Earlier lifecycle gates have not all passed yet."


def _preflight_evidence(surface: OperatorSurface) -> str:
    if surface.configuration.verdict != "READY":
        reasons = ", ".join(surface.configuration.reason_codes) or surface.configuration.verdict
        return f"Configuration verdict is {surface.configuration.verdict}: {reasons}."
    blocked_gate = next(
        (gate for gate in surface.readiness_gates if _operator_gate_status(gate) != "passed"),
        None,
    )
    if blocked_gate is not None:
        return blocked_gate.gate_result.operator_reason or blocked_gate.detail
    return "Configuration and readiness gates are passing."


def _account_evidence(surface: OperatorSurface) -> str:
    freeze = _freeze_evidence(surface)
    if freeze:
        return freeze
    if surface.broker.safety_verdict != "PAPER_ONLY":
        return f"Broker safety verdict is {surface.broker.safety_verdict}."
    if surface.broker.connection != "CONNECTED":
        return f"Broker connection is {surface.broker.connection}."
    return "Account freeze, broker safety, and broker connection are clear."


def _reconcile_evidence(surface: OperatorSurface) -> str:
    reconciliation = surface.reconciliation
    if reconciliation is None:
        return "No reconciliation projection is available for this status snapshot."
    if reconciliation.failure_reason:
        return reconciliation.failure_reason
    if reconciliation.state == "ADOPTED":
        count = len(reconciliation.adopted_intent_ids)
        return f"Reconciliation adopted {count} broker intent(s)."
    return f"Reconciliation state is {reconciliation.state}."


def _reconcile_ts_ms(surface: OperatorSurface) -> int | None:
    reconciliation = surface.reconciliation
    return reconciliation.last_reconcile_ms if reconciliation is not None else None


def _reconcile_ts_resolved(surface: OperatorSurface) -> bool:
    return _reconcile_ts_ms(surface) is not None


def _reconciliation_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    reconciliation = surface.reconciliation
    if reconciliation is None:
        return ()
    ts_ms = reconciliation.last_reconcile_ms
    receipts = [
        _receipt("reconciliation.state", reconciliation.state, source="reconciliation_projection", ts_ms=ts_ms),
        _receipt(
            "adopted_intent_count",
            len(reconciliation.adopted_intent_ids),
            source="reconciliation_projection",
            ts_ms=ts_ms,
        ),
    ]
    if reconciliation.last_reconcile_ms is not None:
        receipts.append(
            _receipt(
                "last_reconcile_ms",
                reconciliation.last_reconcile_ms,
                unit="ms UTC",
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    if reconciliation.sidecar_wal_seq is not None:
        receipts.append(
            _receipt(
                "sidecar_wal_seq",
                reconciliation.sidecar_wal_seq,
                unit="seq",
                source="reconciliation_projection",
                ts_ms=ts_ms,
            )
        )
    if reconciliation.broker_observed_at_ms is not None:
        receipts.append(
            _receipt(
                "broker_observed_at_ms",
                reconciliation.broker_observed_at_ms,
                unit="ms UTC",
                source="reconciliation_projection",
                ts_ms=reconciliation.broker_observed_at_ms,
            )
        )
    if reconciliation.failure_reason:
        receipts.append(
            _receipt("failure_reason", reconciliation.failure_reason, source="reconciliation_projection", ts_ms=ts_ms)
        )
    return tuple(receipts)


def _activate_evidence(desired_state: DesiredStateView | None) -> str:
    desired = _desired_value(desired_state)
    if desired is None:
        if desired_state is not None and desired_state.path_status == "absent":
            return "Desired-state sidecar is absent; effective state is RUNNING."
        return "Desired-state sidecar is unavailable or not healthy."
    return f"Durable desired state is {desired}."


def _broker_writer_evidence(surface: OperatorSurface) -> str:
    health = surface.broker_activity_health
    if health is None:
        return (
            "No broker-activity publisher is registered for this snapshot; this says nothing about "
            "R3 AccountOwner daemon/IPC writer authority."
        )
    if health.headline is not None:
        return health.headline.message
    return (
        f"Broker-activity publisher state is {health.state}; this is capture health, not proof that "
        "R3 AccountOwner daemon/IPC single-writer authority is shipped."
    )


def _account_owner_label(surface: OperatorSurface) -> str:
    owner = surface.account_owner
    if owner is None:
        return "generation unproven"
    if owner.generation is None:
        return f"{owner.phase} generation unknown"
    return f"{owner.phase} gen {owner.generation}"


def _account_owner_evidence(surface: OperatorSurface) -> str:
    owner = surface.account_owner
    if owner is None:
        return (
            "No AccountOwner generation evidence is available; R2 still uses process-local broker "
            "sessions and the R3 daemon/IPC single-writer authority is not shipped."
        )
    if owner.generation is None:
        return (
            f"AccountOwner phase is {owner.phase}, but generation is unproven; R2 still uses "
            "process-local broker sessions."
        )
    return (
        f"AccountOwner phase is {owner.phase}; generation is {owner.generation}. This is generation "
        "evidence, not proof that R3 daemon/IPC single-writer authority is shipped."
    )


def _account_owner_ts_ms(surface: OperatorSurface) -> int | None:
    owner = surface.account_owner
    return owner.recorded_at_ms if owner is not None else None


def _account_owner_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    owner = surface.account_owner
    if owner is None:
        return ()
    ts_ms = owner.recorded_at_ms
    return (
        _receipt("account_owner.phase", owner.phase, source=owner.source, ts_ms=ts_ms),
        _receipt(
            "account_owner.generation",
            owner.generation if owner.generation is not None else "unknown",
            source=owner.source,
            ts_ms=ts_ms,
        ),
    )


def _recovery_evidence(surface: OperatorSurface) -> str:
    if surface.prior_run.classification == "HALT_TRIGGERED":
        return "Previous run halted for safety and requires recovery review."
    if surface.incident_headline is not None:
        return surface.incident_headline.message
    if surface.host_process.state == "STOPPING":
        return "The bot process is currently stopping."
    return "No active recovery incident is present."


def _configuration_evidence(surface: OperatorSurface) -> str:
    if surface.configuration.reason_codes:
        return ", ".join(surface.configuration.reason_codes)
    return f"Configuration verdict is {surface.configuration.verdict}."


def _freeze_evidence(surface: OperatorSurface) -> str | None:
    gate = next((gate for gate in _all_gate_results(surface) if gate.status == "freeze"), None)
    if gate is None:
        return None
    return gate.operator_reason


def _reconcile_label(surface: OperatorSurface) -> str:
    reconciliation = surface.reconciliation
    return reconciliation.state if reconciliation is not None else "NOT_AVAILABLE"


def _broker_activity_label(surface: OperatorSurface) -> str:
    health = surface.broker_activity_health
    return health.state if health is not None else "not registered"


def _command_loop_evidence(surface: OperatorSurface) -> str:
    freshness = surface.runtime_freshness
    if freshness is None:
        return "Runtime freshness is unavailable for this status snapshot."
    command_loop = freshness.command_loop
    if command_loop.stale_reason_codes:
        return ", ".join(command_loop.stale_reason_codes)
    return f"Command loop freshness is {command_loop.state}."
