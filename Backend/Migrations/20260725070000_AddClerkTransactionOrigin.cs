using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Typed canonical origin for Clerk transaction history (#1236).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725070000_AddClerkTransactionOrigin")]
    public partial class AddClerkTransactionOrigin : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transactions
                    ADD COLUMN transaction_origin character varying(32) NOT NULL DEFAULT 'manual',
                    ADD COLUMN order_instruction jsonb NOT NULL DEFAULT '{}'::jsonb,
                    ADD CONSTRAINT ck_clerk_transactions_origin CHECK (transaction_origin IN ('manual', 'strategy', 'recovery', 'emergency', 'shutdown', 'force_flat', 'other'));
                CREATE INDEX ix_clerk_transactions_origin_history
                    ON clerk_transactions (account_id, transaction_origin, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
                CREATE INDEX ix_clerk_transactions_lifecycle_history
                    ON clerk_transactions (account_id, lifecycle_state, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
                CREATE INDEX ix_clerk_transactions_strategy_history
                    ON clerk_transactions (account_id, strategy_instance_id, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
                CREATE INDEX ix_clerk_transactions_run_history
                    ON clerk_transactions (account_id, run_id, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS ix_clerk_transactions_origin_history;
                DROP INDEX IF EXISTS ix_clerk_transactions_lifecycle_history;
                DROP INDEX IF EXISTS ix_clerk_transactions_strategy_history;
                DROP INDEX IF EXISTS ix_clerk_transactions_run_history;
                ALTER TABLE clerk_transactions
                    DROP CONSTRAINT IF EXISTS ck_clerk_transactions_origin,
                    DROP COLUMN IF EXISTS order_instruction,
                    DROP COLUMN IF EXISTS transaction_origin;
            ");
        }
    }
}
