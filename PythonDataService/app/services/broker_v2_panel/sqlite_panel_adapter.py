"""Adapt SQLite Clerk folds to the existing Broker V2 panel contract.

The panel remains the product surface.  This module is only a projection
adapter: it neither replays transitions nor authors a second recovery policy.
Every action and action token comes from the SQLite recovery catalog.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
    RecoveryCapability,
)
from app.broker.v2panel.vocabulary import copy_for
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    MissionVerdictView,
    PanelAction,
    ReadinessCheckView,
    StationView,
    TransactionRail,
    WorkingOrderView,
)
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    OperatorBlocker,
    OperatorConfirmationCopy,
)

_WORKING_BROKER_STATES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)
# Matches the established station-derivation semantic (`station_derivation.py`
# `_fill_station`): FILL is satisfied only by actual fill evidence, never by
# any terminal outcome — a canceled/expired/rejected order definitively
# received zero fill and must not render as satisfied.
_FILLED_BROKER_STATES = frozenset({"filled", "partially_filled"})


def adapt_sqlite_panel(
    panel: BotPanelView,
    projection: ClerkProjection,
) -> BotPanelView:
    """Replace JSONL-derived custody fields with one SQLite fold snapshot."""
    actions = [_panel_action(item, projection.control_revision) for item in projection.recovery_actions]
    checks = [_readiness_check(item, projection.generated_at_ms) for item in projection.recovery_actions]
    ready_count = sum(check.ready for check in checks)
    return panel.model_copy(
        update={
            "updated_at_ms": projection.generated_at_ms,
            "revision": projection.control_revision,
            "mission_verdict": _mission_verdict(panel, projection),
            "rail": _transaction_rail(
                projection,
                selected_ref=panel.rail.transaction_ref,
            ),
            "journal_tail_ref": (
                f"/api/alpaca-clerk-sqlite/accounts/{projection.account_id}/bots/"
                f"{projection.strategy_instance_id}/timeline"
            ),
            "journal_tail_seq": projection.control_revision,
            "actions": actions,
            "readiness_checks": checks,
            "readiness_ready_count": ready_count,
            "readiness_blocked_count": len(checks) - ready_count,
            "exposure": {
                position.symbol: position.attributed_qty
                for position in projection.positions
                if abs(position.attributed_qty) > 0
            },
            "working_orders": _working_orders(panel, projection),
            # SQLite is the sole custody projection after activation.  Legacy
            # JSONL fill/P&L rollups must not be mixed into this evidence cut.
            "recent_fills": [],
            "fills_today": None,
            "realized_pnl_today": None,
            "open_pnl": None,
        }
    )


def adapt_sqlite_catalog(
    rows: list[BotCatalogView],
    projections: dict[str, ClerkProjection],
) -> list[BotCatalogView]:
    """Replace legacy roster rollups/actions with per-bot SQLite folds."""
    adapted: list[BotCatalogView] = []
    for row in rows:
        projection = projections.get(row.strategy_instance_id)
        if projection is None:
            adapted.append(row)
            continue
        timestamps = (
            *(item.updated_at_ms for item in projection.operations),
            *(item.updated_at_ms for item in projection.positions),
            *(item.updated_at_ms for item in projection.commands),
        )
        adapted.append(
            row.model_copy(
                update={
                    "exposure": {
                        position.symbol: position.attributed_qty
                        for position in projection.positions
                        if abs(position.attributed_qty) > 0
                    },
                    "fills_today": None,
                    "realized_pnl_today": None,
                    "open_pnl": None,
                    "last_activity_at_ms": max(timestamps, default=None),
                    "needs_attention": bool(
                        projection.holds
                        or projection.uncertainties
                        or projection.authority_health != "healthy"
                    ),
                    # Recovery mutations require the bot panel's typed
                    # confirmation flow; the compact fleet row links there.
                    "row_action": None,
                }
            )
        )
    return adapted


def _panel_action(capability: RecoveryCapability, revision: int) -> PanelAction:
    blocker = None if capability.available else _capability_blocker(capability)
    confirmation = capability.confirmation
    return PanelAction(
        action_id=capability.action_id,
        label=capability.label,
        explanation=capability.explanation,
        enabled=capability.available,
        blockers=[] if blocker is None else [blocker],
        confirmation=(
            None
            if confirmation is None
            else OperatorConfirmationCopy(
                title=confirmation.title,
                body=capability.explanation,
                consequence=confirmation.explanation,
                confirm_label=confirmation.confirm_label,
            )
        ),
        revision=revision,
        concurrency_token=capability.concurrency_token,
    )


def _capability_blocker(capability: RecoveryCapability) -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id=capability.unavailable_reason_code or "RECOVERY_ACTION_UNAVAILABLE",
        scope="bot" if capability.scope == "BOT" else "account",
        host="bot_cockpit",
        anchor=SURFACE_ANCHOR,
        audience="both",
        disposition="wait",
        headline=capability.unavailable_reason or "This recovery action is unavailable.",
        detail=capability.next_step,
        applies_to="run",
        evidence={
            "freshness": capability.freshness,
            "evidence_count": len(capability.evidence),
        },
    )


def _readiness_check(
    capability: RecoveryCapability,
    evaluated_at_ms: int,
) -> ReadinessCheckView:
    return ReadinessCheckView(
        operation=capability.action_id,
        label=capability.label,
        ready=capability.available,
        scope="bot" if capability.scope == "BOT" else "account",
        authority="SQLite Account Clerk recovery policy",
        explanation=(
            capability.explanation
            if capability.available
            else capability.unavailable_reason or capability.explanation
        ),
        evidence={
            "freshness": capability.freshness,
            "evidence_count": len(capability.evidence),
            "primary": capability.primary,
        },
        evaluated_at_ms=evaluated_at_ms,
        cure=None if capability.available else capability.next_step,
    )


def _mission_verdict(
    panel: BotPanelView,
    projection: ClerkProjection,
) -> MissionVerdictView:
    guidance = projection.guidance
    if projection.authority_health != "healthy" or projection.uncertainties or projection.holds:
        state = "blocked"
        label = "Mission blocked"
    elif panel.health.running:
        state = "working"
        label = "Working"
    elif panel.health.phase == "RETIRED":
        state = "retired"
        label = "Retired"
    else:
        state = "off_duty"
        label = "Off duty"
    return MissionVerdictView(
        state=state,
        label=label,
        explanation=guidance.explanation,
        next_action=guidance.next_step,
        evaluated_at_ms=projection.generated_at_ms,
    )


def _working_orders(
    panel: BotPanelView,
    projection: ClerkProjection,
) -> list[WorkingOrderView]:
    return [
        WorkingOrderView(
            order_ref=order.order_ref,
            broker_order_id=order.broker_order_id,
            symbol=panel.symbol,
            side="Unknown — inspect custody timeline",
            quantity=None,
            filled_quantity=None,
            status=order.broker_state or "unknown",
            observed_at_ms=order.updated_at_ms,
        )
        for operation in projection.operations
        for order in operation.orders
        if order.broker_order_id is not None
        and (order.broker_state or "").lower() in _WORKING_BROKER_STATES
    ]


def _transaction_rail(
    projection: ClerkProjection,
    *,
    selected_ref: str | None,
) -> TransactionRail:
    operation = _select_operation(projection, selected_ref)
    if operation is None:
        return TransactionRail(
            transaction_ref=None,
            stations=[_station(station_id, "not_applicable", "No custody operation selected.") for station_id in _station_ids()],
        )
    has_broker_ack = any(order.broker_order_id is not None for order in operation.orders)
    has_fill_evidence = any(
        (order.broker_state or "").lower() in _FILLED_BROKER_STATES for order in operation.orders
    )
    reconciled = (
        operation.terminal_receipt_id is not None
        or (
            projection.latest_reconciliation is not None
            and projection.latest_reconciliation.effect_operation_id
            == operation.effect_operation_id
            and projection.latest_reconciliation.outcome == "RESOLVED_SUCCESS"
        )
    )
    stations = [
        _station("SIGNAL", "satisfied", "The durable command records the requested operation."),
        _station("INTENT", "satisfied", "The Clerk captured the operation before broker contact."),
        _station("SUBMIT_GATE", "satisfied", "SQLite custody accepted the operation under its active authority."),
        _station(
            "BROKER_ACK",
            "satisfied" if has_broker_ack else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "A broker order identity is durably linked." if has_broker_ack else "No broker acknowledgment is durably linked yet.",
        ),
        _station(
            "FILL",
            "satisfied" if has_fill_evidence else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "A broker fill or partial fill is durably recorded." if has_fill_evidence else "No fill has been recorded for this order.",
        ),
        _station(
            "RECONCILED",
            "satisfied" if reconciled else ("unknown_stale" if operation.state == "unknown" else "waiting"),
            "Terminal or reconciliation evidence closes the custody chain." if reconciled else "Awaiting terminal or reconciliation evidence.",
        ),
    ]
    return TransactionRail(
        transaction_ref=operation.effect_operation_id,
        stations=stations,
    )


def _select_operation(
    projection: ClerkProjection,
    selected_ref: str | None,
) -> ProjectedOperation | None:
    if selected_ref is not None:
        for operation in projection.operations:
            if operation.effect_operation_id == selected_ref or any(
                order.order_ref == selected_ref for order in operation.orders
            ):
                return operation
    return projection.operations[0] if projection.operations else None


def _station_ids() -> tuple[str, ...]:
    return ("SIGNAL", "INTENT", "SUBMIT_GATE", "BROKER_ACK", "FILL", "RECONCILED")


def _station(station_id: str, state: str, receipt: str) -> StationView:
    station_copy = copy_for(station_id)
    state_copy = copy_for(state)
    return StationView(
        station_id=station_id,
        label=station_copy.label,
        state=state,
        state_label=state_copy.label,
        receipt=receipt,
        evidence_at_ms=None,
        blocker=None,
    )


__all__ = ["adapt_sqlite_catalog", "adapt_sqlite_panel"]
