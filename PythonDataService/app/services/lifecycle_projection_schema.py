"""Typed declaration of the lifecycle projection Postgres schema.

The projection tables are a rebuildable read model over canonical file
artifacts. EF migrations own the storage shape; Python owns all lifecycle
meaning and validates the live database shape through the shared schema drift
test.
"""

from __future__ import annotations

from app.data_lake.catalog_schema import ColumnExpectation, TableExpectation

_EVENT_COLUMNS = (
    ColumnExpectation("id", "bigint", nullable=False),
    ColumnExpectation("account_id", "character varying", nullable=False),
    ColumnExpectation("strategy_instance_id", "character varying", nullable=True),
    ColumnExpectation("run_id", "character varying", nullable=True),
    ColumnExpectation("event_id", "character varying", nullable=False),
    ColumnExpectation("event_type", "character varying", nullable=False),
    ColumnExpectation("category", "character varying", nullable=False),
    ColumnExpectation("node_id", "character varying", nullable=True),
    ColumnExpectation("gate_id", "character varying", nullable=True),
    ColumnExpectation("status", "character varying", nullable=True),
    ColumnExpectation("severity", "character varying", nullable=False),
    ColumnExpectation("ts_ms", "bigint", nullable=True),
    ColumnExpectation("ts_ms_resolved", "boolean", nullable=False),
    ColumnExpectation("source_artifact", "text", nullable=False),
    ColumnExpectation("source_type", "character varying", nullable=False),
    ColumnExpectation("source_seq", "bigint", nullable=True),
    ColumnExpectation("source_offset", "bigint", nullable=True),
    ColumnExpectation("source_hash", "character", nullable=True),
    ColumnExpectation("summary", "text", nullable=False),
    ColumnExpectation("why", "text", nullable=True),
    ColumnExpectation("operator_next_step", "character varying", nullable=True),
    ColumnExpectation("receipt_payload", "jsonb", nullable=False),
    ColumnExpectation("evidence_refs", "jsonb", nullable=False),
    ColumnExpectation("rendered_headline", "text", nullable=True),
    ColumnExpectation("rendered_template_id", "character varying", nullable=True),
    ColumnExpectation("inserted_at_ms", "bigint", nullable=False),
    ColumnExpectation("updated_at_ms", "bigint", nullable=False),
)


BOT_LIFECYCLE_EVENTS = TableExpectation(
    name="bot_lifecycle_events",
    columns=_EVENT_COLUMNS,
    primary_key=("id",),
    partial_unique_indexes=("uq_bot_lifecycle_events_source_seq",),
    check_constraints=(
        "ck_bot_lifecycle_events_status",
        "ck_bot_lifecycle_events_severity",
        "ck_bot_lifecycle_events_ts_resolution",
    ),
    indexes=(
        "uq_bot_lifecycle_events_event_id",
        "ix_bot_lifecycle_events_timeline",
        "ix_bot_lifecycle_events_safety",
    ),
)


ACCOUNT_LIFECYCLE_EVENTS = TableExpectation(
    name="account_lifecycle_events",
    columns=_EVENT_COLUMNS,
    primary_key=("id",),
    partial_unique_indexes=("uq_account_lifecycle_events_source_seq",),
    check_constraints=(
        "ck_account_lifecycle_events_status",
        "ck_account_lifecycle_events_severity",
        "ck_account_lifecycle_events_ts_resolution",
    ),
    indexes=(
        "uq_account_lifecycle_events_event_id",
        "ix_account_lifecycle_events_timeline",
        "ix_account_lifecycle_events_safety",
    ),
)


OPERATOR_GATE_SNAPSHOTS = TableExpectation(
    name="operator_gate_snapshots",
    columns=(
        ColumnExpectation("id", "bigint", nullable=False),
        ColumnExpectation("account_id", "character varying", nullable=False),
        ColumnExpectation("strategy_instance_id", "character varying", nullable=True),
        ColumnExpectation("run_id", "character varying", nullable=True),
        ColumnExpectation("gate_id", "character varying", nullable=False),
        ColumnExpectation("status", "character varying", nullable=False),
        ColumnExpectation("severity", "character varying", nullable=False),
        ColumnExpectation("ts_ms", "bigint", nullable=False),
        ColumnExpectation("ts_ms_resolved", "boolean", nullable=False),
        ColumnExpectation("source_artifact", "text", nullable=False),
        ColumnExpectation("source_seq", "bigint", nullable=True),
        ColumnExpectation("source_offset", "bigint", nullable=True),
        ColumnExpectation("source_hash", "character", nullable=True),
        ColumnExpectation("operator_reason", "text", nullable=False),
        ColumnExpectation("operator_next_step", "character varying", nullable=True),
        ColumnExpectation("receipt_payload", "jsonb", nullable=False),
        ColumnExpectation("inserted_at_ms", "bigint", nullable=False),
        ColumnExpectation("updated_at_ms", "bigint", nullable=False),
    ),
    primary_key=("id",),
    partial_unique_indexes=("uq_operator_gate_snapshots_source_gate",),
    check_constraints=(
        "ck_operator_gate_snapshots_status",
        "ck_operator_gate_snapshots_severity",
        "ck_operator_gate_snapshots_ts_resolution",
    ),
    indexes=("ix_operator_gate_snapshots_gate",),
)


LIFECYCLE_NODE_RECEIPTS = TableExpectation(
    name="lifecycle_node_receipts",
    columns=(
        ColumnExpectation("id", "bigint", nullable=False),
        ColumnExpectation("account_id", "character varying", nullable=False),
        ColumnExpectation("strategy_instance_id", "character varying", nullable=True),
        ColumnExpectation("run_id", "character varying", nullable=True),
        ColumnExpectation("node_id", "character varying", nullable=False),
        ColumnExpectation("label", "character varying", nullable=False),
        ColumnExpectation("value", "text", nullable=False),
        ColumnExpectation("unit", "character varying", nullable=True),
        ColumnExpectation("ts_ms", "bigint", nullable=True),
        ColumnExpectation("ts_ms_resolved", "boolean", nullable=False),
        ColumnExpectation("source_artifact", "text", nullable=False),
        ColumnExpectation("source_seq", "bigint", nullable=True),
        ColumnExpectation("source_offset", "bigint", nullable=True),
        ColumnExpectation("source_hash", "character", nullable=True),
        ColumnExpectation("receipt_payload", "jsonb", nullable=False),
        ColumnExpectation("inserted_at_ms", "bigint", nullable=False),
        ColumnExpectation("updated_at_ms", "bigint", nullable=False),
    ),
    primary_key=("id",),
    partial_unique_indexes=("uq_lifecycle_node_receipts_source_node_label",),
    check_constraints=("ck_lifecycle_node_receipts_ts_resolution",),
    indexes=("ix_lifecycle_node_receipts_node",),
)


ACCOUNT_OWNER_STATUS_SNAPSHOTS = TableExpectation(
    name="account_owner_status_snapshots",
    columns=(
        ColumnExpectation("id", "bigint", nullable=False),
        ColumnExpectation("account_id", "character varying", nullable=False),
        ColumnExpectation("generation", "integer", nullable=False),
        ColumnExpectation("phase", "character varying", nullable=False),
        ColumnExpectation("recorded_at_ms", "bigint", nullable=False),
        ColumnExpectation("ts_ms_resolved", "boolean", nullable=False),
        ColumnExpectation("source_artifact", "text", nullable=False),
        ColumnExpectation("source_seq", "bigint", nullable=True),
        ColumnExpectation("source_offset", "bigint", nullable=True),
        ColumnExpectation("source_hash", "character", nullable=True),
        ColumnExpectation("receipt_payload", "jsonb", nullable=False),
        ColumnExpectation("inserted_at_ms", "bigint", nullable=False),
        ColumnExpectation("updated_at_ms", "bigint", nullable=False),
    ),
    primary_key=("id",),
    check_constraints=(
        "ck_account_owner_status_snapshots_ts_resolution",
        "ck_account_owner_status_snapshots_phase",
    ),
    indexes=(
        "uq_account_owner_status_snapshots_generation",
        "ix_account_owner_status_snapshots_account",
    ),
)


ALL_TABLES: tuple[TableExpectation, ...] = (
    BOT_LIFECYCLE_EVENTS,
    ACCOUNT_LIFECYCLE_EVENTS,
    OPERATOR_GATE_SNAPSHOTS,
    LIFECYCLE_NODE_RECEIPTS,
    ACCOUNT_OWNER_STATUS_SNAPSHOTS,
)
