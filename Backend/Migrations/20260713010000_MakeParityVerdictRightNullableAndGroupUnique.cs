using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260713010000_MakeParityVerdictRightNullableAndGroupUnique")]
    public partial class MakeParityVerdictRightNullableAndGroupUnique : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AlterColumn<int>(
                name: "RightExecutionId",
                table: "ParityVerdicts",
                type: "integer",
                nullable: true,
                oldClrType: typeof(int),
                oldType: "integer");

            migrationBuilder.DropIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts");

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts",
                column: "ParityGroupId",
                unique: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            // This migration is intentionally irreversible. Rows with NULL RightExecutionId
            // cannot be safely restored to NOT NULL without violating the FK constraint.
            // Delete all rows with NULL RightExecutionId before attempting a rollback.
            throw new InvalidOperationException(
                "MakeParityVerdictRightNullableAndGroupUnique is not reversible. " +
                "Delete rows with NULL RightExecutionId before reverting this migration.");
        }
    }
}
