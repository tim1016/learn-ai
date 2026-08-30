using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260829120000_AddDataRootIdToDataLakeArtifactsAndRuns")]
    public partial class AddDataRootIdToDataLakeArtifactsAndRuns : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Additive/safe foundation for the data_root_id redesign (#1876,
            // PR A of #1861) — the v1 constraint comment in
            // 20260521033222_AddDataLakeArtifactsAndRuns anticipated exactly
            // this. A single ADD COLUMN ... DEFAULT ... NOT NULL is both the
            // backfill and the non-null constraint in one atomic statement:
            // every pre-existing row reads as the deterministic legacy-root
            // UUID (Guid.Empty) the moment this migration commits, and
            // Postgres 11+ computes that for existing rows without a table
            // rewrite (no separate UPDATE needed). The default is
            // deliberately kept — not dropped — in this migration: it is
            // "temporary" only in the sense that PR B removes it once every
            // write path supplies data_root_id explicitly; until then, any
            // row inserted by a caller that has not been updated yet still
            // lands on the legacy root instead of failing a NOT NULL check.
            // Existing partial unique indexes are untouched — multi-root
            // uniqueness is out of scope for this slice.
            migrationBuilder.AddColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeArtifacts",
                type: "uuid",
                nullable: false,
                defaultValue: Guid.Empty);

            migrationBuilder.AddColumn<Guid>(
                name: "DataRootId",
                table: "DataLakeRuns",
                type: "uuid",
                nullable: false,
                defaultValue: Guid.Empty);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "DataRootId",
                table: "DataLakeArtifacts");

            migrationBuilder.DropColumn(
                name: "DataRootId",
                table: "DataLakeRuns");
        }
    }
}
