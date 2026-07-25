using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>IBKR lifecycle fields for the rebuildable Clerk transaction projection (#1220).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725020000_AddClerkTransactionLifecycleProjection")]
    public partial class AddClerkTransactionLifecycleProjection : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transactions
                    ADD COLUMN lifecycle_state character varying(64) NOT NULL DEFAULT 'submitted',
                    ADD COLUMN commission_status character varying(16) NOT NULL DEFAULT 'unknown',
                    ADD COLUMN fee double precision NULL;
                ALTER TABLE clerk_transaction_events
                    ADD COLUMN callback_identity character varying(256) NOT NULL DEFAULT '',
                    ADD COLUMN lifecycle_state character varying(64) NOT NULL DEFAULT 'submitted',
                    ADD COLUMN commission_status character varying(16) NOT NULL DEFAULT 'unknown',
                    ADD COLUMN fee double precision NULL;
                CREATE UNIQUE INDEX uq_clerk_transaction_events_callback_identity
                    ON clerk_transaction_events (account_id, journal_seq, callback_identity);
                CREATE INDEX ix_clerk_transactions_order_ref
                    ON clerk_transactions (account_id, order_ref);
                CREATE TABLE clerk_transaction_feed_status (
                    account_id character varying(64) PRIMARY KEY,
                    state character varying(32) NOT NULL,
                    headline character varying(128) NOT NULL,
                    detail character varying(512) NOT NULL,
                    updated_at_ms bigint NOT NULL,
                    CONSTRAINT ck_clerk_transaction_feed_status_updated_at_ms CHECK (updated_at_ms >= 0)
                );
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP TABLE IF EXISTS clerk_transaction_feed_status;
                DROP INDEX IF EXISTS ix_clerk_transactions_order_ref;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_callback_identity;
                ALTER TABLE clerk_transaction_events DROP COLUMN IF EXISTS fee, DROP COLUMN IF EXISTS commission_status, DROP COLUMN IF EXISTS lifecycle_state, DROP COLUMN IF EXISTS callback_identity;
                ALTER TABLE clerk_transactions DROP COLUMN IF EXISTS fee, DROP COLUMN IF EXISTS commission_status, DROP COLUMN IF EXISTS lifecycle_state;
            ");
        }
    }
}
