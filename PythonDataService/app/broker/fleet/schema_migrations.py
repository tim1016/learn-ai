"""The fleet registry's versioned migrations, split from the fresh schema.

One registered, additive-only upgrade per schema version, chained through
``SCHEMA_MIGRATIONS`` and applied inside one transaction by
``schema.migrate_schema``. The v1 shape is retained verbatim here so the
registered v1→v2 migration is testable against exactly the DDL it upgraded
rather than a reconstruction of it. This module is storage DDL only — the
rules about what a write *means* live in ``service.py``, and no Alpaca or
IBKR module is ever imported on a fleet path.
"""

from __future__ import annotations

from app.utils.session_anchors import MAX_TIMESTAMP_MS

# The v1 shape, retained verbatim so the registered v1→v2 migration is
# testable against exactly the DDL it upgrades rather than a reconstruction.
_SCHEMA_DDL_V1_TEMPLATE = """\
CREATE TABLE fleet_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    registry_id         TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);
CREATE TABLE clerks (
    clerk_id                TEXT PRIMARY KEY,
    broker                  TEXT NOT NULL CHECK (length(broker) > 0),
    worker_key              TEXT NOT NULL,
    display_label           TEXT NOT NULL CHECK (length(display_label) > 0),
    volume_id               TEXT NOT NULL,
    volume_root             TEXT NOT NULL CHECK (length(volume_root) > 0),
    volume_attestation_kind TEXT NOT NULL CHECK (length(volume_attestation_kind) > 0),
    volume_attestation_id   TEXT NOT NULL CHECK (length(volume_attestation_id) > 0),
    lifecycle_state         TEXT NOT NULL CHECK (lifecycle_state IN ('provisioned', 'draining', 'retired')),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    retired_at_ms INTEGER CHECK (retired_at_ms IS NULL OR (retired_at_ms >= 0 AND retired_at_ms <= MAX_TIMESTAMP_MS)),
    CHECK ((retired_at_ms IS NULL) = (lifecycle_state <> 'retired'))
);
CREATE UNIQUE INDEX ux_clerks_worker_key
    ON clerks(worker_key) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_id
    ON clerks(volume_id) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_attestation
    ON clerks(volume_attestation_kind, volume_attestation_id) WHERE lifecycle_state <> 'retired';
CREATE INDEX ix_clerks_listing ON clerks(broker, created_at_ms, clerk_id);
CREATE TABLE clerk_sessions (
    clerk_id                    TEXT PRIMARY KEY REFERENCES clerks(clerk_id),
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0 AND last_seen_at_ms <= MAX_TIMESTAMP_MS),
    reported_binding_generation INTEGER,
    reported_account_id         TEXT,
    reported_state              TEXT
);
CREATE TABLE clerk_session_history (
    clerk_id                    TEXT NOT NULL,
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    superseded_at_ms INTEGER NOT NULL CHECK (superseded_at_ms >= 0 AND superseded_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (clerk_id, routing_epoch)
);
CREATE TABLE account_assignments (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL CHECK (length(canonical_external_account_id) > 0),
    clerk_id                        TEXT NOT NULL REFERENCES clerks(clerk_id),
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (broker, canonical_external_account_id),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL))
);
CREATE INDEX ix_account_assignments_owner
    ON account_assignments(clerk_id) WHERE state <> 'released';
CREATE TABLE routing_receipts (
    correlation_id      TEXT PRIMARY KEY,
    broker              TEXT NOT NULL,
    clerk_id            TEXT NOT NULL,
    operation_kind      TEXT NOT NULL,
    nonsecret_target_ref TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('delivered', 'failed', 'outcome_unknown')),
    upstream_receipt_ref TEXT,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);
CREATE UNIQUE INDEX ux_routing_receipts_idempotency
    ON routing_receipts(broker, clerk_id, idempotency_key);
CREATE TRIGGER trg_clerks_identity_immutable
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.worker_key IS NOT NEW.worker_key
    OR OLD.volume_id IS NOT NEW.volume_id
    OR OLD.volume_root IS NOT NEW.volume_root
    OR OLD.volume_attestation_kind IS NOT NEW.volume_attestation_kind
    OR OLD.volume_attestation_id IS NOT NEW.volume_attestation_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a clerk identity is written once at provisioning and never changes');
END;
CREATE TRIGGER trg_clerks_no_delete
BEFORE DELETE ON clerks
BEGIN
    SELECT RAISE(ABORT, 'a clerk is retired, never deleted; IDs are not recycled');
END;
CREATE TRIGGER trg_clerks_lifecycle_forward
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'provisioned' AND OLD.retired_at_ms IS NOT NEW.retired_at_ms)
    OR (OLD.lifecycle_state = 'draining' AND NEW.lifecycle_state NOT IN ('draining', 'retired'))
    OR (OLD.lifecycle_state = 'retired' AND NEW.lifecycle_state <> 'retired')
BEGIN
    SELECT RAISE(ABORT, 'a clerk lifecycle moves forward only');
END;
CREATE TRIGGER trg_clerk_sessions_epoch_monotonic
BEFORE UPDATE ON clerk_sessions
FOR EACH ROW WHEN NEW.routing_epoch < OLD.routing_epoch
BEGIN
    SELECT RAISE(ABORT, 'a routing epoch never moves backwards');
END;
CREATE TRIGGER trg_clerk_sessions_no_delete
BEFORE DELETE ON clerk_sessions
BEGIN
    SELECT RAISE(ABORT, 'a superseded session is archived, not deleted');
END;
CREATE TRIGGER trg_clerk_session_history_immutable
BEFORE UPDATE ON clerk_session_history
BEGIN
    SELECT RAISE(ABORT, 'session history is append-only');
END;
CREATE TRIGGER trg_clerk_session_history_no_delete
BEFORE DELETE ON clerk_session_history
BEGIN
    SELECT RAISE(ABORT, 'session history is append-only');
END;
CREATE TRIGGER trg_account_assignments_generation_monotonic
BEFORE UPDATE ON account_assignments
FOR EACH ROW WHEN NEW.assignment_generation < OLD.assignment_generation
BEGIN
    SELECT RAISE(ABORT, 'an assignment generation never moves backwards');
END;
CREATE TRIGGER trg_account_assignments_no_delete
BEFORE DELETE ON account_assignments
BEGIN
    SELECT RAISE(ABORT, 'an assignment is released, never deleted');
END;
CREATE TRIGGER trg_routing_receipts_outcome_only
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN
    OLD.correlation_id IS NOT NEW.correlation_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.operation_kind IS NOT NEW.operation_kind
    OR OLD.nonsecret_target_ref IS NOT NEW.nonsecret_target_ref
    OR OLD.idempotency_key IS NOT NEW.idempotency_key
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt identity is immutable; only its outcome may move');
END;
CREATE TRIGGER trg_routing_receipts_no_delete
BEFORE DELETE ON routing_receipts
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt is never deleted');
END;
CREATE TABLE account_assignment_history (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL,
    clerk_id                        TEXT NOT NULL,
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    recorded_at_ms                  INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms                   INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (broker, canonical_external_account_id, assignment_generation, state)
);
CREATE TRIGGER trg_account_assignment_history_immutable
BEFORE UPDATE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END;
CREATE TRIGGER trg_account_assignment_history_no_delete
BEFORE DELETE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END;
"""

SCHEMA_DDL_V1 = _SCHEMA_DDL_V1_TEMPLATE.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))

# The v1→v2 upgrade (audit 2026-09-13, findings 1/4/7): additive columns on
# clerks, sessions and assignments; the approved-endpoint table; and a
# rebuild of routing_receipts whose v1 outcome CHECK cannot express the
# four-state attempt vocabulary. The rebuild copies every row — 'failed'
# maps to 'provider_refused' and pre-v2 receipts count as dispatched at
# their last update — so no history is lost and no receipt is deleted.
_MIGRATION_V1_TO_V2_TEMPLATE: tuple[str, ...] = (
    "ALTER TABLE clerks ADD COLUMN deployment_namespace TEXT NOT NULL DEFAULT 'host:local'",
    "ALTER TABLE clerk_sessions ADD COLUMN endpoint_ref TEXT",
    "ALTER TABLE clerk_sessions ADD COLUMN adapter_version TEXT",
    "ALTER TABLE clerk_sessions ADD COLUMN reported_summary_json TEXT",
    "ALTER TABLE account_assignments ADD COLUMN confirmed_binding_generation INTEGER",
    "ALTER TABLE account_assignments ADD COLUMN confirmed_profile_id TEXT",
    "ALTER TABLE account_assignments ADD COLUMN confirmed_revision INTEGER",
    (
        "ALTER TABLE account_assignments ADD COLUMN confirmed_at_ms INTEGER "
        "CHECK (confirmed_at_ms IS NULL OR (confirmed_at_ms >= 0 AND "
        "confirmed_at_ms <= MAX_TIMESTAMP_MS))"
    ),
    "ALTER TABLE account_assignments ADD COLUMN confirmed_agent_instance_id TEXT",
    (
        "ALTER TABLE account_assignments ADD COLUMN confirmed_routing_epoch INTEGER "
        "CHECK (confirmed_routing_epoch IS NULL OR confirmed_routing_epoch >= 1)"
    ),
    "DROP TRIGGER trg_clerks_identity_immutable",
    """CREATE TRIGGER trg_clerks_identity_immutable
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.worker_key IS NOT NEW.worker_key
    OR OLD.volume_id IS NOT NEW.volume_id
    OR OLD.volume_root IS NOT NEW.volume_root
    OR OLD.deployment_namespace IS NOT NEW.deployment_namespace
    OR OLD.volume_attestation_kind IS NOT NEW.volume_attestation_kind
    OR OLD.volume_attestation_id IS NOT NEW.volume_attestation_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a clerk identity is written once at provisioning and never changes');
END""",
    (
        "CREATE TABLE approved_endpoints (\n"
        "    endpoint_ref    TEXT PRIMARY KEY CHECK (length(endpoint_ref) > 0),\n"
        "    clerk_id        TEXT NOT NULL REFERENCES clerks(clerk_id),\n"
        "    base_url        TEXT NOT NULL CHECK (length(base_url) > 0),\n"
        "    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),\n"
        "    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)\n"
        ")"
    ),
    "CREATE UNIQUE INDEX ux_approved_endpoints_clerk ON approved_endpoints(clerk_id)",
    """CREATE TRIGGER trg_approved_endpoints_no_delete
BEFORE DELETE ON approved_endpoints
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint is superseded by re-approval, never deleted');
END""",
    """CREATE TRIGGER trg_approved_endpoints_identity_immutable
BEFORE UPDATE ON approved_endpoints
FOR EACH ROW WHEN
    OLD.endpoint_ref IS NOT NEW.endpoint_ref
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint belongs to its clerk and reference; re-approve the same reference with a new destination');
END""",
    """CREATE TABLE routing_receipts_v2 (
    correlation_id      TEXT PRIMARY KEY,
    broker              TEXT NOT NULL,
    clerk_id            TEXT NOT NULL,
    operation_kind      TEXT NOT NULL,
    nonsecret_target_ref TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('not_dispatched', 'provider_refused', 'delivered', 'outcome_unknown')),
    upstream_receipt_ref TEXT,
    pinned_routing_epoch        INTEGER CHECK (pinned_routing_epoch IS NULL OR pinned_routing_epoch >= 1),
    pinned_binding_generation   INTEGER,
    pinned_agent_instance_id    TEXT,
    dispatched_at_ms INTEGER CHECK (dispatched_at_ms IS NULL OR (dispatched_at_ms >= 0 AND dispatched_at_ms <= MAX_TIMESTAMP_MS)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
)""",
    """INSERT INTO routing_receipts_v2 (correlation_id, broker, clerk_id, operation_kind,
    nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref,
    pinned_routing_epoch, pinned_binding_generation, pinned_agent_instance_id,
    dispatched_at_ms, created_at_ms, updated_at_ms)
SELECT correlation_id, broker, clerk_id, operation_kind, nonsecret_target_ref,
    idempotency_key,
    CASE state WHEN 'failed' THEN 'provider_refused' ELSE state END,
    upstream_receipt_ref, NULL, NULL, NULL, updated_at_ms, created_at_ms, updated_at_ms
FROM routing_receipts""",
    "DROP TABLE routing_receipts",
    "ALTER TABLE routing_receipts_v2 RENAME TO routing_receipts",
    "CREATE UNIQUE INDEX ux_routing_receipts_idempotency ON routing_receipts(broker, clerk_id, idempotency_key)",
    """CREATE TRIGGER trg_routing_receipts_no_delete
BEFORE DELETE ON routing_receipts
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt is never deleted');
END""",
    """CREATE TRIGGER trg_routing_receipts_outcome_only
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN
    OLD.correlation_id IS NOT NEW.correlation_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.operation_kind IS NOT NEW.operation_kind
    OR OLD.nonsecret_target_ref IS NOT NEW.nonsecret_target_ref
    OR OLD.idempotency_key IS NOT NEW.idempotency_key
    OR OLD.pinned_routing_epoch IS NOT NEW.pinned_routing_epoch
    OR OLD.pinned_binding_generation IS NOT NEW.pinned_binding_generation
    OR OLD.pinned_agent_instance_id IS NOT NEW.pinned_agent_instance_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt identity and pinned context are immutable; only its outcome may move');
END""",
    """CREATE TRIGGER trg_routing_receipts_delivered_terminal
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.state = 'delivered' AND NEW.state <> 'delivered'
BEGIN
    SELECT RAISE(ABORT, 'a delivered routing outcome is never downgraded');
END""",
    """CREATE TRIGGER trg_routing_receipts_dispatch_monotonic
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.dispatched_at_ms IS NOT NULL AND NEW.dispatched_at_ms IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a dispatched routing attempt is never un-dispatched');
END""",
    # The attestation uniqueness moves to namespace scope (the v1 index was
    # dropped with the column's old meaning): the same attestation name in
    # two deployment namespaces is two volumes, not a collision. The added
    # namespace column itself carries no CHECK parity with the fresh DDL —
    # SQLite cannot attach one through ALTER — and the service's namespace
    # validation remains the write-path guard.
    "DROP INDEX ux_clerks_volume_attestation",
    (
        "CREATE UNIQUE INDEX ux_clerks_volume_attestation "
        "ON clerks(deployment_namespace, volume_attestation_kind, volume_attestation_id) "
        "WHERE lifecycle_state <> 'retired'"
    ),
)

# The v2→v3 upgrade (#2073b): the nested-root fence gains DDL. The statement
# text is byte-identical to the fresh v3 DDL above — a migrated registry and a
# fresh one must carry the same ``sqlite_master`` entries, which
# ``test_a_v2_registry_gains_the_nested_volume_root_fence`` pins. The equal-root
# index is registered first, so an upgrade over a registry that already
# violates it fails loudly instead of installing half a fence.
_MIGRATION_V2_TO_V3: tuple[str, ...] = (
    """CREATE UNIQUE INDEX ux_clerks_volume_root
    ON clerks(deployment_namespace, volume_root) WHERE lifecycle_state <> 'retired'""",
    """CREATE TRIGGER trg_clerks_volume_root_not_nested
BEFORE INSERT ON clerks
FOR EACH ROW WHEN NEW.lifecycle_state <> 'retired' AND EXISTS (
    SELECT 1 FROM clerks existing
    WHERE existing.lifecycle_state <> 'retired'
      AND existing.deployment_namespace = NEW.deployment_namespace
      AND (substr(NEW.volume_root, 1, length(existing.volume_root) + 1)
               = existing.volume_root || '/'
           OR substr(existing.volume_root, 1, length(NEW.volume_root) + 1)
               = NEW.volume_root || '/')
)
BEGIN
    SELECT RAISE(ABORT, 'one clerk, one physical volume: a volume root never nests inside another active clerk''s root');
END""",
)

# The v3→v4 upgrade (#2133 P2-a): the audit read surface's two newest-first
# indexes. Byte-identical to the fresh v4 DDL above, so a migrated registry
# and a fresh one carry the same ``sqlite_master`` entries. Both statements
# are additive ``CREATE INDEX``, never a table rewrite, so the upgrade cannot
# lose or reshape a row.
_MIGRATION_V3_TO_V4: tuple[str, ...] = (
    """CREATE INDEX ix_routing_receipts_created_at
    ON routing_receipts(created_at_ms, correlation_id)""",
    """CREATE INDEX ix_routing_receipts_clerk_created_at
    ON routing_receipts(clerk_id, created_at_ms, correlation_id)""",
)

# The v4→v5 upgrade (ADR 0063, #2111): the drain ceremony's columns, indexes
# and trigger backstops. The column adds carry the self-referencing bounds
# SQLite accepts through ALTER; the two cross-column parities — draining
# requires its two instants, retired_at_ms pairs with 'retired' — cannot ride
# an ALTER and stay service-guarded, exactly as the v1→v2 namespace column's
# parity did. The lifecycle trigger is dropped and recreated with its fourth
# arm, byte-identical to the fresh v5 DDL, and the new write-once trigger is
# installed alongside it. The two CREATE INDEX statements are additive.
#
# The fourth arm is the one non-additive change in v5: a registry upgraded
# from v4 refuses a direct provisioned -> retired transition it previously
# accepted, for any clerk that has served. That is Decision 6's point.
_MIGRATION_V4_TO_V5_TEMPLATE: tuple[str, ...] = (
    (
        "ALTER TABLE clerks ADD COLUMN draining_since_ms INTEGER "
        "CHECK (draining_since_ms IS NULL OR (draining_since_ms >= 0 AND "
        "draining_since_ms <= MAX_TIMESTAMP_MS))"
    ),
    (
        "ALTER TABLE clerks ADD COLUMN drain_deadline_at_ms INTEGER "
        "CHECK (drain_deadline_at_ms IS NULL OR (drain_deadline_at_ms >= 0 AND "
        "drain_deadline_at_ms <= MAX_TIMESTAMP_MS))"
    ),
    (
        "ALTER TABLE clerks ADD COLUMN lane_confirmation TEXT "
        "CHECK (lane_confirmation IS NULL OR lane_confirmation IN ('absent', 'present'))"
    ),
    (
        "ALTER TABLE clerks ADD COLUMN retire_operator TEXT "
        "CHECK (retire_operator IS NULL OR (length(retire_operator) > 0 AND "
        "length(retire_operator) <= 128))"
    ),
    (
        "ALTER TABLE clerks ADD COLUMN retire_change_ref TEXT "
        "CHECK (retire_change_ref IS NULL OR (length(retire_change_ref) > 0 AND "
        "length(retire_change_ref) <= 512))"
    ),
    (
        "ALTER TABLE account_assignment_history ADD COLUMN lane_confirmation TEXT "
        "CHECK (lane_confirmation IS NULL OR lane_confirmation IN ('absent', 'present'))"
    ),
    (
        "ALTER TABLE account_assignment_history ADD COLUMN attested_operator TEXT "
        "CHECK (attested_operator IS NULL OR (length(attested_operator) > 0 AND "
        "length(attested_operator) <= 128))"
    ),
    (
        "ALTER TABLE account_assignment_history ADD COLUMN attested_change_ref TEXT "
        "CHECK (attested_change_ref IS NULL OR (length(attested_change_ref) > 0 AND "
        "length(attested_change_ref) <= 512))"
    ),
    (
        "ALTER TABLE account_assignment_history ADD COLUMN attested_at_ms INTEGER "
        "CHECK (attested_at_ms IS NULL OR (attested_at_ms >= 0 AND "
        "attested_at_ms <= MAX_TIMESTAMP_MS))"
    ),
    """CREATE INDEX ix_routing_receipts_unsettled
    ON routing_receipts(clerk_id)
    WHERE state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL""",
    """CREATE INDEX ix_account_assignment_history_clerk
    ON account_assignment_history(clerk_id)""",
    """CREATE TABLE force_retire_correlations (
    correlation_id  TEXT PRIMARY KEY,
    broker          TEXT NOT NULL CHECK (length(broker) > 0),
    clerk_id        TEXT NOT NULL CHECK (length(clerk_id) > 0),
    forced_at_ms INTEGER NOT NULL CHECK (forced_at_ms >= 0 AND forced_at_ms <= MAX_TIMESTAMP_MS),
    operator TEXT NOT NULL CHECK (length(operator) > 0 AND length(operator) <= 128),
    change_ref TEXT NOT NULL CHECK (length(change_ref) > 0 AND length(change_ref) <= 512)
)""",
    """CREATE TRIGGER trg_force_retire_correlations_immutable
BEFORE UPDATE ON force_retire_correlations
BEGIN
    SELECT RAISE(ABORT, 'forced-unknown obligations are append-only');
END""",
    """CREATE TRIGGER trg_force_retire_correlations_no_delete
BEFORE DELETE ON force_retire_correlations
BEGIN
    SELECT RAISE(ABORT, 'forced-unknown obligations are append-only');
END""",
    "DROP TRIGGER trg_clerks_lifecycle_forward",
    """CREATE TRIGGER trg_clerks_lifecycle_forward
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'provisioned' AND OLD.retired_at_ms IS NOT NEW.retired_at_ms)
    OR (OLD.lifecycle_state = 'draining' AND NEW.lifecycle_state NOT IN ('draining', 'retired'))
    OR (OLD.lifecycle_state = 'retired' AND NEW.lifecycle_state <> 'retired')
    OR (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'retired' AND (
        EXISTS (SELECT 1 FROM account_assignment_history WHERE clerk_id = OLD.clerk_id)
        OR EXISTS (SELECT 1 FROM clerk_session_history WHERE clerk_id = OLD.clerk_id)
        OR EXISTS (SELECT 1 FROM clerk_sessions WHERE clerk_id = OLD.clerk_id)
    ))
BEGIN
    SELECT RAISE(ABORT, 'a clerk lifecycle moves forward only, and a clerk that has served retires through draining');
END""",
    """CREATE TRIGGER trg_clerks_closeout_facts_write_once
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state <> 'provisioned' AND (
        OLD.draining_since_ms IS NOT NEW.draining_since_ms
        OR OLD.drain_deadline_at_ms IS NOT NEW.drain_deadline_at_ms
    ))
    OR (OLD.lifecycle_state = 'retired' AND (
        OLD.lane_confirmation IS NOT NEW.lane_confirmation
        OR OLD.retire_operator IS NOT NEW.retire_operator
        OR OLD.retire_change_ref IS NOT NEW.retire_change_ref
    ))
BEGIN
    SELECT RAISE(ABORT, 'a clerk''s drain and retirement facts are written once and never rewritten');
END""",
)

_MIGRATION_V5_TO_V6_TEMPLATE: tuple[str, ...] = (
    """CREATE TABLE clerk_lane_confirmations (
    confirmation_seq  INTEGER PRIMARY KEY,
    clerk_id          TEXT NOT NULL CHECK (length(clerk_id) > 0),
    agent_instance_id TEXT NOT NULL CHECK (length(agent_instance_id) > 0),
    routing_epoch  INTEGER NOT NULL CHECK (routing_epoch >= 1),
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0 AND observed_at_ms <= MAX_TIMESTAMP_MS),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    runner_idle       INTEGER NOT NULL CHECK (runner_idle IN (0, 1)),
    broker_work_ended INTEGER NOT NULL CHECK (broker_work_ended IN (0, 1)),
    account_flat      INTEGER NOT NULL CHECK (account_flat IN (0, 1)),
    intents_resolved  INTEGER NOT NULL CHECK (intents_resolved IN (0, 1))
)""",
    """CREATE INDEX ix_clerk_lane_confirmations_latest
    ON clerk_lane_confirmations(clerk_id, agent_instance_id, routing_epoch, confirmation_seq DESC)""",
    """CREATE TRIGGER trg_clerk_lane_confirmations_immutable
BEFORE UPDATE ON clerk_lane_confirmations
BEGIN
    SELECT RAISE(ABORT, 'lane-quiet confirmations are append-only');
END""",
    """CREATE TRIGGER trg_clerk_lane_confirmations_no_delete
BEFORE DELETE ON clerk_lane_confirmations
BEGIN
    SELECT RAISE(ABORT, 'lane-quiet confirmations are append-only');
END""",
)

#: v6 -> v7 (#2154): one live assignment per clerk becomes structural. The
#: create fails the whole upgrade if a registry already breaks the rule.
_MIGRATION_V6_TO_V7: tuple[str, ...] = (
    "DROP INDEX ix_account_assignments_owner",
    """CREATE UNIQUE INDEX ix_account_assignments_owner
    ON account_assignments(clerk_id) WHERE state <> 'released'""",
)


SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: tuple(
        statement.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))
        for statement in _MIGRATION_V1_TO_V2_TEMPLATE
    ),
    2: _MIGRATION_V2_TO_V3,
    3: _MIGRATION_V3_TO_V4,
    4: tuple(
        statement.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))
        for statement in _MIGRATION_V4_TO_V5_TEMPLATE
    ),
    5: tuple(
        statement.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))
        for statement in _MIGRATION_V5_TO_V6_TEMPLATE
    ),
    6: _MIGRATION_V6_TO_V7,
}


__all__ = [
    "SCHEMA_DDL_V1",
    "SCHEMA_MIGRATIONS",
]
