using System;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class DropBacktestRunTables : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "BacktestTrades");

            migrationBuilder.DropTable(
                name: "ParityVerdicts");

            migrationBuilder.DropTable(
                name: "StrategyExecutions");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "StrategyExecutions",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    TickerId = table.Column<int>(type: "integer", nullable: false),
                    Alpha = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    AnnualStandardDeviation = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Beta = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    BrokeragePolicy = table.Column<string>(type: "varchar(40)", maxLength: 40, nullable: true),
                    CommissionPerOrder = table.Column<decimal>(type: "numeric(18,8)", nullable: true),
                    CompoundingAnnualReturn = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    DataPolicyJson = table.Column<string>(type: "jsonb", nullable: true),
                    DrawdownRecoveryDays = table.Column<int>(type: "integer", nullable: false),
                    DurationMs = table.Column<long>(type: "bigint", nullable: false),
                    EndDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    EquityCurveJson = table.Column<string>(type: "jsonb", nullable: true),
                    ExecutedAt = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    FillMode = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    FinalEquity = table.Column<decimal>(type: "numeric(18,2)", precision: 18, scale: 2, nullable: false),
                    InformationRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    InitialCash = table.Column<decimal>(type: "numeric(18,2)", precision: 18, scale: 2, nullable: false),
                    InsightSummaryJson = table.Column<string>(type: "jsonb", nullable: true),
                    LeanAnalysisJson = table.Column<string>(type: "jsonb", nullable: true),
                    LeanRunId = table.Column<string>(type: "character varying(128)", maxLength: 128, nullable: true),
                    LeanStatisticsJson = table.Column<string>(type: "jsonb", nullable: true),
                    LosingTrades = table.Column<int>(type: "integer", nullable: false),
                    MaxDrawdown = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    MetricDocumentationJson = table.Column<string>(type: "jsonb", nullable: true),
                    Multiplier = table.Column<int>(type: "integer", nullable: false),
                    Notes = table.Column<string>(type: "text", nullable: true),
                    Parameters = table.Column<string>(type: "text", nullable: false),
                    ParityGroupId = table.Column<string>(type: "varchar(64)", maxLength: 64, nullable: true),
                    ProbabilisticSharpeRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ProfitFactor = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    RequestedEngine = table.Column<string>(type: "varchar(12)", maxLength: 12, nullable: true),
                    RunVerdictJson = table.Column<string>(type: "jsonb", nullable: true),
                    SharpeRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    SortinoRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: true),
                    Source = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    StartDate = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    StrategyName = table.Column<string>(type: "character varying(100)", maxLength: 100, nullable: false),
                    Timespan = table.Column<string>(type: "character varying(20)", maxLength: 20, nullable: false),
                    TotalFees = table.Column<decimal>(type: "numeric(18,4)", precision: 18, scale: 4, nullable: false),
                    TotalPnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    TotalTrades = table.Column<int>(type: "integer", nullable: false),
                    TrackingError = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    TreynorRatio = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ValidationAnalyticsJson = table.Column<string>(type: "jsonb", nullable: true),
                    ValueAtRisk95 = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ValueAtRisk99 = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    VerdictGrade = table.Column<string>(type: "varchar(4)", maxLength: 4, nullable: true),
                    VerdictSignal = table.Column<string>(type: "varchar(16)", maxLength: 16, nullable: true),
                    VerdictVersion = table.Column<int>(type: "integer", nullable: true),
                    WinRate = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    WinningTrades = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyExecutions", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyExecutions_Tickers_TickerId",
                        column: x => x.TickerId,
                        principalTable: "Tickers",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "BacktestTrades",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false),
                    CumulativePnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    EntryPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    EntryTimestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ExitPrice = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    ExitTimestamp = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    IsSyntheticExit = table.Column<bool>(type: "boolean", nullable: false, defaultValue: false),
                    PnL = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    Quantity = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    SignalReason = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    TradeType = table.Column<string>(type: "character varying(10)", maxLength: 10, nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_BacktestTrades", x => x.Id);
                    table.ForeignKey(
                        name: "FK_BacktestTrades_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "ParityVerdicts",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    LeftExecutionId = table.Column<int>(type: "integer", nullable: false),
                    RightExecutionId = table.Column<int>(type: "integer", nullable: true),
                    CreatedAtUtc = table.Column<DateTime>(type: "timestamp with time zone", nullable: false),
                    ParityGroupId = table.Column<string>(type: "varchar(64)", maxLength: 64, nullable: true),
                    Status = table.Column<string>(type: "varchar(16)", maxLength: 16, nullable: false),
                    VerdictJson = table.Column<string>(type: "jsonb", nullable: false),
                    VerdictVersion = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_ParityVerdicts", x => x.Id);
                    table.ForeignKey(
                        name: "FK_ParityVerdicts_StrategyExecutions_LeftExecutionId",
                        column: x => x.LeftExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_ParityVerdicts_StrategyExecutions_RightExecutionId",
                        column: x => x.RightExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_BacktestTrades_StrategyExecutionId",
                table: "BacktestTrades",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_LeftExecutionId_RightExecutionId",
                table: "ParityVerdicts",
                columns: new[] { "LeftExecutionId", "RightExecutionId" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts",
                column: "ParityGroupId",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_RightExecutionId",
                table: "ParityVerdicts",
                column: "RightExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_ExecutedAt",
                table: "StrategyExecutions",
                column: "ExecutedAt");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_ParityGroupId",
                table: "StrategyExecutions",
                column: "ParityGroupId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_Source",
                table: "StrategyExecutions",
                column: "Source");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_Source_LeanRunId",
                table: "StrategyExecutions",
                columns: new[] { "Source", "LeanRunId" },
                unique: true,
                filter: "\"LeanRunId\" IS NOT NULL");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_TickerId_StrategyName",
                table: "StrategyExecutions",
                columns: new[] { "TickerId", "StrategyName" });
        }
    }
}
