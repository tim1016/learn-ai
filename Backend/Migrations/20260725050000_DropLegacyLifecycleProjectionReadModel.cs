using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Retires the superseded lifecycle projection read model (#1224).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725050000_DropLegacyLifecycleProjectionReadModel")]
    public partial class DropLegacyLifecycleProjectionReadModel : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP TABLE IF EXISTS account_owner_status_snapshots;
                DROP TABLE IF EXISTS lifecycle_node_receipts;
                DROP TABLE IF EXISTS operator_gate_snapshots;
                DROP TABLE IF EXISTS account_lifecycle_events;
                DROP TABLE IF EXISTS bot_lifecycle_events;
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            throw new NotSupportedException(
                "Restoring the retired lifecycle projection requires an audited backup restore; EF rollback cannot recreate derived evidence.");
        }
    }
}
