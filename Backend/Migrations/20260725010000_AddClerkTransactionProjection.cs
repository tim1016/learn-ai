using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <summary>Dedicated Clerk-journal transaction read model (#1219).</summary>
    [DbContext(typeof(AppDbContext))]
    [Migration("20260725010000_AddClerkTransactionProjection")]
    public partial class AddClerkTransactionProjection : Migration
    {
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                CREATE TABLE clerk_transactions (
                    id bigserial PRIMARY KEY,
                    transaction_id character varying(96) NOT NULL,
                    account_id character varying(64) NOT NULL,
                    journal_seq bigint NOT NULL,
                    recorded_at_ms bigint NOT NULL,
                    transaction_kind character varying(64) NOT NULL,
                    strategy_instance_id character varying(128) NOT NULL,
                    run_id character varying(128) NOT NULL,
                    intent_id character varying(256) NOT NULL,
                    order_ref character varying(512) NOT NULL,
                    order_id bigint NULL,
                    perm_id bigint NULL,
                    exec_id character varying(256) NULL,
                    receipt jsonb NOT NULL,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL,
                    CONSTRAINT ck_clerk_transactions_recorded_at_ms CHECK (recorded_at_ms >= 0),
                    CONSTRAINT ck_clerk_transactions_journal_seq CHECK (journal_seq >= 1)
                );
                CREATE TABLE clerk_transaction_events (
                    id bigserial PRIMARY KEY,
                    event_id character varying(96) NOT NULL,
                    transaction_id character varying(96) NOT NULL,
                    account_id character varying(64) NOT NULL,
                    journal_seq bigint NOT NULL,
                    event_kind character varying(64) NOT NULL,
                    recorded_at_ms bigint NOT NULL,
                    receipt jsonb NOT NULL,
                    inserted_at_ms bigint NOT NULL,
                    CONSTRAINT ck_clerk_transaction_events_recorded_at_ms CHECK (recorded_at_ms >= 0),
                    CONSTRAINT ck_clerk_transaction_events_journal_seq CHECK (journal_seq >= 1)
                );
                CREATE TABLE clerk_transaction_projection_cursors (
                    account_id character varying(64) NOT NULL,
                    journal_path text NOT NULL,
                    last_byte_offset bigint NOT NULL,
                    last_journal_seq bigint NOT NULL,
                    updated_at_ms bigint NOT NULL,
                    CONSTRAINT pk_clerk_transaction_projection_cursors PRIMARY KEY (account_id, journal_path),
                    CONSTRAINT ck_clerk_transaction_projection_cursors_byte_offset CHECK (last_byte_offset >= 0),
                    CONSTRAINT ck_clerk_transaction_projection_cursors_journal_seq CHECK (last_journal_seq >= 0)
                );
                CREATE UNIQUE INDEX uq_clerk_transactions_transaction_id ON clerk_transactions (transaction_id);
                CREATE UNIQUE INDEX uq_clerk_transactions_account_journal_seq ON clerk_transactions (account_id, journal_seq);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_event_id ON clerk_transaction_events (event_id);
                CREATE UNIQUE INDEX uq_clerk_transaction_events_account_journal_seq ON clerk_transaction_events (account_id, journal_seq);
                CREATE INDEX ix_clerk_transaction_events_transaction_id ON clerk_transaction_events (transaction_id);
                CREATE INDEX ix_clerk_transactions_history ON clerk_transactions (account_id, recorded_at_ms DESC, journal_seq DESC, transaction_id DESC);
            ");
        }

        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP TABLE IF EXISTS clerk_transaction_projection_cursors;
                DROP TABLE IF EXISTS clerk_transaction_events;
                DROP TABLE IF EXISTS clerk_transactions;
            ");
        }
    }
}
