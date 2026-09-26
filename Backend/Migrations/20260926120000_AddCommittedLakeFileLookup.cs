using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    [DbContext(typeof(AppDbContext))]
    [Migration("20260926120000_AddCommittedLakeFileLookup")]
    public partial class AddCommittedLakeFileLookup : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Readers verify each physical file against a committed receipt
            // (#2456). Identity indexes do not cover this path lookup.
            migrationBuilder.Sql(@"
                CREATE INDEX ix_data_lake_artifacts_committed_file
                  ON ""DataLakeArtifacts"" (""DataRootId"", ""PriceAdjustmentMode"", ""FilePath"")
                  WHERE ""Status"" = 'complete';
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql("DROP INDEX ix_data_lake_artifacts_committed_file;");
        }
    }
}
