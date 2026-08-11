"""Adapt active SQLite Clerk folds to the existing Account Desk contract."""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionError,
    ExecutionRow,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.external_orders import (
    SqliteExternalOrderReader,
    acknowledge_external_order,
)
from app.broker.alpaca.clerk.sqlite.models import ControlMetaSnapshot, ExternalOrderResource
from app.broker.alpaca.clerk.sqlite.projection_models import (
    OperationPage,
    ProjectedOperation,
    ProjectedOrder,
    TimelineEntry,
)
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository_external_order_api import (
    ExternalOrderNotFoundError,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.engine.live.order_identity import (
    OwnershipRung,
    build_bot_order_namespace,
    build_manual_order_namespace,
    classify_ownership,
)
from app.schemas.clerk_transaction_projection import (
    ClerkCustodyTimeline,
    ClerkCustodyWindowSummary,
    ClerkOrderInstruction,
    ClerkTransactionEventRow,
    ClerkTransactionHistoryResponse,
    ClerkTransactionRow,
    ClerkTransactionSummaryRow,
    TransactionOrigin,
)
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable

_EXTERNAL_TRANSACTION_PREFIX = "external-order:"
_ALL_HISTORY_CURSOR_KIND = "sqlite-account-history-v1"
# `transaction_origin` is not a SQL column — it is classified in Python from
# each operation's order namespace. A sparse origin (e.g. "unknown") can
# legitimately be absent from many consecutive raw pages, so a single
# unfiltered page is not a reliable "no matches" signal. This caps how many
# raw pages one request will scan to fill its origin-filtered page before
# handing the caller a (possibly short) page plus a cursor to continue from.
_ORIGIN_FILTER_MAX_PAGE_FETCHES = 20


class ExternalOrderAcknowledgementNotFound(ValueError):
    """The active SQLite authority has no requested external-order observation."""


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
    if origin == "external":
        return _external_transaction_history(
            account_id=account_id,
            limit=limit,
            cursor=cursor,
            lifecycle_state=lifecycle_state,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        )
    if origin is None:
        return _all_transaction_history(
            account_id=account_id,
            limit=limit,
            cursor=cursor,
            lifecycle_state=lifecycle_state,
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
        )
    readers = _active_readers(account_id)
    if readers is None:
        return None
    reader, economic_reader = readers
    try:
        rows, page, matching_operations = _origin_filtered_operation_page(
            reader,
            economic_reader,
            account_id=account_id,
            origin=origin,
            strategy_instance_id=strategy_instance_id,
            lifecycle_state=lifecycle_state,
            run_id=run_id,
            cursor=cursor,
            limit=limit,
        )
    except EconomicProjectionError as exc:
        raise ClerkTransactionProjectionUnavailable(
            "SQLite execution economics are unavailable for transaction history."
        ) from exc
    finally:
        reader.close()
        economic_reader.close()
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
        custody_summary=_all_window_summary(matching_operations, record_count=len(rows)),
        rows=rows,
        next_cursor=page.next_cursor,
    )


def _origin_filtered_operation_page(
    reader: SqliteClerkProjectionReader,
    economic_reader: SqliteEconomicProjectionReader,
    *,
    account_id: str,
    origin: TransactionOrigin,
    strategy_instance_id: str | None,
    lifecycle_state: str | None,
    run_id: str | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[ClerkTransactionSummaryRow], OperationPage, list[ProjectedOperation]]:
    """Scan raw operation pages until the origin-filtered page fills.

    ``transaction_origin`` is classified in Python from each operation's
    order namespace, not a SQL predicate on the underlying page. A sparse
    origin (e.g. ``unknown``) can legitimately be absent from a raw page
    while still having matches further back, so a single unfiltered fetch is
    not a reliable "no matches" signal. This never truncates a raw page's
    matches mid-page — the returned row count can exceed ``limit`` by up to
    one page rather than silently dropping a match whose page also happened
    to fill the quota.
    """
    rows: list[ClerkTransactionSummaryRow] = []
    matched_operations: list[ProjectedOperation] = []
    page: OperationPage | None = None
    next_cursor = cursor
    for _ in range(_ORIGIN_FILTER_MAX_PAGE_FETCHES):
        page = reader.operation_page(
            strategy_instance_id=strategy_instance_id,
            lifecycle_state=lifecycle_state,
            run_id=run_id,
            cursor=next_cursor,
            page_size=limit,
        )
        summaries = economic_reader.effective_execution_summaries(
            tuple(
                order.order_ref
                for operation in page.operations
                for order in _economic_orders(operation)
            )
        )
        for operation in page.operations:
            row = _summary_row(operation, account_id=account_id, execution_summaries=summaries)
            if row.transaction_origin == origin:
                rows.append(row)
                matched_operations.append(operation)
        next_cursor = page.next_cursor
        if len(rows) >= limit or next_cursor is None:
            break
    assert page is not None
    return rows, page, matched_operations


def sqlite_transaction_detail(
    *,
    account_id: str,
    transaction_id: str,
) -> tuple[bool, ClerkTransactionRow | None]:
    """Return ``(active, row)`` so a SQLite miss never falls through to Postgres."""
    if transaction_id.startswith(_EXTERNAL_TRANSACTION_PREFIX):
        clerk = _active_clerk(account_id)
        if clerk is None:
            return False, None
        external_order_id = transaction_id.removeprefix(_EXTERNAL_TRANSACTION_PREFIX)
        if not external_order_id:
            return True, None
        external_order = clerk.repository.external_order(external_order_id)
        return (
            True,
            None
            if external_order is None
            else _external_detail_row(external_order, account_id=account_id),
        )
    readers = _active_readers(account_id)
    if readers is None:
        return False, None
    reader, economic_reader = readers
    try:
        operation = reader.operation(transaction_id)
        if operation is None:
            return True, None
        timeline = reader.operation_timeline(transaction_id)
        executions = economic_reader.effective_order_executions(
            tuple(order.order_ref for order in _economic_orders(operation))
        )
    except EconomicProjectionError as exc:
        raise ClerkTransactionProjectionUnavailable(
            "SQLite execution economics are unavailable for transaction detail."
        ) from exc
    finally:
        reader.close()
        economic_reader.close()
    return True, _detail_row(
        operation,
        timeline,
        account_id=account_id,
        executions=executions,
    )


def sqlite_acknowledge_external_order(
    *,
    account_id: str,
    external_order_id: str,
    operator: str,
) -> ExternalOrderResource | None:
    """Acknowledge one external-order observation in the active SQLite authority.

    ``None`` means SQLite is not the selected authority, allowing the router
    to retain its established no-fallback behavior.  The external-order fold
    owns both the durable acknowledgement and narrowly scoped hold release.
    """
    clerk = _active_clerk(account_id)
    if clerk is None:
        return None
    try:
        return acknowledge_external_order(
            clerk.repository,
            external_order_id=external_order_id,
            operator=operator,
        )
    except ExternalOrderNotFoundError as exc:
        raise ExternalOrderAcknowledgementNotFound(str(exc)) from exc


def _active_readers(
    account_id: str,
) -> tuple[SqliteClerkProjectionReader, SqliteEconomicProjectionReader] | None:
    clerk = _active_clerk(account_id)
    if clerk is None:
        return None
    return (
        SqliteClerkProjectionReader.from_repository(clerk.repository),
        SqliteEconomicProjectionReader.from_repository(clerk.repository),
    )


def _active_clerk(account_id: str) -> SqliteAlpacaClerkFacade | None:
    runtime = get_active_clerk_runtime()
    if runtime is None:
        return None
    if runtime.authority_kind == "unavailable":
        failure = runtime.startup_failure
        if (
            failure is not None
            and failure.activation_detected
            and failure.account_id == account_id
        ):
            raise ClerkTransactionProjectionUnavailable(
                "The selected SQLite Clerk authority is unavailable after startup failure."
            )
        return None
    if runtime.authority_kind != "sqlite":
        return None
    clerk = runtime.clerk
    if not isinstance(clerk, SqliteAlpacaClerkFacade):
        raise RuntimeError("Active SQLite Clerk does not expose its read authority")
    if clerk.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    return clerk


def _all_transaction_history(
    *,
    account_id: str,
    limit: int,
    cursor: str | None,
    lifecycle_state: str | None,
    strategy_instance_id: str | None,
    run_id: str | None,
) -> ClerkTransactionHistoryResponse | None:
    """Merge strategy operations and external observations under one keyset cursor.

    The cursor is anchored to one SQLite ``UNION ALL`` projection and each
    source's immutable creation evidence.  It never merges independently
    paginated source pages in application memory, which could otherwise omit
    a deferred external row or duplicate it on the next request. Each pointer
    is subsequently hydrated from its native typed fold before presentation.
    """
    clerk = _active_clerk(account_id)
    if clerk is None:
        return None
    meta = clerk.repository.control_meta_snapshot()
    filters = {
        "lifecycle_state": lifecycle_state,
        "strategy_instance_id": strategy_instance_id,
        "run_id": run_id,
    }
    cursor_key = _decode_all_history_cursor(
        cursor,
        account_id=account_id,
        authority_generation=meta.authority_generation,
        filters=filters,
    )
    pointers, high_water_sequence = _all_history_pointers(
        db_path=clerk.repository.db_path,
        account_id=account_id,
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        lifecycle_state=lifecycle_state,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        cursor_key=cursor_key,
        limit=limit,
    )
    visible = pointers[:limit]
    reader = SqliteClerkProjectionReader.from_repository(clerk.repository)
    economic_reader = SqliteEconomicProjectionReader.from_repository(clerk.repository)
    try:
        operation_ids = tuple(
            pointer[2] for pointer in visible if pointer[1] == "strategy"
        )
        operations = reader.operations_by_ids(operation_ids)
        operations_by_id = {
            operation.effect_operation_id: operation for operation in operations
        }
        summaries = economic_reader.effective_execution_summaries(
            tuple(
                order.order_ref
                for operation in operations
                for order in _economic_orders(operation)
            )
        )
    except EconomicProjectionError as exc:
        raise ClerkTransactionProjectionUnavailable(
            "SQLite execution economics are unavailable for transaction history."
        ) from exc
    finally:
        reader.close()
        economic_reader.close()

    rows: list[ClerkTransactionSummaryRow] = []
    strategy_operations: list[ProjectedOperation] = []
    for _recorded_at_ms, source, identity in visible:
        if source == "strategy":
            operation = operations_by_id.get(identity)
            if operation is None:
                raise ClerkTransactionProjectionUnavailable(
                    "SQLite operation disappeared during account-history hydration."
                )
            strategy_operations.append(operation)
            rows.append(
                _summary_row(
                    operation,
                    account_id=account_id,
                    execution_summaries=summaries,
                )
            )
            continue
        external_order = clerk.repository.external_order(identity)
        if external_order is None:
            raise ClerkTransactionProjectionUnavailable(
                "SQLite external-order observation disappeared during account-history hydration."
            )
        rows.append(_external_summary_row(external_order, account_id=account_id))
    _assert_history_hydration_revision(
        before=meta,
        after=clerk.repository.control_meta_snapshot(),
    )
    next_cursor = (
        _encode_all_history_cursor(
            recorded_at_ms=visible[-1][0],
            source=visible[-1][1],
            identity=visible[-1][2],
            account_id=account_id,
            authority_generation=meta.authority_generation,
            filters=filters,
        )
        if len(pointers) > limit and visible
        else None
    )
    return ClerkTransactionHistoryResponse(
        projection_available=True,
        canonical_fallback_required=False,
        feed_state="live",
        feed_headline="SQLite custody projection live",
        feed_detail=(
            "This page is a single SQLite keyset projection of bot operations and external "
            "observations; retired Postgres and broker order-history reads were not consulted."
        ),
        high_water_journal_seq=high_water_sequence,
        lag_records=0,
        lag_is_lower_bound=False,
        custody_summary=_all_window_summary(strategy_operations, record_count=len(rows)),
        rows=rows,
        next_cursor=next_cursor,
    )


def _assert_history_hydration_revision(
    *,
    before: ControlMetaSnapshot,
    after: ControlMetaSnapshot,
) -> None:
    """Reject a page if separately hydrated folds crossed a SQLite revision.

    The account-history pointer page is read in one SQLite snapshot, while
    the typed operation, economic, and external-order folds deliberately use
    their native readers. A changed control revision anywhere during that
    bounded hydration means those readers could have straddled a writer.
    Refuse the mixed page so the caller can retry against one stable revision.
    """
    if (
        before.account_id,
        before.db_identity_token,
        before.authority_generation,
        before.control_revision,
    ) != (
        after.account_id,
        after.db_identity_token,
        after.authority_generation,
        after.control_revision,
    ):
        raise ClerkTransactionProjectionUnavailable(
            "SQLite account-history changed during typed-fold hydration; retry the page."
        )


def _all_history_pointers(
    *,
    db_path: Path,
    account_id: str,
    authority_generation: int,
    db_identity_token: str,
    lifecycle_state: str | None,
    strategy_instance_id: str | None,
    run_id: str | None,
    cursor_key: tuple[int, str, str] | None,
    limit: int,
) -> tuple[list[tuple[int, str, str]], int]:
    """Read one identity-verified, cross-origin account-history pointer page."""
    if not 1 <= limit <= 100:
        raise ValueError("transaction history limit must be between 1 and 100")
    operation_clauses: list[str] = []
    operation_params: list[object] = []
    for column, value in (
        ("e.state", lifecycle_state),
        ("e.strategy_instance_id", strategy_instance_id),
        ("e.run_id", run_id),
    ):
        if value is not None:
            operation_clauses.append(f"{column} = ?")
            operation_params.append(value)
    external_clauses = [
        "EXISTS (SELECT 1 FROM custody_transitions observed "
        "WHERE observed.broker_order_id = eo.broker_order_id "
        "AND observed.transition_kind = 'EXTERNAL_ORDER_OBSERVED')"
    ]
    if strategy_instance_id is not None or run_id is not None:
        external_clauses.append("0")
    elif lifecycle_state == "review_required":
        external_clauses.append("eo.acknowledged_at_ms IS NULL")
    elif lifecycle_state == "reviewed":
        external_clauses.append("eo.acknowledged_at_ms IS NOT NULL")
    elif lifecycle_state is not None:
        external_clauses.append("0")
    operation_where = (
        f"WHERE {' AND '.join(operation_clauses)}" if operation_clauses else ""
    )
    external_where = f"WHERE {' AND '.join(external_clauses)}"
    cursor_where = ""
    cursor_params: list[object] = []
    if cursor_key is not None:
        cursor_where = (
            "WHERE (recorded_at_ms < ? OR (recorded_at_ms = ? AND "
            "(source < ? OR (source = ? AND identity < ?))))"
        )
        cursor_params.extend(
            (cursor_key[0], cursor_key[0], cursor_key[1], cursor_key[1], cursor_key[2])
        )
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        meta = conn.execute(
            "SELECT account_id, authority_generation, db_identity_token FROM control_meta WHERE id = 1"
        ).fetchone()
        if meta is None or (
            meta["account_id"],
            meta["authority_generation"],
            meta["db_identity_token"],
        ) != (account_id, authority_generation, db_identity_token):
            raise ClerkTransactionProjectionUnavailable(
                "SQLite account-history authority identity changed during projection."
            )
        rows = conn.execute(
            f"""
            WITH history AS (
                SELECT e.created_at_ms AS recorded_at_ms,
                'strategy' AS source,
                e.effect_operation_id AS identity
                FROM effect_operations e
                {operation_where}
                UNION ALL
                SELECT (
                    SELECT observed.recorded_at_ms FROM custody_transitions observed
                    WHERE observed.broker_order_id = eo.broker_order_id
                      AND observed.transition_kind = 'EXTERNAL_ORDER_OBSERVED'
                    ORDER BY observed.sequence ASC LIMIT 1
                ) AS recorded_at_ms,
                'external' AS source,
                eo.external_order_id AS identity
                FROM external_orders eo
                {external_where}
            )
            SELECT recorded_at_ms, source, identity FROM history
            {cursor_where}
            ORDER BY recorded_at_ms DESC, source DESC, identity DESC
            LIMIT ?
            """,
            (*operation_params, *cursor_params, limit + 1),
        ).fetchall()
        high_water = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM custody_transitions"
        ).fetchone()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return [
        (int(row["recorded_at_ms"]), row["source"], row["identity"])
        for row in rows
    ], int(high_water["sequence"])


def _encode_all_history_cursor(
    *,
    recorded_at_ms: int,
    source: str,
    identity: str,
    account_id: str,
    authority_generation: int,
    filters: dict[str, str | None],
) -> str:
    payload = json.dumps(
        {
            "account_id": account_id,
            "authority_generation": authority_generation,
            "filters": filters,
            "identity": identity,
            "kind": _ALL_HISTORY_CURSOR_KIND,
            "recorded_at_ms": recorded_at_ms,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_all_history_cursor(
    cursor: str | None,
    *,
    account_id: str,
    authority_generation: int,
    filters: dict[str, str | None],
) -> tuple[int, str, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
        recorded_at_ms = payload["recorded_at_ms"]
        source = payload["source"]
        identity = payload["identity"]
        if (
            payload["kind"] != _ALL_HISTORY_CURSOR_KIND
            or payload["account_id"] != account_id
            or payload["authority_generation"] != authority_generation
            or payload["filters"] != filters
            or isinstance(recorded_at_ms, bool)
            or not isinstance(recorded_at_ms, int)
            or recorded_at_ms < 0
            or source not in {"strategy", "external"}
            or not isinstance(identity, str)
            or not identity
        ):
            raise ValueError("cursor scope or shape is invalid")
        return recorded_at_ms, source, identity
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("transaction history cursor is invalid") from exc


def _external_transaction_history(
    *,
    account_id: str,
    limit: int,
    cursor: str | None,
    lifecycle_state: str | None,
    strategy_instance_id: str | None,
    run_id: str | None,
) -> ClerkTransactionHistoryResponse | None:
    """Read a bounded external-only transaction page from its separate fold.

    External observations have their own source-native keyset cursor.  They
    intentionally do not share a fabricated merge cursor with bot operations;
    callers select this explicit origin page when reviewing account-scope
    external evidence.
    """
    clerk = _active_clerk(account_id)
    if clerk is None:
        return None
    if strategy_instance_id is not None or run_id is not None:
        page_rows: tuple[ExternalOrderResource, ...] = ()
        next_cursor = None
    else:
        reader = SqliteExternalOrderReader.from_repository(clerk.repository)
        try:
            page = reader.external_orders(
                cursor=cursor,
                page_size=limit,
                lifecycle_state=lifecycle_state,
            )
        finally:
            reader.close()
        page_rows = page.orders
        next_cursor = page.next_cursor
    rows = [_external_summary_row(order, account_id=account_id) for order in page_rows]
    return ClerkTransactionHistoryResponse(
        projection_available=True,
        canonical_fallback_required=False,
        feed_state="live",
        feed_headline="SQLite external-order projection live",
        feed_detail=(
            "This external-order page comes from the active Account Clerk's separate "
            "external observation fold; no bot economics or retired Postgres projection was consulted."
        ),
        high_water_journal_seq=None,
        lag_records=0,
        lag_is_lower_bound=False,
        custody_summary=ClerkCustodyWindowSummary(
            record_count=len(rows),
            a0_custody_accepted_count=0,
            a1_broker_write_started_count=0,
            a2_broker_known_count=0,
            a3_economic_terminal_count=0,
            uncertain_count=0,
        ),
        rows=rows,
        next_cursor=next_cursor,
    )


def _external_summary_row(
    order: ExternalOrderResource,
    *,
    account_id: str,
) -> ClerkTransactionSummaryRow:
    observation_sequence, observation_recorded_at_ms = _external_observation_provenance(order)
    origin = classify_transaction_origin(
        order_ref=order.client_order_id,
        strategy_instance_id=None,
        external_observation=True,
    )
    if origin != "external":
        raise ClerkTransactionProjectionUnavailable(
            "External-order fold conflicts with canonical ownership classification."
        )
    return ClerkTransactionSummaryRow(
        transaction_id=f"{_EXTERNAL_TRANSACTION_PREFIX}{order.external_order_id}",
        broker="alpaca",
        account_id=account_id,
        journal_seq=observation_sequence,
        recorded_at_ms=observation_recorded_at_ms,
        transaction_kind="external_order_observation",
        transaction_origin=origin,
        order_instruction=ClerkOrderInstruction(
            symbol=order.symbol,
            action=order.side.lower(),
            quantity=order.qty,
            order_type=order.order_type,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
        ),
        strategy_instance_id=None,
        run_id=None,
        intent_id=None,
        order_ref=None,
        native_order_id=order.broker_order_id,
        lifecycle_state=("reviewed" if order.acknowledged_at_ms is not None else "review_required"),
        external_order_id=order.external_order_id,
        event_count=2 if order.acknowledgement_sequence is not None else 1,
    )


def _external_detail_row(
    order: ExternalOrderResource,
    *,
    account_id: str,
) -> ClerkTransactionRow:
    summary = _external_summary_row(order, account_id=account_id)
    observation_sequence, observation_recorded_at_ms = _external_observation_provenance(order)
    events = [
        ClerkTransactionEventRow(
            event_id=f"{_EXTERNAL_TRANSACTION_PREFIX}{order.external_order_id}:observed",
            broker="alpaca",
            event_kind="external_order_observed",
            callback_identity=order.broker_order_id,
            lifecycle_state="review_required",
            native_order_id=order.broker_order_id,
            journal_seq=observation_sequence,
            recorded_at_ms=observation_recorded_at_ms,
            receipt={
                "external_order_id": order.external_order_id,
                "client_order_id": order.client_order_id,
                "observed_at_ms": order.observed_at_ms,
                "evidence_refs": list(order.evidence_refs),
            },
        )
    ]
    if order.acknowledged_at_ms is not None:
        if order.acknowledgement_sequence is None or order.acknowledgement_recorded_at_ms is None:
            raise ClerkTransactionProjectionUnavailable(
                "External-order acknowledgement lacks durable transition provenance."
            )
        events.append(
            ClerkTransactionEventRow(
                event_id=f"{_EXTERNAL_TRANSACTION_PREFIX}{order.external_order_id}:acknowledged",
                broker="alpaca",
                event_kind="external_order_acknowledged",
                callback_identity=order.external_order_id,
                lifecycle_state="reviewed",
                native_order_id=order.broker_order_id,
                journal_seq=order.acknowledgement_sequence,
                recorded_at_ms=order.acknowledgement_recorded_at_ms,
                receipt={
                    "external_order_id": order.external_order_id,
                    "acknowledged_at_ms": order.acknowledged_at_ms,
                    "ack_operator": order.ack_operator,
                },
            )
        )
    return ClerkTransactionRow(
        **summary.model_dump(exclude={"event_count"}),
        receipt={
            "external_order_id": order.external_order_id,
            "evidence_refs": list(order.evidence_refs),
        },
        events=events,
        custody_timeline=ClerkCustodyTimeline(
            earliest_broker_source_at_ms=order.observed_at_ms,
            a0_custody_accepted_at_ms=observation_recorded_at_ms,
        ),
    )


def _external_observation_provenance(order: ExternalOrderResource) -> tuple[int, int]:
    if order.observation_sequence is None or order.observation_recorded_at_ms is None:
        raise ClerkTransactionProjectionUnavailable(
            "External-order observation lacks durable transition provenance."
        )
    return order.observation_sequence, order.observation_recorded_at_ms


def _summary_row(
    operation: ProjectedOperation,
    *,
    account_id: str,
    execution_summaries: dict[str, tuple[ExecutionRow, ...]] | None = None,
) -> ClerkTransactionSummaryRow:
    economic_orders = _economic_orders(operation)
    order = (economic_orders or operation.orders)[0] if operation.orders else None
    if operation.run_id is None:
        raise RuntimeError("SQLite broker operation is missing its lifecycle run identity")
    executions = tuple(
        execution
        for projected_order in economic_orders
        for execution in (execution_summaries or {}).get(projected_order.order_ref, ())
    )
    execution = executions[0] if len(executions) == 1 else None
    return ClerkTransactionSummaryRow(
        transaction_id=operation.effect_operation_id,
        broker="alpaca",
        account_id=account_id,
        journal_seq=operation.latest_transition_sequence,
        recorded_at_ms=operation.updated_at_ms,
        transaction_kind=operation.kind,
        transaction_origin=classify_transaction_origin(
            order_ref=(order.order_ref if order is not None else None),
            strategy_instance_id=operation.strategy_instance_id,
        ),
        strategy_instance_id=operation.strategy_instance_id,
        run_id=operation.run_id,
        intent_id=operation.command.command_id,
        order_ref=(order.order_ref if order is not None else operation.effect_operation_id),
        native_order_id=(order.broker_order_id if order is not None else None),
        native_execution_id=(execution.execution_id if execution is not None else None),
        lifecycle_state=operation.state,
        commission_status=("reported" if execution is not None and execution.fee is not None else "unknown"),
        fee=(execution.fee if execution is not None else None),
        fee_fidelity=(execution.fee_fidelity if execution is not None else None),
        execution_quantity=(execution.quantity if execution is not None else None),
        execution_price=(execution.price if execution is not None else None),
        event_count=operation.transition_count,
    )


def _detail_row(
    operation: ProjectedOperation,
    timeline: tuple[TimelineEntry, ...],
    *,
    account_id: str,
    executions: tuple[ExecutionRow, ...],
) -> ClerkTransactionRow:
    economic_orders = _economic_orders(operation)
    economic_order_refs = {order.order_ref for order in economic_orders}
    economic_executions = tuple(
        execution for execution in executions if execution.order_ref in economic_order_refs
    )
    summary = _summary_row(
        operation,
        account_id=account_id,
        execution_summaries={
            order.order_ref: tuple(
                execution
                for execution in executions
                if execution.order_ref == order.order_ref
            )
            for order in economic_orders
        },
    )
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
    events.extend(_execution_event_row(operation, execution) for execution in economic_executions)
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


def _execution_event_row(
    operation: ProjectedOperation,
    execution: ExecutionRow,
) -> ClerkTransactionEventRow:
    """Adapt one exact S2 execution leaf without aggregating its economics."""
    execution_identity = execution.execution_id or execution.fill_id
    return ClerkTransactionEventRow(
        event_id=f"{operation.effect_operation_id}:execution:{execution.fill_id}",
        broker="alpaca",
        event_kind=f"execution_{execution.event_kind}",
        callback_identity=execution_identity,
        lifecycle_state=("filled" if execution.event_kind == "fill" else "corrected"),
        native_execution_id=execution.execution_id,
        commission_status="reported" if execution.fee is not None else "unknown",
        fee=execution.fee,
        fee_fidelity=execution.fee_fidelity,
        execution_quantity=execution.quantity,
        execution_price=execution.price,
        journal_seq=execution.journal_seq,
        recorded_at_ms=execution.recorded_at_ms,
        receipt={
            "fill_id": execution.fill_id,
            "execution_id": execution.execution_id,
            "order_ref": execution.order_ref,
            "state": execution.state,
            "filled_at_ms": execution.filled_at_ms,
            "symbol": execution.symbol,
            "side": execution.side.value,
        },
    )


def _economic_orders(operation: ProjectedOperation) -> tuple[ProjectedOrder, ...]:
    """Return only orders allowed to contribute an operation's economics.

    EXIT operations retain entry-order links for custody/audit reconstruction,
    but their prices and fills must come solely from the newly submitted
    reducing order. Enter and other operation kinds retain their complete
    order set.
    """
    if operation.kind.upper() != "EXIT":
        return operation.orders
    return tuple(order for order in operation.orders if order.role.upper() == "REDUCING")


def classify_transaction_origin(
    *,
    order_ref: str | None,
    strategy_instance_id: str | None,
    external_observation: bool = False,
) -> TransactionOrigin:
    """Classify the order only through the canonical ownership ladder.

    The service intentionally never uses prefix checks or string fragments to
    label an operator-facing row.  A foreign broker observation is explicitly
    supplied by the external-order fold; a failed ownership ladder without
    that observation remains ``unknown`` and must retain its safety hold.
    """
    strategy_namespaces = (
        frozenset({build_bot_order_namespace(strategy_instance_id)})
        if strategy_instance_id is not None
        else frozenset()
    )
    if _ownership_matches(order_ref, strategy_namespaces):
        return "strategy"
    if _ownership_matches(order_ref, frozenset({build_manual_order_namespace("operator")})):
        return "manual"
    return "external" if external_observation else "unknown"


def _ownership_matches(order_ref: str | None, allowed_namespaces: frozenset[str]) -> bool:
    if not allowed_namespaces:
        return False
    return classify_ownership(
        order_ref=order_ref,
        perm_id=None,
        exec_id=None,
        allowed_namespaces=allowed_namespaces,
        known_intent_ids=frozenset(),
        known_perm_ids=frozenset(),
        known_exec_ids=frozenset(),
    ) is not OwnershipRung.NONE


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
    return _all_window_summary(operations, record_count=len(operations))


def _all_window_summary(
    operations: list[ProjectedOperation] | tuple[ProjectedOperation, ...],
    *,
    record_count: int,
) -> ClerkCustodyWindowSummary:
    """Summarize strategy custody stages without assigning them to external rows."""
    return ClerkCustodyWindowSummary(
        record_count=record_count,
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
