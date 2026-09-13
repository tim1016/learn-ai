using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddDataLabSessionMsUtcColumns : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            // Additive only (PRD data-lab-workspace-redesign §13 steps 1–2):
            // nullable int64 ms UTC columns; legacy CreatedAt/UpdatedAt/
            // FromDate/ToDate remain untouched for dual-read compatibility.
            migrationBuilder.AddColumn<long>(
                name: "CreatedMsUtc",
                table: "DataLabSessions",
                type: "bigint",
                nullable: true);

            migrationBuilder.AddColumn<long>(
                name: "UpdatedMsUtc",
                table: "DataLabSessions",
                type: "bigint",
                nullable: true);

            migrationBuilder.AddColumn<long>(
                name: "WindowEndMsUtc",
                table: "DataLabSessions",
                type: "bigint",
                nullable: true);

            migrationBuilder.AddColumn<long>(
                name: "WindowStartMsUtc",
                table: "DataLabSessions",
                type: "bigint",
                nullable: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "CreatedMsUtc",
                table: "DataLabSessions");

            migrationBuilder.DropColumn(
                name: "UpdatedMsUtc",
                table: "DataLabSessions");

            migrationBuilder.DropColumn(
                name: "WindowEndMsUtc",
                table: "DataLabSessions");

            migrationBuilder.DropColumn(
                name: "WindowStartMsUtc",
                table: "DataLabSessions");
        }
    }
}
