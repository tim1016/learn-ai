using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Shared Alpaca and IBKR transaction history identity/evidence fields (#1222).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725030000_AddAlpacaTransactionProjectionParity")]
    public partial class AddAlpacaTransactionProjectionParity : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transactions
                    ADD COLUMN broker character varying(16) NOT NULL DEFAULT 'ibkr',
                    ADD COLUMN native_order_id character varying(256) NULL,
                    ADD COLUMN native_execution_id character varying(256) NULL;
                ALTER TABLE clerk_transaction_events
                    ADD COLUMN broker character varying(16) NOT NULL DEFAULT 'ibkr',
                    ADD COLUMN native_order_id character varying(256) NULL,
                    ADD COLUMN native_execution_id character varying(256) NULL;
                DROP INDEX IF EXISTS uq_clerk_transactions_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_callback_identity;
                CREATE UNIQUE INDEX uq_clerk_transactions_broker_account_journal_seq
                    ON clerk_transactions (broker, account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_broker_account_journal_seq
                    ON clerk_transaction_events (broker, account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_callback_identity
                    ON clerk_transaction_events (broker, account_id, callback_identity)
                    WHERE callback_identity <> '';
                DROP INDEX IF EXISTS ix_clerk_transactions_history;
                CREATE INDEX ix_clerk_transactions_history
                    ON clerk_transactions (account_id, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS uq_clerk_transactions_broker_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_broker_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_callback_identity;
                DROP INDEX IF EXISTS ix_clerk_transactions_history;
                ALTER TABLE clerk_transaction_events DROP COLUMN IF EXISTS native_execution_id, DROP COLUMN IF EXISTS native_order_id, DROP COLUMN IF EXISTS broker;
                ALTER TABLE clerk_transactions DROP COLUMN IF EXISTS native_execution_id, DROP COLUMN IF EXISTS native_order_id, DROP COLUMN IF EXISTS broker;
                CREATE UNIQUE INDEX uq_clerk_transactions_account_journal_seq ON clerk_transactions (account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_account_journal_seq ON clerk_transaction_events (account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_callback_identity
                    ON clerk_transaction_events (account_id, callback_identity)
                    WHERE callback_identity <> '';
                CREATE INDEX ix_clerk_transactions_history
                    ON clerk_transactions (account_id, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
            ");
        }
    }
}
