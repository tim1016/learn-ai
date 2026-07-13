using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
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
            migrationBuilder.DropIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts");

            migrationBuilder.CreateIndex(
                name: "IX_ParityVerdicts_ParityGroupId",
                table: "ParityVerdicts",
                column: "ParityGroupId");

            migrationBuilder.AlterColumn<int>(
                name: "RightExecutionId",
                table: "ParityVerdicts",
                type: "integer",
                nullable: false,
                defaultValue: 0,
                oldClrType: typeof(int),
                oldType: "integer",
                oldNullable: true);
        }
    }
}
