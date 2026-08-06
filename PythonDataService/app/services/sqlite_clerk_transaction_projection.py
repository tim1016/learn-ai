"""Adapt active SQLite Clerk folds to the existing Account Desk contract."""

from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.projection_models import (
    OperationPage,
    ProjectedOperation,
    TimelineEntry,
)
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.clerk_transaction_projection import (
    ClerkCustodyTimeline,
    ClerkCustodyWindowSummary,
    ClerkTransactionEventRow,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
    ClerkTransactionSummaryRow,
    TransactionOrigin,
)


def sqlite_transaction_history(
    *,
    account_id: str,
    limit: int,
    cursor: str | None,
    origin: TransactionOrigin | None,
    lifecycle_state: str | None,
    strategy_instance_id: str | None,
    run_id: str | None,
) -> ClerkTransactionHistoryResponse | None:
    """Return ``None`` unless SQLite is the boot-selected authority."""
    reader = _active_reader(account_id)
    if reader is None:
        return None
    try:
        page = reader.operation_page(
            strategy_instance_id=strategy_instance_id,
            lifecycle_state=(
                "__NO_SQLITE_STRATEGY_ORIGIN__"
                if origin is not None and origin != "strategy"
                else lifecycle_state
            ),
            run_id=run_id,
            cursor=cursor,
            page_size=limit,
        )
    finally:
        reader.close()
    rows = [_summary_row(operation, account_id=account_id) for operation in page.operations]
    return ClerkTransactionHistoryResponse(
        projection_available=True,
        canonical_fallback_required=False,
        feed_state="live",
        feed_headline="SQLite custody projection live",
        feed_detail=(
            "This operation-first page comes directly from the active Account Clerk's "
            "materialized folds; the retired Postgres projection was not consulted."
        ),
        high_water_journal_seq=page.high_water_sequence,
        lag_records=0,
        lag_is_lower_bound=False,
        custody_summary=_window_summary(page),
        rows=rows,
        next_cursor=page.next_cursor,
    )


def sqlite_transaction_detail(
    *,
    account_id: str,
    transaction_id: str,
) -> tuple[bool, ClerkTransactionRow | None]:
    """Return ``(active, row)`` so a SQLite miss never falls through to Postgres."""
    reader = _active_reader(account_id)
    if reader is None:
        return False, None
    try:
        operation = reader.operation(transaction_id)
        if operation is None:
            return True, None
        timeline = reader.operation_timeline(transaction_id)
    finally:
        reader.close()
    return True, _detail_row(operation, timeline, account_id=account_id)


def _active_reader(account_id: str) -> SqliteClerkProjectionReader | None:
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.authority_kind != "sqlite":
        return None
    clerk = runtime.clerk
    if not isinstance(clerk, SqliteAlpacaClerkFacade):
        raise RuntimeError("Active SQLite Clerk does not expose its read authority")
    if clerk.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    return SqliteClerkProjectionReader.from_repository(clerk.repository)


def _summary_row(
    operation: ProjectedOperation,
    *,
    account_id: str,
) -> ClerkTransactionSummaryRow:
    order = operation.orders[0] if operation.orders else None
    if operation.run_id is None:
        raise RuntimeError("SQLite broker operation is missing its lifecycle run identity")
    return ClerkTransactionSummaryRow(
        transaction_id=operation.effect_operation_id,
        broker="alpaca",
        account_id=account_id,
        journal_seq=operation.latest_transition_sequence,
        recorded_at_ms=operation.updated_at_ms,
        transaction_kind=operation.kind,
        transaction_origin="strategy",
        strategy_instance_id=operation.strategy_instance_id,
        run_id=operation.run_id,
        intent_id=operation.command.command_id,
        order_ref=(order.order_ref if order is not None else operation.effect_operation_id),
        native_order_id=(order.broker_order_id if order is not None else None),
        lifecycle_state=operation.state,
        event_count=operation.transition_count,
    )


def _detail_row(
    operation: ProjectedOperation,
    timeline: tuple[TimelineEntry, ...],
    *,
    account_id: str,
) -> ClerkTransactionRow:
    summary = _summary_row(operation, account_id=account_id)
    events = [
        ClerkTransactionEventRow(
            event_id=f"{operation.effect_operation_id}:transition:{entry.sequence}",
            broker="alpaca",
            event_kind=entry.transition_kind,
            callback_identity=f"transition:{entry.sequence}",
            lifecycle_state=entry.operation_state,
            native_order_id=entry.broker_order_id,
            journal_seq=entry.sequence,
            recorded_at_ms=entry.recorded_at_ms,
            receipt={
                "operation_ref": entry.operation_ref,
                "custody_owner": entry.custody_owner,
                "execution_authority": entry.execution_authority,
                "proof_reference": entry.proof_reference,
                "source_event_at_ms": entry.source_event_at_ms,
                "clerk_observed_at_ms": entry.clerk_observed_at_ms,
                "recorded_at_ms": entry.recorded_at_ms,
            },
        )
        for entry in timeline
    ]
    return ClerkTransactionRow(
        **summary.model_dump(exclude={"event_count"}),
        receipt={
            "effect_operation_id": operation.effect_operation_id,
            "command_id": operation.command.command_id,
            "custody_owner": operation.custody_owner,
            "terminal_receipt_id": operation.terminal_receipt_id,
            "order_refs": [item.order_ref for item in operation.orders],
        },
        events=events,
        custody_timeline=_custody_timeline(operation, timeline),
    )


def _custody_timeline(
    operation: ProjectedOperation,
    timeline: tuple[TimelineEntry, ...],
) -> ClerkCustodyTimeline:
    first = timeline[0] if timeline else None
    broker_ack = next(
        (entry for entry in timeline if entry.transition_kind == "ORDER_SUBMIT_ACKED"),
        None,
    )
    broker_source_times = [
        entry.source_event_at_ms
        for entry in timeline
        if entry.source_event_at_ms is not None
    ]
    terminal = (
        timeline[-1]
        if timeline and operation.state in {"failed", "succeeded"}
        else None
    )
    return ClerkCustodyTimeline(
        intent_created_at_ms=operation.created_at_ms,
        clerk_intake_admitted_at_ms=(
            first.clerk_observed_at_ms if first is not None else None
        ),
        a0_custody_accepted_at_ms=(
            first.recorded_at_ms if first is not None else None
        ),
        broker_ack_recorded_at_ms=(
            broker_ack.recorded_at_ms if broker_ack is not None else None
        ),
        earliest_broker_source_at_ms=(
            min(broker_source_times) if broker_source_times else None
        ),
        first_callback_arrived_at_ms=(
            first.clerk_observed_at_ms if first is not None else None
        ),
        first_callback_recorded_at_ms=(
            first.recorded_at_ms if first is not None else None
        ),
        economic_terminal_recorded_at_ms=(
            terminal.recorded_at_ms if terminal is not None else None
        ),
    )


def _window_summary(page: OperationPage) -> ClerkCustodyWindowSummary:
    operations = page.operations
    return ClerkCustodyWindowSummary(
        record_count=len(operations),
        a0_custody_accepted_count=len(operations),
        a1_broker_write_started_count=sum(
            operation.state in {"in_progress", "unknown", "failed", "succeeded"}
            for operation in operations
        ),
        a2_broker_known_count=sum(
            any(order.broker_order_id is not None for order in operation.orders)
            for operation in operations
        ),
        a3_economic_terminal_count=sum(
            operation.state in {"failed", "succeeded"} for operation in operations
        ),
        uncertain_count=sum(operation.state == "unknown" for operation in operations),
    )
