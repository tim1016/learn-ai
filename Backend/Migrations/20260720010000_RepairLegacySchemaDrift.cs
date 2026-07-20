using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260720010000_RepairLegacySchemaDrift")]
    public partial class RepairLegacySchemaDrift : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Databases created with EnsureCreated() skipped the raw-SQL portions of
            // earlier migrations. This converges those legacy schemas to the audited
            // migration-chain schema without changing a fresh database's result.
            migrationBuilder.Sql(@"
                ALTER TABLE bot_lifecycle_events
                ADD COLUMN IF NOT EXISTS source_rank integer NOT NULL DEFAULT 0;
                ALTER TABLE bot_lifecycle_events
                ALTER COLUMN source_rank TYPE integer USING source_rank::integer;
                ALTER TABLE bot_lifecycle_events
                ALTER COLUMN source_rank SET NOT NULL;
                ALTER TABLE bot_lifecycle_events
                ALTER COLUMN source_rank DROP DEFAULT;

                ALTER TABLE account_lifecycle_events
                ADD COLUMN IF NOT EXISTS source_rank integer NOT NULL DEFAULT 0;
                ALTER TABLE account_lifecycle_events
                ALTER COLUMN source_rank TYPE integer USING source_rank::integer;
                ALTER TABLE account_lifecycle_events
                ALTER COLUMN source_rank SET NOT NULL;
                ALTER TABLE account_lifecycle_events
                ALTER COLUMN source_rank DROP DEFAULT;

                -- A matching object name does not prove the raw-SQL invariant is
                -- correct. Recreate these constraints so PostgreSQL validates the
                -- current data against the canonical definitions.
                ALTER TABLE bot_lifecycle_events
                DROP CONSTRAINT IF EXISTS ck_bot_lifecycle_events_source_rank_nonnegative;
                ALTER TABLE bot_lifecycle_events
                ADD CONSTRAINT ck_bot_lifecycle_events_source_rank_nonnegative
                CHECK (source_rank >= 0);

                ALTER TABLE account_lifecycle_events
                DROP CONSTRAINT IF EXISTS ck_account_lifecycle_events_source_rank_nonnegative;
                ALTER TABLE account_lifecycle_events
                ADD CONSTRAINT ck_account_lifecycle_events_source_rank_nonnegative
                CHECK (source_rank >= 0);

                DROP INDEX IF EXISTS ix_bot_lifecycle_events_timeline;
                CREATE INDEX ix_bot_lifecycle_events_timeline
                  ON bot_lifecycle_events (account_id, strategy_instance_id, run_id, ts_ms DESC, source_rank DESC, source_seq DESC);

                DROP INDEX IF EXISTS ix_account_lifecycle_events_timeline;
                CREATE INDEX ix_account_lifecycle_events_timeline
                  ON account_lifecycle_events (account_id, ts_ms DESC, source_rank DESC, source_seq DESC);

                -- Recreate functional and partial indexes for the same reason: a
                -- hand-created index with the canonical name can have the wrong key
                -- expression, uniqueness, or predicate.
                DROP INDEX IF EXISTS ix_strategyexecution_datapolicy_symbol;
                CREATE INDEX ix_strategyexecution_datapolicy_symbol
                  ON ""StrategyExecutions"" ((""DataPolicyJson""->>'symbol'));

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_artifact_kind_fields;
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
                DROP CONSTRAINT IF EXISTS ck_artifact_kind_enum;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_artifact_kind_enum CHECK (
                    ""ArtifactKind"" IN ('time_series_bars','factor_file','map_file','metadata')
                );

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_resolution_enum;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_resolution_enum CHECK (
                    ""Resolution"" IS NULL OR ""Resolution"" IN ('minute','hour','daily')
                );

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_data_type_enum;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_data_type_enum CHECK (
                    ""DataType"" IS NULL OR ""DataType"" IN ('trade','quote')
                );

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_price_adjustment_mode_enum;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_price_adjustment_mode_enum CHECK (
                    ""PriceAdjustmentMode"" IS NULL
                    OR ""PriceAdjustmentMode"" IN ('raw','polygon_split_adjusted','lean_adjusted')
                );

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_status_enum;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_status_enum CHECK (
                    ""Status"" IN ('fetching','complete','stale','failed')
                );

                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_raw_only_for_canonical_data_root;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata' OR ""PriceAdjustmentMode"" = 'raw'
                );

                ALTER TABLE ""DataLakeRuns""
                DROP CONSTRAINT IF EXISTS ck_data_lake_runs_run_type;
                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_run_type CHECK (
                    ""RunType"" IN ('python_lab','lean_lab')
                );

                ALTER TABLE ""DataLakeRuns""
                DROP CONSTRAINT IF EXISTS ck_data_lake_runs_ensure_data_status;
                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_ensure_data_status CHECK (
                    ""EnsureDataStatus"" IS NULL
                    OR ""EnsureDataStatus"" IN ('pending','complete','partial','failed')
                );

                ALTER TABLE ""DataLakeRuns""
                DROP CONSTRAINT IF EXISTS ck_data_lake_runs_engine_status;
                ALTER TABLE ""DataLakeRuns""
                ADD CONSTRAINT ck_data_lake_runs_engine_status CHECK (
                    ""EngineStatus"" IS NULL
                    OR ""EngineStatus"" IN ('not_started','running','complete','failed')
                );

                DROP INDEX IF EXISTS uq_data_lake_artifacts_minute_bars;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_minute_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""TradingDate"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" = 'minute';

                DROP INDEX IF EXISTS uq_data_lake_artifacts_aggregated_bars;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_aggregated_bars
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""Resolution"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" IN ('hour','daily');

                DROP INDEX IF EXISTS uq_data_lake_artifacts_corp_actions;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_corp_actions
                  ON ""DataLakeArtifacts"" (""Market"", ""Symbol"", ""ArtifactKind"",
                                             ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                DROP INDEX IF EXISTS uq_data_lake_artifacts_metadata;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_metadata
                  ON ""DataLakeArtifacts"" (""DataContractHash"")
                  WHERE ""ArtifactKind"" = 'metadata';

                DROP INDEX IF EXISTS ix_data_lake_artifacts_corp_action_lookup;
                CREATE INDEX ix_data_lake_artifacts_corp_action_lookup
                  ON ""DataLakeArtifacts"" (""Symbol"", ""ArtifactKind"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                DROP INDEX IF EXISTS ix_data_lake_artifacts_incomplete;
                CREATE INDEX ix_data_lake_artifacts_incomplete
                  ON ""DataLakeArtifacts"" (""Status"", ""LeaseExpiresAtMs"")
                  WHERE ""Status"" <> 'complete';

                DO $$
                DECLARE
                    greek_data_guard text;
                    has_greek_data boolean;
                BEGIN
                    SELECT CASE
                        WHEN count(*) = 0 THEN NULL
                        ELSE format(
                            'SELECT EXISTS (SELECT 1 FROM %I WHERE %s)',
                            'PortfolioSnapshots',
                            string_agg(format('%I IS NOT NULL', column_name), ' OR '))
                    END
                    INTO greek_data_guard
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'PortfolioSnapshots'
                      AND column_name IN ('NetDelta', 'NetGamma', 'NetTheta', 'NetVega');

                    IF greek_data_guard IS NOT NULL THEN
                        EXECUTE greek_data_guard INTO has_greek_data;

                        IF has_greek_data THEN
                            RAISE EXCEPTION 'PortfolioSnapshots contains non-null Greek data; aborting destructive column drop';
                        END IF;
                    END IF;
                END $$;

                ALTER TABLE ""PortfolioSnapshots"" DROP COLUMN IF EXISTS ""NetDelta"";
                ALTER TABLE ""PortfolioSnapshots"" DROP COLUMN IF EXISTS ""NetGamma"";
                ALTER TABLE ""PortfolioSnapshots"" DROP COLUMN IF EXISTS ""NetTheta"";
                ALTER TABLE ""PortfolioSnapshots"" DROP COLUMN IF EXISTS ""NetVega"";
            ");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            throw new InvalidOperationException(
                "RepairLegacySchemaDrift is intentionally irreversible. Restore the pre-adoption database backup to roll it back.");
        }
    }
}
