using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations;

[DbContext(typeof(AppDbContext))]
[Migration("20260809020000_AddMetricDocumentationToStrategyExecution")]
public partial class AddMetricDocumentationToStrategyExecution : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.AddColumn<string>(
            name: "MetricDocumentationJson",
            table: "StrategyExecutions",
            type: "jsonb",
            nullable: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropColumn(
            name: "MetricDocumentationJson",
            table: "StrategyExecutions");
    }
}
