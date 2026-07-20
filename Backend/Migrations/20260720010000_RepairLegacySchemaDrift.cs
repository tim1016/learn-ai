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
                ALTER COLUMN source_rank DROP DEFAULT;

                ALTER TABLE account_lifecycle_events
                ADD COLUMN IF NOT EXISTS source_rank integer NOT NULL DEFAULT 0;
                ALTER TABLE account_lifecycle_events
                ALTER COLUMN source_rank DROP DEFAULT;

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_bot_lifecycle_events_source_rank_nonnegative'
                          AND conrelid = 'bot_lifecycle_events'::regclass
                    ) THEN
                        ALTER TABLE bot_lifecycle_events
                        ADD CONSTRAINT ck_bot_lifecycle_events_source_rank_nonnegative
                        CHECK (source_rank >= 0);
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_account_lifecycle_events_source_rank_nonnegative'
                          AND conrelid = 'account_lifecycle_events'::regclass
                    ) THEN
                        ALTER TABLE account_lifecycle_events
                        ADD CONSTRAINT ck_account_lifecycle_events_source_rank_nonnegative
                        CHECK (source_rank >= 0);
                    END IF;
                END $$;

                DROP INDEX IF EXISTS ix_bot_lifecycle_events_timeline;
                CREATE INDEX ix_bot_lifecycle_events_timeline
                  ON bot_lifecycle_events (account_id, strategy_instance_id, run_id, ts_ms DESC, source_rank DESC, source_seq DESC);

                DROP INDEX IF EXISTS ix_account_lifecycle_events_timeline;
                CREATE INDEX ix_account_lifecycle_events_timeline
                  ON account_lifecycle_events (account_id, ts_ms DESC, source_rank DESC, source_seq DESC);

                CREATE INDEX IF NOT EXISTS ix_strategyexecution_datapolicy_symbol
                  ON ""StrategyExecutions"" ((""DataPolicyJson""->>'symbol'));

                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_artifact_kind_fields'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
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
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_artifact_kind_enum'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_artifact_kind_enum CHECK (
                            ""ArtifactKind"" IN ('time_series_bars','factor_file','map_file','metadata')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_resolution_enum'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_resolution_enum CHECK (
                            ""Resolution"" IS NULL OR ""Resolution"" IN ('minute','hour','daily')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_data_type_enum'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_data_type_enum CHECK (
                            ""DataType"" IS NULL OR ""DataType"" IN ('trade','quote')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_price_adjustment_mode_enum'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_price_adjustment_mode_enum CHECK (
                            ""PriceAdjustmentMode"" IS NULL
                            OR ""PriceAdjustmentMode"" IN ('raw','polygon_split_adjusted','lean_adjusted')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_status_enum'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_status_enum CHECK (
                            ""Status"" IN ('fetching','complete','stale','failed')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_raw_only_for_canonical_data_root'
                          AND conrelid = '""DataLakeArtifacts""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeArtifacts""
                        ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                            ""ArtifactKind"" = 'metadata' OR ""PriceAdjustmentMode"" = 'raw'
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_data_lake_runs_run_type'
                          AND conrelid = '""DataLakeRuns""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeRuns""
                        ADD CONSTRAINT ck_data_lake_runs_run_type CHECK (
                            ""RunType"" IN ('python_lab','lean_lab')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_data_lake_runs_ensure_data_status'
                          AND conrelid = '""DataLakeRuns""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeRuns""
                        ADD CONSTRAINT ck_data_lake_runs_ensure_data_status CHECK (
                            ""EnsureDataStatus"" IS NULL
                            OR ""EnsureDataStatus"" IN ('pending','complete','partial','failed')
                        );
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_data_lake_runs_engine_status'
                          AND conrelid = '""DataLakeRuns""'::regclass
                    ) THEN
                        ALTER TABLE ""DataLakeRuns""
                        ADD CONSTRAINT ck_data_lake_runs_engine_status CHECK (
                            ""EngineStatus"" IS NULL
                            OR ""EngineStatus"" IN ('not_started','running','complete','failed')
                        );
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
