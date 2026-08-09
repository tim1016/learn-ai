using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations;

[DbContext(typeof(AppDbContext))]
[Migration("20260809010000_AddRequestedEngineToStrategyExecution")]
public partial class AddRequestedEngineToStrategyExecution : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "RequestedEngine",
            table: "StrategyExecutions",
            type: "varchar(12)",
            maxLength: 12,
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "RequestedEngine",
            table: "StrategyExecutions");
    }
}
