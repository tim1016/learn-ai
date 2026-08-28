using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260827120000_AllowImportedNonRawAdjustmentModes")]
    public partial class AllowImportedNonRawAdjustmentModes : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // v1's ck_raw_only_for_canonical_data_root assumed a single physical
            // data root, so every non-metadata row had to be 'raw' — there was no
            // way to represent a second price-adjustment lineage without a second
            // root (see the constraint's own comment in
            // 20260521033222_AddDataLakeArtifactsAndRuns: "Relaxed in v2 by adding
            // data_root_id and dropping this constraint").
            //
            // Issue #1832 (lean-cache import) is the first caller that needs to
            // catalog a genuinely non-raw artifact: the existing polygon-adjusted
            // lean-cache must be imported under its true adjustment mode, not
            // misreported as 'raw'. The full data_root_id redesign is out of
            // scope for that issue, so this migration takes the minimal step the
            // original comment anticipated: widen the constraint to also allow
            // 'polygon_split_adjusted'. 'lean_adjusted' (the enum's third member)
            // is deliberately left out — nothing produces it yet, so there is no
            // evidence it needs an exception.
            migrationBuilder.Sql(@"
                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_raw_only_for_canonical_data_root;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata'
                    OR ""PriceAdjustmentMode"" IN ('raw', 'polygon_split_adjusted')
                );
            ");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE ""DataLakeArtifacts""
                DROP CONSTRAINT IF EXISTS ck_raw_only_for_canonical_data_root;
                ALTER TABLE ""DataLakeArtifacts""
                ADD CONSTRAINT ck_raw_only_for_canonical_data_root CHECK (
                    ""ArtifactKind"" = 'metadata' OR ""PriceAdjustmentMode"" = 'raw'
                );
            ");
        }
    }
}
