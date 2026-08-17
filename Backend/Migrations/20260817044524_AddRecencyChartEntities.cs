using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddRecencyChartEntities : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "RecencyLaunches",
                columns: table => new
                {
                    Id = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                    ConfigJson = table.Column<string>(type: "jsonb", nullable: false),
                    ExpectedRuns = table.Column<int>(type: "integer", nullable: false),
                    SucceededRuns = table.Column<int>(type: "integer", nullable: false),
                    FailedRuns = table.Column<int>(type: "integer", nullable: false),
                    Status = table.Column<string>(type: "varchar(16)", maxLength: 16, nullable: false),
                    CreatedAtMs = table.Column<long>(type: "bigint", nullable: false),
                    CompletedAtMs = table.Column<long>(type: "bigint", nullable: true),
                    DeletedAtMs = table.Column<long>(type: "bigint", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecencyLaunches", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "RecencyRuns",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    RecencyLaunchId = table.Column<string>(type: "character varying(64)", nullable: false),
                    StrategyKey = table.Column<string>(type: "character varying(64)", maxLength: 64, nullable: false),
                    Symbol = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    ParamsJson = table.Column<string>(type: "jsonb", nullable: false),
                    ParamsHash = table.Column<string>(type: "varchar(64)", maxLength: 64, nullable: false),
                    StudyId = table.Column<int>(type: "integer", nullable: true),
                    TotalPnl = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Sharpe = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    CreatedAtMs = table.Column<long>(type: "bigint", nullable: false),
                    DeletedAtMs = table.Column<long>(type: "bigint", nullable: true)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecencyRuns", x => x.Id);
                    table.ForeignKey(
                        name: "FK_RecencyRuns_RecencyLaunches_RecencyLaunchId",
                        column: x => x.RecencyLaunchId,
                        principalTable: "RecencyLaunches",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "RecencyTrades",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    RecencyRunId = table.Column<int>(type: "integer", nullable: false),
                    Fingerprint = table.Column<string>(type: "varchar(64)", maxLength: 64, nullable: false),
                    EntryMs = table.Column<long>(type: "bigint", nullable: false),
                    ExitMs = table.Column<long>(type: "bigint", nullable: false),
                    PnlPts = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    PnlPct = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Pnl = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    HoldingSessions = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecencyTrades", x => x.Id);
                    table.ForeignKey(
                        name: "FK_RecencyTrades_RecencyRuns_RecencyRunId",
                        column: x => x.RecencyRunId,
                        principalTable: "RecencyRuns",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_RecencyLaunches_DeletedAtMs",
                table: "RecencyLaunches",
                column: "DeletedAtMs");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyLaunches_Status",
                table: "RecencyLaunches",
                column: "Status");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyRuns_DeletedAtMs",
                table: "RecencyRuns",
                column: "DeletedAtMs");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyRuns_RecencyLaunchId",
                table: "RecencyRuns",
                column: "RecencyLaunchId");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyRuns_Symbol_StrategyKey",
                table: "RecencyRuns",
                columns: new[] { "Symbol", "StrategyKey" });

            migrationBuilder.CreateIndex(
                name: "IX_RecencyTrades_EntryMs",
                table: "RecencyTrades",
                column: "EntryMs");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyTrades_Fingerprint",
                table: "RecencyTrades",
                column: "Fingerprint",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_RecencyTrades_RecencyRunId",
                table: "RecencyTrades",
                column: "RecencyRunId");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "RecencyTrades");

            migrationBuilder.DropTable(
                name: "RecencyRuns");

            migrationBuilder.DropTable(
                name: "RecencyLaunches");
        }
    }
}
