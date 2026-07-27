using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Bounded replay/rebuild evidence for the Clerk transaction projection (#1223).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725040000_AddClerkTransactionProjectionReplayStatus")]
    public partial class AddClerkTransactionProjectionReplayStatus : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transaction_feed_status
                    ADD COLUMN high_water_journal_seq bigint NULL,
                    ADD COLUMN lag_records bigint NULL,
                    ADD COLUMN lag_is_lower_bound boolean NOT NULL DEFAULT FALSE;
                ALTER TABLE clerk_transaction_feed_status
                    ADD CONSTRAINT ck_clerk_transaction_feed_status_high_water
                        CHECK (high_water_journal_seq IS NULL OR high_water_journal_seq >= 0),
                    ADD CONSTRAINT ck_clerk_transaction_feed_status_lag
                        CHECK (lag_records IS NULL OR lag_records >= 0);
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transaction_feed_status
                    DROP CONSTRAINT IF EXISTS ck_clerk_transaction_feed_status_lag,
                    DROP CONSTRAINT IF EXISTS ck_clerk_transaction_feed_status_high_water,
                    DROP COLUMN IF EXISTS lag_is_lower_bound,
                    DROP COLUMN IF EXISTS lag_records,
                    DROP COLUMN IF EXISTS high_water_journal_seq;
            ");
        }
    }
}
