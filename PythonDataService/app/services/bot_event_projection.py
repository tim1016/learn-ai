"""Deterministic raw-to-authored Bot event projection (ADR 0024)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    BotEventRow,
    BotEventSeverity,
    BotEventType,
    FactValue,
    GateStep,
    GateStepResult,
    SourceAuthority,
    TerminalError,
)

_EVENT_PRIORITY: dict[BotEventRawType, int] = {
    BotEventRawType.SIGNAL_FIRED: 10,
    BotEventRawType.ORDER_SUBMITTED: 20,
    BotEventRawType.ORDER_FILLED: 30,
    BotEventRawType.ORDER_CANCELLED: 30,
    BotEventRawType.BLOCKED: 40,
    BotEventRawType.HALTED: 90,
    BotEventRawType.LAUNCH_FAILED: 90,
    BotEventRawType.ORDER_REJECTED: 100,
}


@dataclass
class _Cluster:
    first_seq: int
    first_ts_ms: int
    identity: BotEventIdentity
    source_authority: SourceAuthority
    gate_steps: list[GateStep] = field(default_factory=list)
    events: list[BotEventRaw] = field(default_factory=list)
    facts: dict[str, FactValue] = field(default_factory=dict)


def project_bot_event_rows(raw_events: Iterable[BotEventRaw]) -> list[BotEventRow]:
    """Project raw enforcement-point events into authored stream rows.

    The projector is intentionally side-effect free: publishers and REST/SSE
    backfill can replay the same WAL and get identical rows.
    """

    clusters: dict[tuple[str, object], _Cluster] = {}
    idle_events: list[BotEventRaw] = []

    for raw in sorted(raw_events, key=lambda event: (event.ts_ms, event.seq)):
        if raw.event_type is BotEventRawType.EVALUATION_IDLE:
            idle_events.append(raw)
            continue
        _add_to_cluster(clusters, raw)

    rows = [row for cluster in clusters.values() if (row := _cluster_row(cluster)) is not None]
    return _merge_idle_rows(idle_events, rows)


def _add_to_cluster(clusters: dict[tuple[str, object], _Cluster], raw: BotEventRaw) -> None:
    key = _cluster_key(raw)
    cluster = clusters.get(key)
    if cluster is None:
        cluster = _Cluster(
            first_seq=raw.seq,
            first_ts_ms=raw.ts_ms,
            identity=raw.identity,
            source_authority=raw.source_authority,
        )
        clusters[key] = cluster
    cluster.identity = _merge_identity(cluster.identity, raw.identity)
    if raw.gate_step is not None:
        cluster.gate_steps.append(raw.gate_step)
        cluster.identity = _merge_identity(
            cluster.identity,
            BotEventIdentity(evaluation_id=raw.gate_step.evaluation_id),
        )
    else:
        cluster.events.append(raw)
    cluster.facts.update(raw.facts)


def _cluster_key(raw: BotEventRaw) -> tuple[str, object]:
    if raw.gate_step is not None:
        return ("evaluation", raw.gate_step.evaluation_id)
    identity = raw.identity
    if identity.evaluation_id:
        return ("evaluation", identity.evaluation_id)
    if identity.order_ref:
        return ("order_ref", identity.order_ref)
    if identity.intent_id:
        return ("intent_id", identity.intent_id)
    if identity.req_id is not None:
        return ("req_id", identity.req_id)
    if identity.order_id is not None:
        return ("order_id", identity.order_id)
    if identity.perm_id is not None:
        return ("perm_id", identity.perm_id)
    if identity.exec_id:
        return ("exec_id", identity.exec_id)
    raise ValueError("raw bot event identity has no cluster key")


def _cluster_row(cluster: _Cluster) -> BotEventRow | None:
    primary = _primary_event(cluster)
    if primary is None:
        if not any(step.gate_result is GateStepResult.BLOCK for step in cluster.gate_steps):
            return None
        return BotEventRow(
            seq=cluster.first_seq,
            ts_ms=cluster.first_ts_ms,
            event_type=BotEventType.BLOCKED,
            source_authority=_blocking_source_authority(cluster.gate_steps),
            identity=cluster.identity,
            severity=BotEventSeverity.WARNING,
            headline="Evaluation blocked",
            narrative="A live gate blocked this evaluation before an order was submitted.",
            gate_steps=tuple(cluster.gate_steps),
            facts=_row_facts(cluster),
        )

    event_type = BotEventType(primary.event_type.value)
    terminal_error = primary.terminal_error
    if event_type is BotEventType.BLOCKED and not any(
        step.gate_result is GateStepResult.BLOCK for step in cluster.gate_steps
    ):
        raise ValueError("blocked bot event projection requires a captured blocking gate-step")
    severity = _severity_for(event_type)
    headline, narrative = _copy_for(event_type, terminal_error)
    return BotEventRow(
        seq=primary.seq,
        ts_ms=primary.ts_ms,
        event_type=event_type,
        source_authority=primary.source_authority,
        identity=cluster.identity,
        severity=severity,
        headline=headline,
        narrative=narrative,
        gate_steps=tuple(cluster.gate_steps),
        terminal_error=terminal_error,
        facts=_row_facts(cluster),
    )


def _primary_event(cluster: _Cluster) -> BotEventRaw | None:
    if not cluster.events:
        return None
    return max(
        cluster.events,
        key=lambda event: (_EVENT_PRIORITY.get(event.event_type, 0), event.ts_ms, event.seq),
    )


def _blocking_source_authority(gate_steps: list[GateStep]) -> SourceAuthority:
    for step in gate_steps:
        if step.gate_result is GateStepResult.BLOCK:
            return step.source_authority
    return gate_steps[0].source_authority


def _severity_for(event_type: BotEventType) -> BotEventSeverity:
    if event_type in {BotEventType.ORDER_REJECTED, BotEventType.HALTED, BotEventType.LAUNCH_FAILED}:
        return BotEventSeverity.CRITICAL
    return BotEventSeverity.INFO


def _copy_for(event_type: BotEventType, terminal_error: TerminalError | None) -> tuple[str, str]:
    if event_type is BotEventType.ORDER_REJECTED:
        detail = _terminal_detail(terminal_error)
        return "IBKR rejected the order", detail
    if event_type is BotEventType.HALTED:
        detail = _terminal_detail(terminal_error)
        return "Bot halted", detail
    if event_type is BotEventType.LAUNCH_FAILED:
        detail = _terminal_detail(terminal_error)
        return "Bot failed to launch", detail
    if event_type is BotEventType.SIGNAL_FIRED:
        return "Signal fired", "The strategy decided to act on this evaluation."
    if event_type is BotEventType.ORDER_SUBMITTED:
        return "Order submitted", "The order was submitted and is awaiting broker outcome."
    if event_type is BotEventType.ORDER_FILLED:
        return "Order filled", "The broker reported an execution for this order."
    if event_type is BotEventType.ORDER_CANCELLED:
        return "Order cancelled", "The broker reported this order as cancelled."
    return "Bot event", f"Bot event {event_type.value} was observed."


def _terminal_detail(terminal_error: TerminalError | None) -> str:
    if terminal_error is None:
        return "A terminal bot event was observed without additional detail."
    return terminal_error.external_message or terminal_error.detail or terminal_error.message


def _idle_row(batch: list[BotEventRaw]) -> BotEventRow:
    first = batch[0]
    last = batch[-1]
    count = len(batch)
    narrative = (
        "The bot evaluated this bar without a trade signal."
        if count == 1
        else f"The bot evaluated {count} bars without a trade signal."
    )
    return BotEventRow(
        seq=first.seq,
        ts_ms=first.ts_ms,
        event_type=BotEventType.EVALUATION_IDLE,
        source_authority=first.source_authority,
        identity=first.identity,
        severity=BotEventSeverity.INFO,
        headline="Evaluating, no signal",
        narrative=narrative,
        facts={
            "folded_count": count,
            "first_raw_seq": first.seq,
            "last_raw_seq": last.seq,
            "last_ts_ms": last.ts_ms,
            "raw_event_seqs": [event.seq for event in batch],
        },
    )


def _merge_idle_rows(idle_events: list[BotEventRaw], rows: list[BotEventRow]) -> list[BotEventRow]:
    projected: list[BotEventRow] = []
    idle_batch: list[BotEventRaw] = []
    items: list[tuple[int, int, int, BotEventRaw | BotEventRow]] = []
    items.extend((event.ts_ms, event.seq, 0, event) for event in idle_events)
    items.extend((row.ts_ms, row.seq, 1, row) for row in rows)
    for _ts_ms, _seq, kind, item in sorted(items, key=lambda entry: (entry[0], entry[1], entry[2])):
        if kind == 0:
            if not isinstance(item, BotEventRaw):
                raise AssertionError("idle projection item must be BotEventRaw")
            idle_batch.append(item)
            continue
        if not isinstance(item, BotEventRow):
            raise AssertionError("row projection item must be BotEventRow")
        if idle_batch:
            projected.append(_idle_row(idle_batch))
            idle_batch = []
        projected.append(item)
    if idle_batch:
        projected.append(_idle_row(idle_batch))
    return projected


def _row_facts(cluster: _Cluster) -> dict[str, FactValue]:
    facts = dict(cluster.facts)
    raw_events = [*cluster.events]
    facts["raw_event_seqs"] = [event.seq for event in raw_events]
    facts["raw_event_types"] = [event.event_type.value for event in raw_events]
    if cluster.gate_steps:
        facts["gate_ids"] = [step.gate_id for step in cluster.gate_steps]
    return facts


def _merge_identity(current: BotEventIdentity, incoming: BotEventIdentity) -> BotEventIdentity:
    return BotEventIdentity(
        evaluation_id=incoming.evaluation_id or current.evaluation_id,
        intent_id=incoming.intent_id or current.intent_id,
        order_ref=incoming.order_ref or current.order_ref,
        req_id=incoming.req_id if incoming.req_id is not None else current.req_id,
        order_id=incoming.order_id if incoming.order_id is not None else current.order_id,
        perm_id=incoming.perm_id if incoming.perm_id is not None else current.perm_id,
        exec_id=incoming.exec_id or current.exec_id,
    )


__all__ = ["project_bot_event_rows"]
