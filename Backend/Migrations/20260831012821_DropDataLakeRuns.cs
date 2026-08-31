using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class DropDataLakeRuns : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "DataLakeRuns");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "DataLakeRuns",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: true),
                    CompletedAtMs = table.Column<long>(type: "bigint", nullable: true),
                    DataAvailabilityHash = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    DataRootId = table.Column<Guid>(type: "uuid", nullable: false),
                    EngineRunId = table.Column<string>(type: "character varying(128)", maxLength: 128, nullable: true),
                    EngineStatus = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    EnsureDataResponse = table.Column<string>(type: "jsonb", nullable: true),
                    EnsureDataStatus = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: true),
                    ManifestSha256 = table.Column<string>(type: "character(64)", fixedLength: true, maxLength: 64, nullable: true),
                    RequestedAtMs = table.Column<long>(type: "bigint", nullable: false),
                    RunSpec = table.Column<string>(type: "jsonb", nullable: false),
                    RunType = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    StartedAtMs = table.Column<long>(type: "bigint", nullable: true),
                    WorkspacePath = table.Column<string>(type: "text", nullable: true)
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
                name: "IX_DataLakeRuns_StrategyExecutionId",
                table: "DataLakeRuns",
                column: "StrategyExecutionId");

            // The three CHECK constraints were added by raw SQL (in
            // 20260521033222, restated by 20260720010000/20260720020000), so
            // the scaffolder does not know about them and CreateTable above
            // will not restore them. Restated here or a rollback would rebuild
            // the table with a quietly weaker schema than the one it dropped —
            // and the Python catalog mirror's drift test asserts these names.
            migrationBuilder.Sql(@"
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
    }
}
