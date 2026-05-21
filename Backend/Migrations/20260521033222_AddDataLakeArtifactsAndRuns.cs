using System;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddDataLakeArtifactsAndRuns : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "DataLakeArtifacts",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    ArtifactKind = table.Column<string>(type: "character varying(40)", maxLength: 40, nullable: false),
                    Market = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    Symbol = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    TradingDate = table.Column<DateOnly>(type: "date", nullable: true),
                    Resolution = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    DataType = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    Provider = table.Column<string>(type: "character varying(40)", maxLength: 40, nullable: false),
                    ProviderParams = table.Column<string>(type: "jsonb", nullable: false),
                    PriceAdjustmentMode = table.Column<string>(type: "character varying(40)", maxLength: 40, nullable: true),
                    DataContractHash = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: false),
                    RowCount = table.Column<int>(type: "integer", nullable: true),
                    FirstBarStartMs = table.Column<long>(type: "bigint", nullable: true),
                    LastBarStartMs = table.Column<long>(type: "bigint", nullable: true),
                    CorpActionRevision = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    FilePath = table.Column<string>(type: "text", nullable: false),
                    FileSizeBytes = table.Column<long>(type: "bigint", nullable: true),
                    FileSha256 = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    Status = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    LeaseOwner = table.Column<string>(type: "character varying(128)", maxLength: 128, nullable: true),
                    LeaseExpiresAtMs = table.Column<long>(type: "bigint", nullable: true),
                    AttemptCount = table.Column<int>(type: "integer", nullable: false, defaultValue: 0),
                    LastError = table.Column<string>(type: "text", nullable: true),
                    ErrorMessage = table.Column<string>(type: "text", nullable: true),
                    FetchedAtMs = table.Column<long>(type: "bigint", nullable: false),
                    CompletedAtMs = table.Column<long>(type: "bigint", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_DataLakeArtifacts", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "DataLakeRuns",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: true),
                    EngineRunId = table.Column<string>(type: "character varying(128)", maxLength: 128, nullable: true),
                    RunType = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    RunSpec = table.Column<string>(type: "jsonb", nullable: false),
                    WorkspacePath = table.Column<string>(type: "text", nullable: true),
                    ManifestSha256 = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    DataAvailabilityHash = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    EnsureDataStatus = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    EnsureDataResponse = table.Column<string>(type: "jsonb", nullable: true),
                    EngineStatus = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    RequestedAtMs = table.Column<long>(type: "bigint", nullable: false),
                    StartedAtMs = table.Column<long>(type: "bigint", nullable: true),
                    CompletedAtMs = table.Column<long>(type: "bigint", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_DataLakeRuns", x => x.Id);
                    table.ForeignKey(
                        name: "FK_DataLakeRuns_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.SetNull);
                });

            migrationBuilder.CreateIndex(
                name: "IX_DataLakeArtifacts_Market_Symbol_Resolution_DataType_Trading~",
                table: "DataLakeArtifacts",
                columns: new[] { "Market", "Symbol", "Resolution", "DataType", "TradingDate" });

            migrationBuilder.CreateIndex(
                name: "IX_DataLakeRuns_StrategyExecutionId",
                table: "DataLakeRuns",
                column: "StrategyExecutionId");

            // Partial unique indexes — one identity scheme per artifact kind.
            // EF Core has no fluent API for partial indexes; declared in raw SQL.
            migrationBuilder.Sql(@"
                CREATE UNIQUE INDEX uq_data_lake_artifacts_minute_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""TradingDate"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" = 'minute';

                CREATE UNIQUE INDEX uq_data_lake_artifacts_aggregated_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""Resolution"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" IN ('hour','daily');

                CREATE UNIQUE INDEX uq_data_lake_artifacts_corp_actions
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""ArtifactKind"",
                                             ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                CREATE UNIQUE INDEX uq_data_lake_artifacts_metadata
                  ON ""DataLakeArtifacts"" (""DataContractHash"")
                  WHERE ""ArtifactKind"" = 'metadata';

                CREATE INDEX ix_data_lake_artifacts_corp_action_lookup
                  ON ""DataLakeArtifacts"" (""Symbol"", ""ArtifactKind"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                CREATE INDEX ix_data_lake_artifacts_incomplete
                  ON ""DataLakeArtifacts"" (""Status"", ""LeaseExpiresAtMs"")
                  WHERE ""Status"" <> 'complete';
            ");

            // CHECK constraints — declared in raw SQL because EF's
            // ToTable(t => t.HasCheckConstraint(...)) is unwieldy for
            // multi-clause invariants like ours.
            migrationBuilder.Sql(@"
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_artifact_kind_fields CHECK (
                    (""ArtifactKind"" = 'time_series_bars'
                       AND ""Market"" IS NOT NULL AND ""Symbol"" IS NOT NULL
                       AND ""Resolution"" IS NOT NULL AND ""DataType"" IS NOT NULL
                       AND ""PriceAdjustmentMode"" IS NOT NULL
                       AND ((""Resolution"" = 'minute' AND ""TradingDate"" IS NOT NULL)
                            OR (""Resolution"" IN ('hour','daily') AND ""TradingDate"" IS NULL)))
                    OR (""ArtifactKind"" IN ('factor_file','map_file')
                       AND ""Market"" IS NOT NULL AND ""Symbol"" IS NOT NULL
                       AND ""PriceAdjustmentMode"" IS NOT NULL
                       AND ""TradingDate"" IS NULL AND ""Resolution"" IS NULL
                       AND ""DataType"" IS NULL)
                    OR (""ArtifactKind"" = 'metadata'
                       AND ""TradingDate"" IS NULL AND ""Resolution"" IS NULL
                       AND ""DataType"" IS NULL)
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_artifact_kind_enum CHECK (
                    ""ArtifactKind"" IN ('time_series_bars','factor_file','map_file','metadata')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_resolution_enum CHECK (
                    ""Resolution"" IS NULL OR ""Resolution"" IN ('minute','hour','daily')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_data_type_enum CHECK (
                    ""DataType"" IS NULL OR ""DataType"" IN ('trade','quote')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_price_adjustment_mode_enum CHECK (
                    ""PriceAdjustmentMode"" IS NULL
                    OR ""PriceAdjustmentMode"" IN ('raw','polygon_split_adjusted','lean_adjusted')
                );

                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_status_enum CHECK (
                    ""Status"" IN ('fetching','complete','stale','failed')
                );

                -- v1 only: single canonical root → raw bars only.
                -- Relaxed in v2 by adding data_root_id and dropping this constraint.
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata' OR ""PriceAdjustmentMode"" = 'raw'
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_run_type CHECK (
                    ""RunType"" IN ('python_lab','lean_lab')
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_ensure_data_status CHECK (
                    ""EnsureDataStatus"" IS NULL
                    OR ""EnsureDataStatus"" IN ('pending','complete','partial','failed')
                );

                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_engine_status CHECK (
                    ""EngineStatus"" IS NULL
                    OR ""EngineStatus"" IN ('not_started','running','complete','failed')
                );
            ");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS uq_data_lake_artifacts_minute_bars;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_aggregated_bars;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_corp_actions;
                DROP INDEX IF EXISTS uq_data_lake_artifacts_metadata;
                DROP INDEX IF EXISTS ix_data_lake_artifacts_corp_action_lookup;
                DROP INDEX IF EXISTS ix_data_lake_artifacts_incomplete;
            ");

            migrationBuilder.DropTable(
                name: "DataLakeRuns");

            migrationBuilder.DropTable(
                name: "DataLakeArtifacts");
        }
    }
}
