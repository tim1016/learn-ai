using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class DropPortfolioSnapshotGreekColumns : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropColumn(
                name: "NetDelta",
                table: "PortfolioSnapshots");

            migrationBuilder.DropColumn(
                name: "NetGamma",
                table: "PortfolioSnapshots");

            migrationBuilder.DropColumn(
                name: "NetTheta",
                table: "PortfolioSnapshots");

            migrationBuilder.DropColumn(
                name: "NetVega",
                table: "PortfolioSnapshots");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<decimal>(
                name: "NetDelta",
                table: "PortfolioSnapshots",
                type: "numeric(18,8)",
                precision: 18,
                scale: 8,
                nullable: true);

            migrationBuilder.AddColumn<decimal>(
                name: "NetGamma",
                table: "PortfolioSnapshots",
                type: "numeric(18,8)",
                precision: 18,
                scale: 8,
                nullable: true);

            migrationBuilder.AddColumn<decimal>(
                name: "NetTheta",
                table: "PortfolioSnapshots",
                type: "numeric(18,8)",
                precision: 18,
                scale: 8,
                nullable: true);

            migrationBuilder.AddColumn<decimal>(
                name: "NetVega",
                table: "PortfolioSnapshots",
                type: "numeric(18,8)",
                precision: 18,
                scale: 8,
                nullable: true);
        }
    }
}
