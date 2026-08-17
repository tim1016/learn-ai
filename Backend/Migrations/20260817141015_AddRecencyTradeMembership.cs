using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddRecencyTradeMembership : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "RecencyTradeMemberships",
                columns: table => new
                {
                    Id = table.Column<int>(type: "integer", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    RecencyTradeId = table.Column<int>(type: "integer", nullable: false),
                    RecencyRunId = table.Column<int>(type: "integer", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecencyTradeMemberships", x => x.Id);
                    table.ForeignKey(
                        name: "FK_RecencyTradeMemberships_RecencyRuns_RecencyRunId",
                        column: x => x.RecencyRunId,
                        principalTable: "RecencyRuns",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_RecencyTradeMemberships_RecencyTrades_RecencyTradeId",
                        column: x => x.RecencyTradeId,
                        principalTable: "RecencyTrades",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_RecencyTradeMemberships_RecencyRunId",
                table: "RecencyTradeMemberships",
                column: "RecencyRunId");

            migrationBuilder.CreateIndex(
                name: "IX_RecencyTradeMemberships_RecencyTradeId_RecencyRunId",
                table: "RecencyTradeMemberships",
                columns: new[] { "RecencyTradeId", "RecencyRunId" },
                unique: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "RecencyTradeMemberships");
        }
    }
}
