using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddUniqueIndexOnLeanRunId : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateIndex(
                name: "IX_StrategyExecutions_Source_LeanRunId",
                table: "StrategyExecutions",
                columns: new[] { "Source", "LeanRunId" },
                unique: true,
                filter: "\"LeanRunId\" IS NOT NULL");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropIndex(
                name: "IX_StrategyExecutions_Source_LeanRunId",
                table: "StrategyExecutions");
        }
    }
}
