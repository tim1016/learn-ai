using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260830130000_AddLeaseGenerationToDataLakeArtifacts")]
    public partial class AddLeaseGenerationToDataLakeArtifacts : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Fences the artifact lease against a zombie writer (issue #1888).
            // A single ADD COLUMN ... DEFAULT 1 NOT NULL both backfills every
            // pre-existing row (every row alive today is either 'complete' or
            // a settled 'failed'/'stale' state with no live lease, so seeding
            // generation 1 for all of them is correct -- there is no historical
            // claim/steal history to reconstruct) and applies the NOT NULL
            // constraint in one atomic statement, the same pattern
            // 20260829120000_AddDataRootIdToDataLakeArtifactsAndRuns used for
            // DataRootId. Every write path (catalog_client.py's claim_*,
            // steal_or_retry_minute_bar, refresh_complete_artifact) is updated
            // in this same PR to seed/increment it, and complete_artifact plus
            // the new confirm_lease_generation both gate on it.
            migrationBuilder.AddColumn<int>(
                name: "LeaseGeneration",
                table: "DataLakeArtifacts",
                type: "integer",
                nullable: false,
                defaultValue: 1);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "LeaseGeneration",
                table: "DataLakeArtifacts");
        }
    }
}
