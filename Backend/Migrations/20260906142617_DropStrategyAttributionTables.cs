using System;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class DropStrategyAttributionTables : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "StrategyAllocations");

            migrationBuilder.DropTable(
                name: "StrategyTradeLinks");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "StrategyAllocations",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    AccountId = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false),
                    CapitalAllocated = table.Column<decimal>(type: "numeric(18,8)", precision: 18, scale: 8, nullable: false),
                    EndDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: true),
                    StartDate = table.Column<DateTime>(type: "timestamp with time zone", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyAllocations", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyAllocations_Accounts_AccountId",
                        column: x => x.AccountId,
                        principalTable: "Accounts",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_StrategyAllocations_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateTable(
                name: "StrategyTradeLinks",
                columns: table => new
                {
                    Id = table.Column<Guid>(type: "uuid", nullable: false),
                    StrategyExecutionId = table.Column<int>(type: "integer", nullable: false),
                    TradeId = table.Column<Guid>(type: "uuid", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_StrategyTradeLinks", x => x.Id);
                    table.ForeignKey(
                        name: "FK_StrategyTradeLinks_PortfolioTrades_TradeId",
                        column: x => x.TradeId,
                        principalTable: "PortfolioTrades",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_StrategyTradeLinks_StrategyExecutions_StrategyExecutionId",
                        column: x => x.StrategyExecutionId,
                        principalTable: "StrategyExecutions",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_StrategyAllocations_AccountId_StrategyExecutionId",
                table: "StrategyAllocations",
                columns: new[] { "AccountId", "StrategyExecutionId" });

            migrationBuilder.CreateIndex(
                name: "IX_StrategyAllocations_StrategyExecutionId",
                table: "StrategyAllocations",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyTradeLinks_StrategyExecutionId",
                table: "StrategyTradeLinks",
                column: "StrategyExecutionId");

            migrationBuilder.CreateIndex(
                name: "IX_StrategyTradeLinks_TradeId",
                table: "StrategyTradeLinks",
                column: "TradeId");
        }
    }
}
