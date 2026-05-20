using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    public partial class AddDataPolicyAndCommissionToStrategyExecution : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.AddColumn<string>(
                name: "BrokeragePolicy",
                table: "StrategyExecutions",
                type: "varchar(40)",
                maxLength: 40,
                nullable: true);

            migrationBuilder.AddColumn<decimal>(
                name: "CommissionPerOrder",
                table: "StrategyExecutions",
                type: "numeric(18,8)",
                nullable: true);

            migrationBuilder.AddColumn<string>(
                name: "DataPolicyJson",
                table: "StrategyExecutions",
                type: "jsonb",
                nullable: true);

            // Functional index on the DataPolicy symbol so the compare-view
            // and history filters can scan by symbol without hydrating the
            // full JSON blob. ``IF NOT EXISTS`` keeps the migration idempotent
            // against environments that hand-applied the index ahead of EF.
            migrationBuilder.Sql(
                @"CREATE INDEX IF NOT EXISTS ix_strategyexecution_datapolicy_symbol
                  ON ""StrategyExecutions"" ((""DataPolicyJson""->>'symbol'));");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql("DROP INDEX IF EXISTS ix_strategyexecution_datapolicy_symbol;");

            migrationBuilder.DropColumn(
                name: "BrokeragePolicy",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "CommissionPerOrder",
                table: "StrategyExecutions");

            migrationBuilder.DropColumn(
                name: "DataPolicyJson",
                table: "StrategyExecutions");
        }
    }
}
