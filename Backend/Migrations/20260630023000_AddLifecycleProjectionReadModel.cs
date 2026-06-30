using Backend.Data;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

#nullable disable

namespace Backend.Migrations
{
    /// <inheritdoc />
    [DbContext(typeof(AppDbContext))]
    [Migration("20260630023000_AddLifecycleProjectionReadModel")]
    public partial class AddLifecycleProjectionReadModel : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                CREATE TABLE bot_lifecycle_events (
                    id bigserial PRIMARY KEY,
                    account_id character varying(64) NOT NULL,
                    strategy_instance_id character varying(128) NULL,
                    run_id character varying(128) NULL,
                    event_id character varying(256) NOT NULL,
                    event_type character varying(80) NOT NULL,
                    category character varying(40) NOT NULL,
                    node_id character varying(80) NULL,
                    gate_id character varying(128) NULL,
                    status character varying(40) NULL,
                    severity character varying(20) NOT NULL,
                    ts_ms bigint NULL,
                    ts_ms_resolved boolean NOT NULL,
                    source_artifact text NOT NULL,
                    source_type character varying(80) NOT NULL,
                    source_rank integer NOT NULL,
                    source_seq bigint NULL,
                    source_offset bigint NULL,
                    source_hash character(64) NULL,
                    summary text NOT NULL,
                    why text NULL,
                    operator_next_step character varying(128) NULL,
                    receipt_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
                    rendered_headline text NULL,
                    rendered_template_id character varying(120) NULL,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL
                );

                CREATE TABLE account_lifecycle_events (
                    id bigserial PRIMARY KEY,
                    account_id character varying(64) NOT NULL,
                    strategy_instance_id character varying(128) NULL,
                    run_id character varying(128) NULL,
                    event_id character varying(256) NOT NULL,
                    event_type character varying(80) NOT NULL,
                    category character varying(40) NOT NULL,
                    node_id character varying(80) NULL,
                    gate_id character varying(128) NULL,
                    status character varying(40) NULL,
                    severity character varying(20) NOT NULL,
                    ts_ms bigint NULL,
                    ts_ms_resolved boolean NOT NULL,
                    source_artifact text NOT NULL,
                    source_type character varying(80) NOT NULL,
                    source_rank integer NOT NULL,
                    source_seq bigint NULL,
                    source_offset bigint NULL,
                    source_hash character(64) NULL,
                    summary text NOT NULL,
                    why text NULL,
                    operator_next_step character varying(128) NULL,
                    receipt_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
                    rendered_headline text NULL,
                    rendered_template_id character varying(120) NULL,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL
                );

                CREATE TABLE operator_gate_snapshots (
                    id bigserial PRIMARY KEY,
                    account_id character varying(64) NOT NULL,
                    strategy_instance_id character varying(128) NULL,
                    run_id character varying(128) NULL,
                    gate_id character varying(128) NOT NULL,
                    status character varying(40) NOT NULL,
                    severity character varying(20) NOT NULL,
                    ts_ms bigint NOT NULL,
                    ts_ms_resolved boolean NOT NULL,
                    source_artifact text NOT NULL,
                    source_seq bigint NULL,
                    source_offset bigint NULL,
                    source_hash character(64) NULL,
                    operator_reason text NOT NULL,
                    operator_next_step character varying(128) NULL,
                    receipt_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL
                );

                CREATE TABLE lifecycle_node_receipts (
                    id bigserial PRIMARY KEY,
                    account_id character varying(64) NOT NULL,
                    strategy_instance_id character varying(128) NULL,
                    run_id character varying(128) NULL,
                    node_id character varying(80) NOT NULL,
                    label character varying(120) NOT NULL,
                    value text NOT NULL,
                    unit character varying(40) NULL,
                    ts_ms bigint NULL,
                    ts_ms_resolved boolean NOT NULL,
                    source_artifact text NOT NULL,
                    source_seq bigint NULL,
                    source_offset bigint NULL,
                    source_hash character(64) NULL,
                    receipt_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL
                );

                CREATE TABLE account_owner_status_snapshots (
                    id bigserial PRIMARY KEY,
                    account_id character varying(64) NOT NULL,
                    generation integer NOT NULL,
                    phase character varying(20) NOT NULL,
                    recorded_at_ms bigint NOT NULL,
                    ts_ms_resolved boolean NOT NULL,
                    source_artifact text NOT NULL,
                    source_seq bigint NULL,
                    source_offset bigint NULL,
                    source_hash character(64) NULL,
                    receipt_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                    inserted_at_ms bigint NOT NULL,
                    updated_at_ms bigint NOT NULL
                );

                ALTER TABLE bot_lifecycle_events
                ADD CONSTRAINT ck_bot_lifecycle_events_status CHECK (
                    status IS NULL OR status IN ('passed','active','blocked','poison','freeze','inactive','unknown')
                );
                ALTER TABLE account_lifecycle_events
                ADD CONSTRAINT ck_account_lifecycle_events_status CHECK (
                    status IS NULL OR status IN ('passed','active','blocked','poison','freeze','inactive','unknown')
                );
                ALTER TABLE operator_gate_snapshots
                ADD CONSTRAINT ck_operator_gate_snapshots_status CHECK (
                    status IN ('pass','block','poison','freeze','unknown','not_applicable')
                );

                ALTER TABLE bot_lifecycle_events
                ADD CONSTRAINT ck_bot_lifecycle_events_severity CHECK (severity IN ('info','warning','critical'));
                ALTER TABLE account_lifecycle_events
                ADD CONSTRAINT ck_account_lifecycle_events_severity CHECK (severity IN ('info','warning','critical'));
                ALTER TABLE operator_gate_snapshots
                ADD CONSTRAINT ck_operator_gate_snapshots_severity CHECK (severity IN ('info','warning','critical'));
                ALTER TABLE bot_lifecycle_events
                ADD CONSTRAINT ck_bot_lifecycle_events_source_rank_nonnegative CHECK (source_rank >= 0);
                ALTER TABLE account_lifecycle_events
                ADD CONSTRAINT ck_account_lifecycle_events_source_rank_nonnegative CHECK (source_rank >= 0);

                ALTER TABLE bot_lifecycle_events
                ADD CONSTRAINT ck_bot_lifecycle_events_ts_resolution CHECK (
                    (ts_ms IS NULL AND ts_ms_resolved = false)
                    OR (ts_ms IS NOT NULL AND ts_ms_resolved = true)
                );
                ALTER TABLE account_lifecycle_events
                ADD CONSTRAINT ck_account_lifecycle_events_ts_resolution CHECK (
                    (ts_ms IS NULL AND ts_ms_resolved = false)
                    OR (ts_ms IS NOT NULL AND ts_ms_resolved = true)
                );
                ALTER TABLE lifecycle_node_receipts
                ADD CONSTRAINT ck_lifecycle_node_receipts_ts_resolution CHECK (
                    (ts_ms IS NULL AND ts_ms_resolved = false)
                    OR (ts_ms IS NOT NULL AND ts_ms_resolved = true)
                );
                ALTER TABLE operator_gate_snapshots
                ADD CONSTRAINT ck_operator_gate_snapshots_ts_resolution CHECK (ts_ms_resolved = true);
                ALTER TABLE account_owner_status_snapshots
                ADD CONSTRAINT ck_account_owner_status_snapshots_ts_resolution CHECK (ts_ms_resolved = true);
                ALTER TABLE account_owner_status_snapshots
                ADD CONSTRAINT ck_account_owner_status_snapshots_phase CHECK (
                    phase IN ('accepting','reconnecting','draining','frozen')
                );

                CREATE UNIQUE INDEX uq_bot_lifecycle_events_event_id
                  ON bot_lifecycle_events (event_id);
                CREATE UNIQUE INDEX uq_account_lifecycle_events_event_id
                  ON account_lifecycle_events (event_id);
                CREATE UNIQUE INDEX uq_bot_lifecycle_events_source_seq
                  ON bot_lifecycle_events (source_artifact, source_seq)
                  WHERE source_seq IS NOT NULL;
                CREATE UNIQUE INDEX uq_account_lifecycle_events_source_seq
                  ON account_lifecycle_events (source_artifact, source_seq)
                  WHERE source_seq IS NOT NULL;
                CREATE UNIQUE INDEX uq_operator_gate_snapshots_source_gate
                  ON operator_gate_snapshots (source_artifact, source_seq, gate_id)
                  WHERE source_seq IS NOT NULL;
                CREATE UNIQUE INDEX uq_lifecycle_node_receipts_source_node_label
                  ON lifecycle_node_receipts (source_artifact, source_seq, node_id, label)
                  WHERE source_seq IS NOT NULL;
                CREATE UNIQUE INDEX uq_account_owner_status_snapshots_generation
                  ON account_owner_status_snapshots (account_id, generation, phase, recorded_at_ms);

                CREATE INDEX ix_bot_lifecycle_events_timeline
                  ON bot_lifecycle_events (account_id, strategy_instance_id, run_id, ts_ms DESC, source_rank DESC, source_seq DESC);
                CREATE INDEX ix_account_lifecycle_events_timeline
                  ON account_lifecycle_events (account_id, ts_ms DESC, source_rank DESC, source_seq DESC);
                CREATE INDEX ix_bot_lifecycle_events_safety
                  ON bot_lifecycle_events (severity, status, account_id, strategy_instance_id)
                  WHERE severity IN ('warning','critical');
                CREATE INDEX ix_account_lifecycle_events_safety
                  ON account_lifecycle_events (severity, status, account_id)
                  WHERE severity IN ('warning','critical');
                CREATE INDEX ix_operator_gate_snapshots_gate
                  ON operator_gate_snapshots (account_id, gate_id, ts_ms DESC);
                CREATE INDEX ix_lifecycle_node_receipts_node
                  ON lifecycle_node_receipts (account_id, strategy_instance_id, run_id, node_id);
                CREATE INDEX ix_account_owner_status_snapshots_account
                  ON account_owner_status_snapshots (account_id, recorded_at_ms DESC);
            ");
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.Sql(@"
                DROP TABLE IF EXISTS account_owner_status_snapshots;
                DROP TABLE IF EXISTS lifecycle_node_receipts;
                DROP TABLE IF EXISTS operator_gate_snapshots;
                DROP TABLE IF EXISTS account_lifecycle_events;
                DROP TABLE IF EXISTS bot_lifecycle_events;
            ");
        }
    }
}
