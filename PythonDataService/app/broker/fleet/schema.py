"""The fleet registry schema, its PRAGMAs and its versioned migrations.

A dedicated SQLite database on the coordinator's own control volume (PRD
FR-030), following the repository's established conventions — WAL,
``PRAGMA foreign_keys = ON``, a guarded singleton metadata row carrying the
schema version, additive-only registered migrations applied inside one
transaction, and uniqueness expressed in the schema rather than in service
code.

The registry is deliberately custody-free (PRD FR-023 / ADR 0062 Decision 1):
no table here names an order, fill, position, activation, arming or envelope
fact, and ``tests/broker/fleet/test_registry_contains_no_custody.py`` asserts
the shipped DDL never grows one. All timestamps are ``INTEGER`` ms UTC; no
textual datetime storage exists in this schema.

Schema v2 (audit 2026-09-13, finding 1/4/7) separates *observed* from
*confirmed* facts: the assignment row now carries the coordinator's confirmed
binding observation (generation, tuple, instance, epoch) and is the only
routing fence; sessions carry heartbeat observations only. Volume roots are
qualified by a deployment namespace so equal path strings in two containers
are not mistaken for one physical volume, and approved endpoints are
deployment-owned rows an agent can cite but never change. Routing receipts
carry the pinned attempt context and a four-state outcome vocabulary in which
a delivered outcome is terminal.
"""

from __future__ import annotations

import sqlite3

from app.utils.session_anchors import MAX_TIMESTAMP_MS

SCHEMA_VERSION = 2

#: Every ``*_ms`` column carries this bound in the schema, so a corrupt or
#: hostile write cannot persist a negative or out-of-range instant as fleet
#: evidence (ADR 0022's ``int64 ms UTC`` domain).
PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

# The DDL spells the bound symbolically and the constant is substituted once,
# below, so the checked number lives in exactly one place.
_SCHEMA_DDL_TEMPLATE = """\
-- ============================================================
-- fleet_meta — guarded singleton carrying the schema version
-- ============================================================
CREATE TABLE fleet_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    registry_id         TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);

-- ============================================================
-- clerks — durable lanes; identity columns are immutable
-- ============================================================
CREATE TABLE clerks (
    clerk_id                TEXT PRIMARY KEY,
    broker                  TEXT NOT NULL CHECK (length(broker) > 0),
    worker_key              TEXT NOT NULL,
    display_label           TEXT NOT NULL CHECK (length(display_label) > 0),
    volume_id               TEXT NOT NULL,
    -- The canonical mounted root recorded at provisioning, used only to
    -- refuse two clerks sharing writable subtrees of one physical volume
    -- (FR-020/021). Deployment detail: never projected over the public API.
    volume_root             TEXT NOT NULL CHECK (length(volume_root) > 0),
    -- The deployment namespace qualifies every root comparison: two
    -- containers may legitimately mount distinct volumes at the same path,
    -- and equal strings across namespaces prove nothing (audit 2026-09-13,
    -- finding 4). Immutable like the rest of the clerk's identity.
    deployment_namespace    TEXT NOT NULL CHECK (length(deployment_namespace) > 0),
    volume_attestation_kind TEXT NOT NULL CHECK (length(volume_attestation_kind) > 0),
    volume_attestation_id   TEXT NOT NULL CHECK (length(volume_attestation_id) > 0),
    lifecycle_state         TEXT NOT NULL CHECK (lifecycle_state IN ('provisioned', 'draining', 'retired')),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    retired_at_ms INTEGER CHECK (retired_at_ms IS NULL OR (retired_at_ms >= 0 AND retired_at_ms <= MAX_TIMESTAMP_MS)),
    CHECK ((retired_at_ms IS NULL) = (lifecycle_state <> 'retired'))
);

-- Active clerks own unique worker keys, volumes and attestations (FR-032).
-- Partial indexes over the non-retired rows let a retired clerk's identifiers
-- rest in history without ever being recycled onto a new active clerk.
CREATE UNIQUE INDEX ux_clerks_worker_key
    ON clerks(worker_key) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_id
    ON clerks(volume_id) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_attestation
    ON clerks(deployment_namespace, volume_attestation_kind, volume_attestation_id)
    WHERE lifecycle_state <> 'retired';

CREATE INDEX ix_clerks_listing ON clerks(broker, created_at_ms, clerk_id);

-- ============================================================
-- clerk_sessions — the one current registration per clerk.
-- Observed facts only: nothing here can confirm an assignment.
-- ============================================================
CREATE TABLE clerk_sessions (
    clerk_id                    TEXT PRIMARY KEY REFERENCES clerks(clerk_id),
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0 AND last_seen_at_ms <= MAX_TIMESTAMP_MS),
    reported_binding_generation INTEGER,
    reported_account_id         TEXT,
    reported_state              TEXT,
    -- The deployment-approved endpoint reference this registration cites;
    -- the destination itself lives only in approved_endpoints and can never
    -- be changed by a registration (audit 2026-09-13, finding 4).
    endpoint_ref                TEXT,
    -- The serving agent's adapter build, for the mixed-version matrix.
    adapter_version             TEXT,
    -- The agent's bounded, typed summary observation, stored as strict JSON
    -- (validated at ingestion; never free-form agent-authored JSON).
    reported_summary_json       TEXT
);

-- Every registration is kept, so a clerk's epoch history is auditable and a
-- duplicated live session is structurally impossible: the current row above
-- is the only place an agent can present itself.
CREATE TABLE clerk_session_history (
    clerk_id                    TEXT NOT NULL,
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    superseded_at_ms INTEGER NOT NULL CHECK (superseded_at_ms >= 0 AND superseded_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (clerk_id, routing_epoch)
);

-- ============================================================
-- approved_endpoints — deployment-owned internal destinations.
-- Written only by the host ceremony; a registration may cite a reference,
-- never change what it points at.
-- ============================================================
CREATE TABLE approved_endpoints (
    endpoint_ref    TEXT PRIMARY KEY CHECK (length(endpoint_ref) > 0),
    clerk_id        TEXT NOT NULL REFERENCES clerks(clerk_id),
    base_url        TEXT NOT NULL CHECK (length(base_url) > 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);

CREATE UNIQUE INDEX ux_approved_endpoints_clerk ON approved_endpoints(clerk_id);

CREATE TRIGGER trg_approved_endpoints_no_delete
BEFORE DELETE ON approved_endpoints
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint is superseded by re-approval, never deleted');
END;

CREATE TRIGGER trg_approved_endpoints_identity_immutable
BEFORE UPDATE ON approved_endpoints
FOR EACH ROW WHEN
    OLD.endpoint_ref IS NOT NEW.endpoint_ref
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint belongs to its clerk and reference; re-approve the same reference with a new destination');
END;

-- ============================================================
-- account_assignments — the broker-qualified fence (PRD §9.6).
-- The confirmed_* columns are the coordinator's confirmed binding
-- observation: the exact facts a confirming worker presented, fenced by the
-- instance and epoch that presented them. They are a routing fence, not an
-- authority: the clerk-local selection transaction remains the single
-- authority for the effective tuple.
-- ============================================================
CREATE TABLE account_assignments (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL CHECK (length(canonical_external_account_id) > 0),
    clerk_id                        TEXT NOT NULL REFERENCES clerks(clerk_id),
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    confirmed_binding_generation    INTEGER,
    confirmed_profile_id            TEXT,
    confirmed_revision              INTEGER,
    confirmed_at_ms INTEGER CHECK (confirmed_at_ms IS NULL OR (confirmed_at_ms >= 0 AND confirmed_at_ms <= MAX_TIMESTAMP_MS)),
    confirmed_agent_instance_id     TEXT,
    confirmed_routing_epoch         INTEGER CHECK (confirmed_routing_epoch IS NULL OR confirmed_routing_epoch >= 1),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (broker, canonical_external_account_id),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL))
);

CREATE INDEX ix_account_assignments_owner
    ON account_assignments(clerk_id) WHERE state <> 'released';

-- ============================================================
-- routing_receipts — correlation only, never execution evidence.
-- The pinned_* context is written before dispatch and is immutable; the
-- outcome vocabulary separates not-dispatched, provider refusal, delivery
-- with a provider receipt, and unknown outcome. A delivered outcome is
-- terminal (audit 2026-09-13, finding 7).
-- ============================================================
CREATE TABLE routing_receipts (
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
);

-- One durable idempotency identity per lane and key (PRD FR-078): a retry
-- updates the same receipt instead of appending a sibling.
CREATE UNIQUE INDEX ux_routing_receipts_idempotency
    ON routing_receipts(broker, clerk_id, idempotency_key);

-- ============================================================
-- The invariants that are the schema's job, not a caller's
-- ============================================================

-- A clerk's identity never changes: broker, key, volume and attestation are
-- written once at provisioning, and rows are never deleted — retirement is a
-- lifecycle transition, and IDs are never recycled (PRD FR-013).
CREATE TRIGGER trg_clerks_identity_immutable
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
END;

CREATE TRIGGER trg_clerks_no_delete
BEFORE DELETE ON clerks
BEGIN
    SELECT RAISE(ABORT, 'a clerk is retired, never deleted; IDs are not recycled');
END;

-- Lifecycle moves forward only: provisioned -> draining -> retired, with a
-- direct provisioned -> retired allowed for a clerk that never served.
CREATE TRIGGER trg_clerks_lifecycle_forward
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'provisioned' AND OLD.retired_at_ms IS NOT NEW.retired_at_ms)
    OR (OLD.lifecycle_state = 'draining' AND NEW.lifecycle_state NOT IN ('draining', 'retired'))
    OR (OLD.lifecycle_state = 'retired' AND NEW.lifecycle_state <> 'retired')
BEGIN
    SELECT RAISE(ABORT, 'a clerk lifecycle moves forward only');
END;

-- The current session's epoch never decreases; history rows never change.
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

-- Assignment generations are monotonic and rows are never deleted: released
-- stays as terminal history, which is what makes "never expire into takeover"
-- auditable (PRD FR-053/054).
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

-- A receipt is inserted once with its pinned attempt context; only the
-- outcome state, upstream reference, dispatch time and updated timestamp may
-- move.
CREATE TRIGGER trg_routing_receipts_outcome_only
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
END;

CREATE TRIGGER trg_routing_receipts_no_delete
BEFORE DELETE ON routing_receipts
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt is never deleted');
END;

-- A delivered outcome is terminal: a late failed retry must never erase a
-- known successful result (audit 2026-09-13, finding 7).
CREATE TRIGGER trg_routing_receipts_delivered_terminal
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.state = 'delivered' AND NEW.state <> 'delivered'
BEGIN
    SELECT RAISE(ABORT, 'a delivered routing outcome is never downgraded');
END;

-- Dispatch is one-way: once an attempt was dispatched it stays dispatched,
-- so "definitively not dispatched" remains provable.
CREATE TRIGGER trg_routing_receipts_dispatch_monotonic
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.dispatched_at_ms IS NOT NULL AND NEW.dispatched_at_ms IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a dispatched routing attempt is never un-dispatched');
END;

-- ============================================================
-- account_assignment_history — append-only audit of every
-- assignment transition; the current row above is the pointer
-- ============================================================
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

SCHEMA_DDL = _SCHEMA_DDL_TEMPLATE.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))

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

SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: tuple(
        statement.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))
        for statement in _MIGRATION_V1_TO_V2_TEMPLATE
    ),
}


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set to a freshly-opened connection.

    ``PRAGMA journal_mode`` returns the mode SQLite actually retained, not the
    mode asked for: on a filesystem where WAL is unsupported it can silently
    fall back to a rollback journal. The returned mode is therefore inspected
    and anything other than ``wal`` refuses, so the concurrency-sensitive
    registry never opens in a weaker mode than its fences assume.
    """
    for statement in PRAGMA_STATEMENTS:
        cursor = conn.execute(statement)
        if statement == "PRAGMA journal_mode = WAL":
            retained = str(cursor.fetchone()[0]).lower()
            if retained != "wal":
                raise ValueError(
                    f"the fleet registry requires WAL journaling; SQLite retained "
                    f"{retained!r} instead — mount the control volume from a "
                    "container-local named volume"
                )


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table, index and trigger (fresh init only)."""
    conn.executescript(SCHEMA_DDL)


def is_upgradable_to_current(version: int) -> bool:
    """Whether a chained, registered migration path reaches ``SCHEMA_VERSION``."""
    seen = version
    while seen < SCHEMA_VERSION:
        if seen not in SCHEMA_MIGRATIONS:
            return False
        seen += 1
    return True


def migrate_schema(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Apply every registered upgrade from ``from_version`` to ``SCHEMA_VERSION``.

    One transaction, so a partially-applied upgrade can never be observed.
    An unregistered version raises rather than guessing.
    """
    version = from_version
    if version == SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        while version < SCHEMA_VERSION:
            statements = SCHEMA_MIGRATIONS.get(version)
            if statements is None:
                raise ValueError(
                    f"no registered migration from schema_version={version} to {version + 1}"
                )
            for statement in statements:
                conn.execute(statement)
            version += 1
        conn.execute(
            "UPDATE fleet_meta SET schema_version = ? WHERE id = 1",
            (SCHEMA_VERSION,),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = [
    "PRAGMA_STATEMENTS",
    "SCHEMA_DDL",
    "SCHEMA_DDL_V1",
    "SCHEMA_MIGRATIONS",
    "SCHEMA_VERSION",
    "apply_schema",
    "configure_connection",
    "is_upgradable_to_current",
    "migrate_schema",
]
