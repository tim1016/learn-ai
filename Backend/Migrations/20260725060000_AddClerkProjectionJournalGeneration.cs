using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Separates replay cursors and sequence indexes after a Clerk journal replacement.</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725060000_AddClerkProjectionJournalGeneration")]
    public partial class AddClerkProjectionJournalGeneration : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                ALTER TABLE clerk_transactions
                    ADD COLUMN IF NOT EXISTS journal_generation character varying(128) NOT NULL DEFAULT '';
                ALTER TABLE clerk_transaction_events
                    ADD COLUMN IF NOT EXISTS journal_generation character varying(128) NOT NULL DEFAULT '';
                ALTER TABLE clerk_transaction_projection_cursors
                    ADD COLUMN IF NOT EXISTS journal_generation character varying(128) NOT NULL DEFAULT '';

                DROP INDEX IF EXISTS uq_clerk_transactions_broker_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_broker_account_journal_seq;
                CREATE UNIQUE INDEX uq_clerk_transactions_broker_account_journal_seq
                    ON clerk_transactions (broker, account_id, journal_generation, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_broker_account_journal_seq
                    ON clerk_transaction_events (broker, account_id, journal_generation, journal_seq);
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP INDEX IF EXISTS uq_clerk_transactions_broker_account_journal_seq;
                DROP INDEX IF EXISTS uq_clerk_transaction_events_broker_account_journal_seq;
                CREATE UNIQUE INDEX uq_clerk_transactions_broker_account_journal_seq
                    ON clerk_transactions (broker, account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_broker_account_journal_seq
                    ON clerk_transaction_events (broker, account_id, journal_seq);

                ALTER TABLE clerk_transaction_projection_cursors DROP COLUMN IF EXISTS journal_generation;
                ALTER TABLE clerk_transaction_events DROP COLUMN IF EXISTS journal_generation;
                ALTER TABLE clerk_transactions DROP COLUMN IF EXISTS journal_generation;
            ");
        }
    }
}
