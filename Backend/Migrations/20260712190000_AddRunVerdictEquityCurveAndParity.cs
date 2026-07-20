using System;
using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260712190000_AddRunVerdictEquityCurveAndParity")]
    public partial class AddRunVerdictEquityCurveAndParity : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<string>(
                name: "EquityCurveJson",
                table: "StrategyExecutions",
                type: "jsonb",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "InsightSummaryJson",
                table: "StrategyExecutions",
                type: "jsonb",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "ParityGroupId",
                table: "StrategyExecutions",
                type: "varchar(64)",
                maxLength: 64,
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "RunVerdictJson",
                table: "StrategyExecutions",
                type: "jsonb",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "VerdictGrade",
                table: "StrategyExecutions",
                type: "varchar(4)",
                maxLength: 4,
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "VerdictSignal",
                table: "StrategyExecutions",
                type: "varchar(16)",
                maxLength: 16,
                nullable: true);

            migrationBuilder.AddColumn<int>(
                name: "VerdictVersion",
                table: "StrategyExecutions",
                type: "integer",
                nullable: true);

            migrationBuilder.CreateTable(
                name: "ParityVerdicts",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    LeftExecutionId = table.Column<int>(type: "integer", nullable: false),
                    RightExecutionId = table.Column<int>(type: "integer", nullable: false),
                    ParityGroupId = table.Column<string>(type: "varchar(64)", maxLength: 64, nullable: true),
                    VerdictVersion = table.Column<int>(type: "integer", nullable: false),
                    Status = table.Column<string>(type: "varchar(16)", maxLength: 16, nullable: false),
                    VerdictJson = table.Column<string>(type: "jsonb", nullable: false),
                    CreatedAtUtc = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
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
                name: "IX_StrategyExecutions_ParityGroupId",
                table: "StrategyExecutions",
                column: "ParityGroupId");

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_LeftExecutionId_RightExecutionId",
                table: "ParityVerdicts",
                columns: new[] { "LeftExecutionId", "RightExecutionId" },
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts",
                column: "ParityGroupId");

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_RightExecutionId",
                table: "ParityVerdicts",
                column: "RightExecutionId");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "ParityVerdicts");

            migrationBuilder.DropIndex(
                name: "IX_StrategyExecutions_ParityGroupId",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "EquityCurveJson",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "InsightSummaryJson",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "ParityGroupId",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "RunVerdictJson",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "VerdictGrade",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "VerdictSignal",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "VerdictVersion",
                table: "StrategyExecutions");
        }
    }
}
