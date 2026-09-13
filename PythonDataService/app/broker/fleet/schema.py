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
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

SCHEMA_DDL = """\
-- ============================================================
-- fleet_meta — guarded singleton carrying the schema version
-- ============================================================
CREATE TABLE fleet_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    registry_id         TEXT NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
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
    volume_attestation_kind TEXT NOT NULL CHECK (length(volume_attestation_kind) > 0),
    volume_attestation_id   TEXT NOT NULL CHECK (length(volume_attestation_id) > 0),
    lifecycle_state         TEXT NOT NULL CHECK (lifecycle_state IN ('provisioned', 'draining', 'retired')),
    created_at_ms           INTEGER NOT NULL,
    retired_at_ms           INTEGER,
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
    ON clerks(volume_attestation_kind, volume_attestation_id) WHERE lifecycle_state <> 'retired';

CREATE INDEX ix_clerks_listing ON clerks(broker, created_at_ms, clerk_id);

-- ============================================================
-- clerk_sessions — the one current registration per clerk
-- ============================================================
CREATE TABLE clerk_sessions (
    clerk_id                    TEXT PRIMARY KEY REFERENCES clerks(clerk_id),
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms               INTEGER NOT NULL,
    last_seen_at_ms             INTEGER NOT NULL,
    reported_binding_generation INTEGER,
    reported_account_id         TEXT,
    reported_state              TEXT
);

-- Every registration is kept, so a clerk's epoch history is auditable and a
-- duplicated live session is structurally impossible: the current row above
-- is the only place an agent can present itself.
CREATE TABLE clerk_session_history (
    clerk_id                    TEXT NOT NULL,
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms               INTEGER NOT NULL,
    superseded_at_ms            INTEGER NOT NULL,
    PRIMARY KEY (clerk_id, routing_epoch)
);

-- ============================================================
-- account_assignments — the broker-qualified fence (PRD §9.6)
-- ============================================================
CREATE TABLE account_assignments (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL CHECK (length(canonical_external_account_id) > 0),
    clerk_id                        TEXT NOT NULL REFERENCES clerks(clerk_id),
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    recorded_at_ms                  INTEGER NOT NULL,
    updated_at_ms                   INTEGER NOT NULL,
    PRIMARY KEY (broker, canonical_external_account_id),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL))
);

CREATE INDEX ix_account_assignments_owner
    ON account_assignments(clerk_id) WHERE state <> 'released';

-- ============================================================
-- routing_receipts — correlation only, never execution evidence
-- ============================================================
CREATE TABLE routing_receipts (
    correlation_id      TEXT PRIMARY KEY,
    broker              TEXT NOT NULL,
    clerk_id            TEXT NOT NULL,
    operation_kind      TEXT NOT NULL,
    nonsecret_target_ref TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('delivered', 'failed', 'outcome_unknown')),
    upstream_receipt_ref TEXT,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
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

-- A receipt is inserted once; only its outcome state, upstream reference and
-- updated timestamp may move.
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
"""

# Additive-only upgrades keyed by the ``schema_version`` they start from; v1 is
# the initial schema, so the registry is empty by construction. The machinery
# ships with it (and is exercised by ``tests/broker/fleet/test_registry_store.py``)
# so the first real upgrade is a table entry, mirroring the profiles database.
SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {}


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set to a freshly-opened connection."""
    for statement in PRAGMA_STATEMENTS:
        conn.execute(statement)


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
    "SCHEMA_MIGRATIONS",
    "SCHEMA_VERSION",
    "apply_schema",
    "configure_connection",
    "is_upgradable_to_current",
    "migrate_schema",
]
