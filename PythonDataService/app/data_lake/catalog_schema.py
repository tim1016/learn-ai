"""Typed declaration of the Postgres schema Python expects.

The drift integration test (tests/integration/data_lake/test_schema_drift.py)
introspects the live database via pg_catalog and asserts equality against
the expectations below. If the EF Core migration changes a column or
constraint, this file must be updated in the same PR or CI will fail.

Authority for the schema itself:
docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 3
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnExpectation:
    name: str
    pg_type: str  # canonical type string from pg_catalog (e.g. 'text', 'bigint', 'jsonb')
    nullable: bool


@dataclass(frozen=True)
class TableExpectation:
    name: str
    columns: tuple[ColumnExpectation, ...]
    primary_key: tuple[str, ...]
    # Partial unique indexes and CHECK constraint names (full SQL not asserted; only names).
    partial_unique_indexes: tuple[str, ...] = field(default_factory=tuple)
    check_constraints: tuple[str, ...] = field(default_factory=tuple)
    indexes: tuple[str, ...] = field(default_factory=tuple)


DATA_LAKE_ARTIFACTS = TableExpectation(
    name="DataLakeArtifacts",
    columns=(
        ColumnExpectation("Id", "bigint", nullable=False),
        ColumnExpectation("ArtifactKind", "character varying", nullable=False),
        ColumnExpectation("Market", "character varying", nullable=True),
        ColumnExpectation("Symbol", "character varying", nullable=True),
        ColumnExpectation("TradingDate", "date", nullable=True),
        ColumnExpectation("Resolution", "character varying", nullable=True),
        ColumnExpectation("DataType", "character varying", nullable=True),
        ColumnExpectation("Provider", "character varying", nullable=False),
        ColumnExpectation("ProviderParams", "jsonb", nullable=False),
        ColumnExpectation("PriceAdjustmentMode", "character varying", nullable=True),
        ColumnExpectation("DataContractHash", "character", nullable=False),
        ColumnExpectation("RowCount", "integer", nullable=True),
        ColumnExpectation("FirstBarStartMs", "bigint", nullable=True),
        ColumnExpectation("LastBarStartMs", "bigint", nullable=True),
        ColumnExpectation("CorpActionRevision", "character", nullable=True),
        ColumnExpectation("FilePath", "text", nullable=False),
        ColumnExpectation("FileSizeBytes", "bigint", nullable=True),
        ColumnExpectation("FileSha256", "character", nullable=True),
        ColumnExpectation("Status", "character varying", nullable=False),
        ColumnExpectation("LeaseOwner", "character varying", nullable=True),
        ColumnExpectation("LeaseExpiresAtMs", "bigint", nullable=True),
        ColumnExpectation("AttemptCount", "integer", nullable=False),
        ColumnExpectation("LastError", "text", nullable=True),
        ColumnExpectation("ErrorMessage", "text", nullable=True),
        ColumnExpectation("FetchedAtMs", "bigint", nullable=False),
        ColumnExpectation("CompletedAtMs", "bigint", nullable=True),
    ),
    primary_key=("Id",),
    partial_unique_indexes=(
        "uq_data_lake_artifacts_minute_bars",
        "uq_data_lake_artifacts_aggregated_bars",
        "uq_data_lake_artifacts_corp_actions",
        "uq_data_lake_artifacts_metadata",
    ),
    check_constraints=(
        "ck_artifact_kind_fields",
        "ck_artifact_kind_enum",
        "ck_resolution_enum",
        "ck_data_type_enum",
        "ck_price_adjustment_mode_enum",
        "ck_status_enum",
        "ck_raw_only_for_canonical_data_root",
    ),
    indexes=(
        "ix_data_lake_artifacts_corp_action_lookup",
        "ix_data_lake_artifacts_incomplete",
    ),
)


DATA_LAKE_RUNS = TableExpectation(
    name="DataLakeRuns",
    columns=(
        ColumnExpectation("Id", "uuid", nullable=False),
        ColumnExpectation("StrategyExecutionId", "integer", nullable=True),
        ColumnExpectation("EngineRunId", "character varying", nullable=True),
        ColumnExpectation("RunType", "character varying", nullable=False),
        ColumnExpectation("RunSpec", "jsonb", nullable=False),
        ColumnExpectation("WorkspacePath", "text", nullable=True),
        ColumnExpectation("ManifestSha256", "character", nullable=True),
        ColumnExpectation("DataAvailabilityHash", "character", nullable=True),
        ColumnExpectation("EnsureDataStatus", "character varying", nullable=True),
        ColumnExpectation("EnsureDataResponse", "jsonb", nullable=True),
        ColumnExpectation("EngineStatus", "character varying", nullable=True),
        ColumnExpectation("RequestedAtMs", "bigint", nullable=False),
        ColumnExpectation("StartedAtMs", "bigint", nullable=True),
        ColumnExpectation("CompletedAtMs", "bigint", nullable=True),
    ),
    primary_key=("Id",),
    check_constraints=(
        "ck_data_lake_runs_run_type",
        "ck_data_lake_runs_ensure_data_status",
        "ck_data_lake_runs_engine_status",
    ),
)


ALL_TABLES: tuple[TableExpectation, ...] = (DATA_LAKE_ARTIFACTS, DATA_LAKE_RUNS)
