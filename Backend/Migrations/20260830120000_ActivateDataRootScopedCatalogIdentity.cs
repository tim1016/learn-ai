using System;
using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260830120000_ActivateDataRootScopedCatalogIdentity")]
    public partial class ActivateDataRootScopedCatalogIdentity : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Activation step for the data_root_id redesign (#1878, PR B of
            // #1861). PR A (#1876, 20260829120000_AddDataRootIdToDataLakeArtifactsAndRuns)
            // added the column, backfilled every pre-existing row to the
            // deterministic legacy-root UUID, and left a temporary server
            // default plus the old mode-only uniqueness in place so nothing
            // broke mid-rollout. This migration flips both: uniqueness now
            // leads with DataRootId (so a second physical root can hold the
            // same symbol/date/mode identity without colliding with the
            // first), and every write path is required to supply
            // DataRootId explicitly (the temporary default is dropped).

            // Hot-path coverage lookup (AppDbContext.ConfigureDataLakeModels):
            // must lead with DataRootId too, or a root-filtered coverage
            // query (select_coverage_minute_bars, select_artifact_coverage,
            // select_symbol_coverage_spans) falls back to a full scan
            // filtered post-hoc instead of an index-backed one.
            migrationBuilder.DropIndex(
                name: "IX_DataLakeArtifacts_Market_Symbol_Resolution_DataType_Trading~",
                table: "DataLakeArtifacts");

            migrationBuilder.CreateIndex(
                name: "ix_data_lake_artifacts_root_scoped_coverage",
                table: "DataLakeArtifacts",
                columns: new[] { "DataRootId", "Market", "Symbol", "Resolution", "DataType", "TradingDate" });

            // Partial unique indexes — rebuilt with DataRootId leading. Same
            // names, same WHERE predicates (partial-index/status semantics
            // preserved); only the column order changes. Every
            // catalog_client.py claim_* function's ON CONFLICT target is
            // updated in this same PR to match, so there is no window where
            // the application references a conflict target that no longer
            // exists.
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS uq_data_lake_artifacts_minute_bars;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_minute_bars
                  ON ""DataLakeArtifacts"" (""DataRootId"", ""Market"", ""Symbol"", ""TradingDate"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" = 'minute';

                DROP INDEX IF EXISTS uq_data_lake_artifacts_aggregated_bars;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_aggregated_bars
                  ON ""DataLakeArtifacts"" (""DataRootId"", ""Market"", ""Symbol"", ""Resolution"",
                                             ""DataType"", ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" = 'time_series_bars'
                    AND ""Resolution"" IN ('hour','daily');

                DROP INDEX IF EXISTS uq_data_lake_artifacts_corp_actions;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_corp_actions
                  ON ""DataLakeArtifacts"" (""DataRootId"", ""Market"", ""Symbol"", ""ArtifactKind"",
                                             ""Provider"", ""PriceAdjustmentMode"")
                  WHERE ""ArtifactKind"" IN ('factor_file','map_file');

                DROP INDEX IF EXISTS uq_data_lake_artifacts_metadata;
                CREATE UNIQUE INDEX uq_data_lake_artifacts_metadata
                  ON ""DataLakeArtifacts"" (""DataRootId"", ""DataContractHash"")
                  WHERE ""ArtifactKind"" = 'metadata';

                -- v1's single-canonical-root constraint (20260521033222_AddDataLakeArtifactsAndRuns)
                -- and v1.5's minimal widening (20260827120000_AllowImportedNonRawAdjustmentModes)
                -- both assumed a single physical root, so a non-raw row's only
                -- protection against colliding with a differently-adjusted root
                -- was refusing it outright. Adjustment mode is now a physical path
                -- segment under a root-scoped identity (#1839, #1876), so the
                -- constraint no longer represents reality -- its own comment in
                -- the v1 migration anticipated exactly this removal.
                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_raw_only_for_canonical_data_root;
            ");

            // Temporary defaults dropped: every write path (catalog_client.py's
            // claim_* functions, cache_import.py) now supplies DataRootId
            // explicitly, so a caller that forgot would fail loudly (NOT NULL
            // violation) instead of silently landing on the legacy root.
            migrationBuilder.AlterColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeArtifacts",
                type: "uuid",
                nullable: false,
                oldClrType: typeof(Guid),
                oldType: "uuid",
                oldDefaultValue: Guid.Empty);

            migrationBuilder.AlterColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeRuns",
                type: "uuid",
                nullable: false,
                oldClrType: typeof(Guid),
                oldType: "uuid",
                oldDefaultValue: Guid.Empty);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            // Refuse to collapse the schema back to mode-only uniqueness if
            // doing so would silently choose one root's row over another's.
            // Each check groups by the OLD (mode-only) identity key and looks
            // for more than one distinct DataRootId sharing it -- exactly the
            // set of rows that would violate the narrower unique index this
            // rollback is about to recreate. No automatic destructive dedup:
            // an operator must make an explicit data decision (delete/merge
            // one root's rows, or restore from a backup) before this can roll
            // back. Runs first, inside the same migration transaction, so a
            // raised exception leaves the schema untouched.
            migrationBuilder.Sql(@"
                DO $$
                DECLARE
                    v_count integer;
                BEGIN
                    SELECT count(*) INTO v_count FROM (
                        SELECT 1
                          FROM ""DataLakeArtifacts""
                         WHERE ""ArtifactKind"" = 'time_series_bars' AND ""Resolution"" = 'minute'
                         GROUP BY ""Market"", ""Symbol"", ""TradingDate"", ""DataType"", ""Provider"", ""PriceAdjustmentMode""
                        HAVING COUNT(DISTINCT ""DataRootId"") > 1
                    ) AS conflicts;
                    IF v_count > 0 THEN
                        RAISE EXCEPTION 'ActivateDataRootScopedCatalogIdentity rollback refused: % '
                            'minute-bar identity/identities exist under more than one distinct '
                            'DataRootId. Rebuilding the mode-only unique index would collide. '
                            'Resolve the multi-root duplication explicitly (an operator data '
                            'decision, or a database restore) before rolling back.', v_count;
                    END IF;

                    SELECT count(*) INTO v_count FROM (
                        SELECT 1
                          FROM ""DataLakeArtifacts""
                         WHERE ""ArtifactKind"" = 'time_series_bars' AND ""Resolution"" IN ('hour', 'daily')
                         GROUP BY ""Market"", ""Symbol"", ""Resolution"", ""DataType"", ""Provider"", ""PriceAdjustmentMode""
                        HAVING COUNT(DISTINCT ""DataRootId"") > 1
                    ) AS conflicts;
                    IF v_count > 0 THEN
                        RAISE EXCEPTION 'ActivateDataRootScopedCatalogIdentity rollback refused: % '
                            'aggregated-bar identity/identities exist under more than one distinct '
                            'DataRootId. Rebuilding the mode-only unique index would collide. '
                            'Resolve the multi-root duplication explicitly before rolling back.', v_count;
                    END IF;

                    SELECT count(*) INTO v_count FROM (
                        SELECT 1
                          FROM ""DataLakeArtifacts""
                         WHERE ""ArtifactKind"" IN ('factor_file', 'map_file')
                         GROUP BY ""Market"", ""Symbol"", ""ArtifactKind"", ""Provider"", ""PriceAdjustmentMode""
                        HAVING COUNT(DISTINCT ""DataRootId"") > 1
                    ) AS conflicts;
                    IF v_count > 0 THEN
                        RAISE EXCEPTION 'ActivateDataRootScopedCatalogIdentity rollback refused: % '
                            'corporate-action identity/identities exist under more than one distinct '
                            'DataRootId. Rebuilding the mode-only unique index would collide. '
                            'Resolve the multi-root duplication explicitly before rolling back.', v_count;
                    END IF;

                    SELECT count(*) INTO v_count FROM (
                        SELECT 1
                          FROM ""DataLakeArtifacts""
                         WHERE ""ArtifactKind"" = 'metadata'
                         GROUP BY ""DataContractHash""
                        HAVING COUNT(DISTINCT ""DataRootId"") > 1
                    ) AS conflicts;
                    IF v_count > 0 THEN
                        RAISE EXCEPTION 'ActivateDataRootScopedCatalogIdentity rollback refused: % '
                            'metadata identity/identities exist under more than one distinct '
                            'DataRootId. Rebuilding the mode-only unique index would collide. '
                            'Resolve the multi-root duplication explicitly before rolling back.', v_count;
                    END IF;
                END $$;
            ");

            migrationBuilder.AlterColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeArtifacts",
                type: "uuid",
                nullable: false,
                defaultValue: Guid.Empty,
                oldClrType: typeof(Guid),
                oldType: "uuid");

            migrationBuilder.AlterColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeRuns",
                type: "uuid",
                nullable: false,
                defaultValue: Guid.Empty,
                oldClrType: typeof(Guid),
                oldType: "uuid");

            migrationBuilder.Sql(@"
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata'
                    OR ""PriceAdjustmentMode"" IN ('raw', 'polygon_split_adjusted')
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
            ");

            migrationBuilder.DropIndex(
                name: "ix_data_lake_artifacts_root_scoped_coverage",
                table: "DataLakeArtifacts");

            migrationBuilder.CreateIndex(
                name: "IX_DataLakeArtifacts_Market_Symbol_Resolution_DataType_Trading~",
                table: "DataLakeArtifacts",
                columns: new[] { "Market", "Symbol", "Resolution", "DataType", "TradingDate" });
        }
    }
}
