"""Receipt authoring for lifecycle chart facts."""

from __future__ import annotations

from app.schemas.live_runs import BotLifecycleEvent, LifecycleChartReceipt, OperatorSurface


def chart_receipt(
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


def daily_order_cap_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    cap = surface.daily_order_cap
    receipts: list[LifecycleChartReceipt] = []
    if cap.used is not None:
        receipts.append(chart_receipt("daily_order_cap.used", cap.used, unit="orders", source="readiness"))
    if cap.limit is not None:
        receipts.append(chart_receipt("daily_order_cap.limit", cap.limit, unit="orders", source="readiness"))
    return tuple(receipts)


def configuration_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    return tuple(
        chart_receipt(
            "configuration.reason_code",
            code,
            source="operator_surface.configuration",
        )
        for code in surface.configuration.reason_codes
    )


def command_loop_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    freshness = surface.runtime_freshness
    if freshness is None:
        return ()
    return tuple(
        chart_receipt(
            "runtime_freshness.command_loop.stale_reason_code",
            code,
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
            chart_receipt("failure_reason", reconciliation.failure_reason, source="reconciliation_projection", ts_ms=ts_ms)
        )
    return tuple(receipts)


def account_owner_receipts(surface: OperatorSurface) -> tuple[LifecycleChartReceipt, ...]:
    owner = surface.account_owner
    if owner is None:
        return ()
    ts_ms = owner.recorded_at_ms
    return (
        chart_receipt("account_owner.phase", owner.phase, source=owner.source, ts_ms=ts_ms),
        chart_receipt(
            "account_owner.generation",
            owner.generation if owner.generation is not None else "unknown",
            source=owner.source,
            ts_ms=ts_ms,
        ),
    )


__all__ = [
    "account_owner_receipts",
    "chart_receipt",
    "command_loop_receipts",
    "configuration_receipts",
    "daily_order_cap_receipts",
    "event_receipts",
    "incident_receipts",
    "reconciliation_receipts",
]
