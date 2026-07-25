"""Typed expectation for the Clerk-native transaction projection schema."""

from __future__ import annotations

from app.data_lake.catalog_schema import ColumnExpectation, TableExpectation

CLERK_TRANSACTIONS = TableExpectation(
    name="clerk_transactions",
    columns=(
        ColumnExpectation("id", "bigint", False), ColumnExpectation("transaction_id", "character varying", False),
        ColumnExpectation("broker", "character varying", False), ColumnExpectation("account_id", "character varying", False), ColumnExpectation("journal_seq", "bigint", False),
        ColumnExpectation("recorded_at_ms", "bigint", False), ColumnExpectation("transaction_kind", "character varying", False),
        ColumnExpectation("strategy_instance_id", "character varying", False), ColumnExpectation("run_id", "character varying", False),
        ColumnExpectation("intent_id", "character varying", False), ColumnExpectation("order_ref", "character varying", False),
        ColumnExpectation("order_id", "bigint", True), ColumnExpectation("perm_id", "bigint", True),
        ColumnExpectation("exec_id", "character varying", True), ColumnExpectation("native_order_id", "character varying", True), ColumnExpectation("native_execution_id", "character varying", True), ColumnExpectation("lifecycle_state", "character varying", False),
        ColumnExpectation("commission_status", "character varying", False), ColumnExpectation("fee", "double precision", True), ColumnExpectation("receipt", "jsonb", False),
        ColumnExpectation("inserted_at_ms", "bigint", False), ColumnExpectation("updated_at_ms", "bigint", False),
    ), primary_key=("id",), partial_unique_indexes=(),
    check_constraints=("ck_clerk_transactions_recorded_at_ms", "ck_clerk_transactions_journal_seq"),
    indexes=("uq_clerk_transactions_transaction_id", "uq_clerk_transactions_broker_account_journal_seq", "ix_clerk_transactions_history", "ix_clerk_transactions_order_ref"),
)
CLERK_TRANSACTION_EVENTS = TableExpectation(
    name="clerk_transaction_events",
    columns=(
        ColumnExpectation("id", "bigint", False), ColumnExpectation("event_id", "character varying", False),
        ColumnExpectation("transaction_id", "character varying", False), ColumnExpectation("broker", "character varying", False), ColumnExpectation("account_id", "character varying", False),
        ColumnExpectation("journal_seq", "bigint", False), ColumnExpectation("event_kind", "character varying", False),
        ColumnExpectation("callback_identity", "character varying", False), ColumnExpectation("lifecycle_state", "character varying", False), ColumnExpectation("native_order_id", "character varying", True), ColumnExpectation("native_execution_id", "character varying", True),
        ColumnExpectation("commission_status", "character varying", False), ColumnExpectation("fee", "double precision", True), ColumnExpectation("recorded_at_ms", "bigint", False), ColumnExpectation("receipt", "jsonb", False),
        ColumnExpectation("inserted_at_ms", "bigint", False),
    ), primary_key=("id",), partial_unique_indexes=("uq_clerk_transaction_events_callback_identity",),
    check_constraints=("ck_clerk_transaction_events_recorded_at_ms", "ck_clerk_transaction_events_journal_seq"),
    indexes=(
        "uq_clerk_transaction_events_event_id", "uq_clerk_transaction_events_broker_account_journal_seq",
        "ix_clerk_transaction_events_transaction_id",
    ),
)
CLERK_TRANSACTION_PROJECTION_CURSORS = TableExpectation(
    name="clerk_transaction_projection_cursors",
    columns=(
        ColumnExpectation("account_id", "character varying", False), ColumnExpectation("journal_path", "text", False),
        ColumnExpectation("last_byte_offset", "bigint", False), ColumnExpectation("last_journal_seq", "bigint", False),
        ColumnExpectation("updated_at_ms", "bigint", False),
    ), primary_key=("account_id", "journal_path"), partial_unique_indexes=(),
    check_constraints=("ck_clerk_transaction_projection_cursors_byte_offset", "ck_clerk_transaction_projection_cursors_journal_seq"), indexes=(),
)
CLERK_TRANSACTION_FEED_STATUS = TableExpectation(
    name="clerk_transaction_feed_status",
    columns=(
        ColumnExpectation("account_id", "character varying", False), ColumnExpectation("state", "character varying", False),
        ColumnExpectation("headline", "character varying", False),
        ColumnExpectation("detail", "character varying", False), ColumnExpectation("updated_at_ms", "bigint", False),
        ColumnExpectation("high_water_journal_seq", "bigint", True), ColumnExpectation("lag_records", "bigint", True),
        ColumnExpectation("lag_is_lower_bound", "boolean", False),
    ), primary_key=("account_id",), partial_unique_indexes=(),
    check_constraints=(
        "ck_clerk_transaction_feed_status_updated_at_ms", "ck_clerk_transaction_feed_status_high_water",
        "ck_clerk_transaction_feed_status_lag",
    ), indexes=(),
)
ALL_TABLES = (CLERK_TRANSACTIONS, CLERK_TRANSACTION_EVENTS, CLERK_TRANSACTION_PROJECTION_CURSORS, CLERK_TRANSACTION_FEED_STATUS)
