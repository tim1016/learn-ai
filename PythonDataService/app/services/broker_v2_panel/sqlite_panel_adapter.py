"""Adapt SQLite Clerk folds to the existing Broker V2 panel contract.

The panel remains the product surface.  This module is only a projection
adapter: it neither replays transitions nor authors a second recovery policy.
Every action and action token comes from the SQLite recovery catalog.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
    ProjectedOrder,
    RecoveryCapability,
)
from app.broker.v2panel.vocabulary import copy_for
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    MissionVerdictView,
    PanelAction,
    ReadinessCheckView,
    RecentFillView,
    StationView,
    TransactionRail,
    WorkingOrderView,
)
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    OperatorBlocker,
    OperatorConfirmationCopy,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    compose_catalog_view,
    require_sqlite_catalog_identity,
    sqlite_catalog_rollup,
)

_WORKING_BROKER_STATES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "pending_cancel"}
)
# Matches the established station-derivation semantic (`station_derivation.py`
# `_fill_station`): FILL is satisfied only by actual fill evidence, never by
# any terminal outcome — a canceled/expired/rejected order definitively
# received zero fill and must not render as satisfied.
_FILLED_BROKER_STATES = frozenset({"filled", "partially_filled"})

# SQLite owns broker-recovery actions after activation, but Resume remains a
# bot-lifecycle action.  Preserve it from the generic panel policy instead of
# trying to reconstruct its admission guard from the custody projection.
SQLITE_PANEL_LIFECYCLE_ACTION_IDS = frozenset({"resume"})


def adapt_sqlite_panel(
    panel: BotPanelView,
    projection: ClerkProjection,
    *,
    economics: EconomicSnapshot | None = None,
) -> BotPanelView:
    """Replace JSONL-derived custody fields with one SQLite fold snapshot.

    Active callers supply ``economics`` at the identical SQLite revision as
    ``projection``.  The optional form remains only for narrow historical
    adapter tests; the active source fails closed before calling this adapter
    without authoritative economic evidence.
    """
    if economics is not None:
        _require_coherent_economic_snapshot(projection, economics)
    lifecycle_actions = [
        action.model_copy(update={"revision": projection.control_revision})
        for action in panel.actions
        if (
            action.action_id in SQLITE_PANEL_LIFECYCLE_ACTION_IDS
            and not panel.health.running
        )
    ]
    actions = [
        *lifecycle_actions,
        *(
            _panel_action(item, projection.control_revision)
            for item in projection.recovery_actions
            if item.action_id not in SQLITE_PANEL_LIFECYCLE_ACTION_IDS
        ),
    ]
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
                if position_quantity_is_nonzero(position.attributed_qty)
            },
            "working_orders": _working_orders(
                panel,
                projection,
                strict=economics is not None,
            ),
            # SQLite is the sole custody projection after activation.  Legacy
            # JSONL fill/P&L rollups must not be mixed into this evidence cut.
            "recent_fills": (
                []
                if economics is None
                else [_recent_fill_view(fill) for fill in economics.recent_fills]
            ),
            "fills_today": None if economics is None else economics.fills_today,
            "realized_pnl_today": (
                None if economics is None else economics.realized_pnl_today
            ),
            "open_pnl": None if economics is None else economics.open_pnl,
        }
    )


def adapt_sqlite_catalog(
    rows: list[BotCatalogView],
    projections: dict[str, ClerkProjection],
    economic_rollups: dict[str, EconomicSnapshot],
) -> list[BotCatalogView]:
    """Replace roster economics with the one S2 SQLite rollup revision."""
    adapted: list[BotCatalogView] = []
    for row in rows:
        economics = economic_rollups.get(row.strategy_instance_id)
        if economics is None:
            raise SqliteCatalogProjectionUnavailable(
                f"Bot '{row.strategy_instance_id}' has no SQLite economic snapshot."
            )
        projection = projections.get(row.strategy_instance_id)
        exposure = {
            symbol: quantity
            for symbol, quantity in economics.exposure.items()
            if position_quantity_is_nonzero(quantity)
        }
        economic_updates: dict[str, object] = {
            "exposure": exposure,
            "fills_today": economics.fills_today,
            "realized_pnl_today": economics.realized_pnl_today,
            "open_pnl": economics.open_pnl,
            "last_activity_at_ms": economics.last_activity_at_ms,
        }
        if projection is None:
            adapted.append(row.model_copy(update=economic_updates))
            continue
        adapted.append(
            row.model_copy(
                update={
                    **economic_updates,
                    "needs_attention": row.needs_attention or bool(
                        projection.holds
                        or projection.uncertainties
                        or projection.authority_health != "healthy"
                    ),
                    "status_explanation": _sqlite_catalog_explanation(
                        row,
                        projection,
                        exposure=exposure,
                    ),
                    # Recovery mutations require the bot panel's typed
                    # confirmation flow; the compact fleet row links there.
                    "row_action": None,
                }
            )
        )
    return adapted


def build_sqlite_catalog(
    statuses: list[BotStatusView],
    projections: dict[str, ClerkProjection],
    *,
    economic_rollups: dict[str, EconomicSnapshot],
    account_id: str,
) -> list[BotCatalogView]:
    """Compose the activated catalog from SQLite config, folds, and economics."""
    identified_statuses = [require_sqlite_catalog_identity(status) for status in statuses]
    _require_one_catalog_economic_revision(
        identified_statuses,
        projections,
        economic_rollups,
        account_id=account_id,
    )
    rows = [
        compose_catalog_view(
            status,
            sqlite_catalog_rollup(economic_rollups[status.strategy_instance_id]),
            account_id=account_id,
        )
        for status in statuses
    ]
    return adapt_sqlite_catalog(rows, projections, economic_rollups)


def _require_one_catalog_economic_revision(
    statuses: list[BotStatusView],
    projections: dict[str, ClerkProjection],
    economic_rollups: dict[str, EconomicSnapshot],
    *,
    account_id: str,
) -> None:
    """Prove the roster's economics came from one account/revision snapshot."""
    snapshots: list[EconomicSnapshot] = []
    for status in statuses:
        snapshot = economic_rollups.get(status.strategy_instance_id)
        if snapshot is None:
            raise SqliteCatalogProjectionUnavailable(
                f"Bot '{status.strategy_instance_id}' has no SQLite economic snapshot."
            )
        if (
            snapshot.strategy_instance_id != status.strategy_instance_id
            or snapshot.account_id != account_id
        ):
            raise SqliteCatalogProjectionUnavailable(
                "SQLite catalog economics do not match the requested bot/account."
            )
        projection = projections.get(status.strategy_instance_id)
        if projection is None:
            raise SqliteCatalogProjectionUnavailable(
                f"Bot '{status.strategy_instance_id}' has no SQLite custody projection."
            )
        if (
            projection.account_id != snapshot.account_id
            or projection.strategy_instance_id != snapshot.strategy_instance_id
            or projection.authority_generation != snapshot.authority_generation
            or projection.control_revision != snapshot.control_revision
        ):
            raise SqliteCatalogRevisionMismatch(
                "SQLite catalog custody and economic folds do not share one authority revision."
            )
        snapshots.append(snapshot)
    revisions = {
        (snapshot.authority_generation, snapshot.control_revision)
        for snapshot in snapshots
    }
    if len(revisions) > 1:
        raise SqliteCatalogProjectionUnavailable(
            "SQLite catalog economics span multiple authority revisions."
        )


def _sqlite_catalog_explanation(
    row: BotCatalogView,
    projection: ClerkProjection,
    *,
    exposure: dict[str, float],
) -> str:
    if projection.holds or projection.uncertainties or projection.authority_health != "healthy":
        return "SQLite Account Clerk evidence requires operator attention."
    if row.phase == "RETIRED":
        return "Retired; no further runs can start."
    if row.running:
        return "Running under SQLite Account Clerk custody."
    if any(position_quantity_is_nonzero(quantity) for quantity in exposure.values()):
        return "Off duty with Clerk-attributed exposure."
    return "Off duty and flat."


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
        evidence_refs=[evidence.reference for evidence in capability.evidence],
    )


def _capability_blocker(capability: RecoveryCapability) -> OperatorBlocker:
    return OperatorBlocker.for_host(
        condition_id=capability.unavailable_reason_code or "RECOVERY_ACTION_UNAVAILABLE",
        scope="bot" if capability.scope == "CUSTODY_SUBJECT" else "account",
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
        scope="bot" if capability.scope == "CUSTODY_SUBJECT" else "account",
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
    *,
    strict: bool,
) -> list[WorkingOrderView]:
    return [
        _working_order_view(panel, order, strict=strict)
        for operation in projection.operations
        for order in operation.orders
        if order.broker_order_id is not None
        and (order.broker_state or "").lower() in _WORKING_BROKER_STATES
    ]


def _working_order_view(
    panel: BotPanelView,
    order: ProjectedOrder,
    *,
    strict: bool,
) -> WorkingOrderView:
    """Shape one durable working order without consulting broker or JSONL."""
    if strict and (
        order.symbol is None
        or order.side is None
        or order.quantity is None
        or order.filled_quantity is None
    ):
        raise SqlitePanelAdapterUnavailable(
            f"SQLite order {order.order_ref!r} lacks durable leg or fill evidence"
        )
    return WorkingOrderView(
        order_ref=order.order_ref,
        broker_order_id=order.broker_order_id or "",
        symbol=order.symbol or panel.symbol,
        side=order.side or "unavailable",
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
        status=order.broker_state or "unknown",
        observed_at_ms=order.updated_at_ms,
    )


def _recent_fill_view(fill: FillRecord) -> RecentFillView:
    """Adapt one S2 fill record to the existing panel wire contract."""
    return RecentFillView(
        order_ref=fill.order_ref,
        symbol=fill.symbol,
        side=fill.side.value,
        quantity=fill.quantity,
        price=fill.fill_price,
        filled_at_ms=fill.filled_at_ms,
    )


class SqlitePanelAdapterUnavailable(RuntimeError):
    """The active SQLite panel cannot safely present an incomplete fold."""


def _require_coherent_economic_snapshot(
    projection: ClerkProjection,
    economics: EconomicSnapshot,
) -> None:
    if (
        economics.account_id != projection.account_id
        or economics.strategy_instance_id != projection.strategy_instance_id
        or economics.authority_generation != projection.authority_generation
        or economics.control_revision != projection.control_revision
    ):
        raise SqlitePanelAdapterUnavailable(
            "SQLite custody and economic projections do not share one authority revision"
        )


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


__all__ = [
    "SQLITE_PANEL_LIFECYCLE_ACTION_IDS",
    "adapt_sqlite_catalog",
    "adapt_sqlite_panel",
    "build_sqlite_catalog",
]
