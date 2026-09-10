"""The profiles database schema, its PRAGMAs and its versioned migrations.

A dedicated SQLite database on the Clerk volume (ADR 0060 Decision 2),
following the Clerk's own conventions — WAL, ``PRAGMA foreign_keys = ON``, a
guarded singleton metadata row carrying the schema version, and additive-only
registered migrations applied inside one transaction. It is **not** part of any
account's custody database and shares no table with one.

Column types are load-bearing on one table. ``profile_revisions`` stores the
four live-envelope floats as ``REAL`` and the two session counts as
``INTEGER``; ``NUMERIC`` is banned here because ``decimal.Decimal`` cannot
reach ``LiveEnvelopeValues.sha`` (ADR 0060 Decision 6). The load path converts
explicitly through ``ValidatedLiveEnvelope`` rather than trusting type
affinity.
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
-- configuration_meta — guarded singleton carrying the schema version
-- ============================================================
CREATE TABLE configuration_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

-- ============================================================
-- local_owner — exactly one row (ADR 0060 Decision 3)
-- ============================================================
CREATE TABLE local_owner (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    -- UNIQUE so the owner references below resolve: SQLite requires a foreign
    -- key's parent column to be a primary key or carry a unique index.
    owner_id            TEXT NOT NULL UNIQUE,
    display_label       TEXT NOT NULL CHECK (length(display_label) > 0),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

-- ============================================================
-- broker_profiles — named, archivable, owned
-- ============================================================
CREATE TABLE broker_profiles (
    profile_id          TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL REFERENCES local_owner(owner_id),
    broker              TEXT NOT NULL CHECK (broker = 'alpaca'),
    display_name        TEXT NOT NULL CHECK (length(display_name) > 0),
    archived            INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);

-- Uniqueness is the schema's job, not the service's (contract §5).
CREATE UNIQUE INDEX ux_broker_profiles_live_name
    ON broker_profiles(owner_id, display_name) WHERE archived = 0;

-- ============================================================
-- profile_revisions — immutable content; the pin is the one binding write
-- ============================================================
CREATE TABLE profile_revisions (
    profile_id              TEXT NOT NULL REFERENCES broker_profiles(profile_id),
    revision                INTEGER NOT NULL CHECK (revision >= 1),
    schema_version          INTEGER NOT NULL,
    credential_slot         TEXT NOT NULL,
    endpoint_mode           TEXT NOT NULL CHECK (endpoint_mode IN ('paper', 'live')),
    account_pin             TEXT,
    account_pinned_at_ms    INTEGER,
    -- The six live-envelope values. REAL for the floats and INTEGER for the
    -- counts, so store -> load -> sha returns today's sha (ADR 0060 D6).
    -- NUMERIC is banned on this path; a Decimal raises rather than hashing.
    live_loss_fraction      REAL,
    live_loss_usd           REAL,
    live_shadow_sessions    INTEGER,
    live_arming_max_sessions INTEGER,
    live_xh_entry_bps       REAL,
    live_xh_exit_bps        REAL,
    content_sha256          TEXT NOT NULL,
    complete                INTEGER NOT NULL CHECK (complete IN (0, 1)),
    author_owner_id         TEXT NOT NULL REFERENCES local_owner(owner_id),
    created_at_ms           INTEGER NOT NULL,
    PRIMARY KEY (profile_id, revision),
    -- A live revision carries the whole envelope or none of it; a paper
    -- revision may carry none. Six-way all-or-nothing, in the schema.
    CHECK (
        (live_loss_fraction IS NULL AND live_loss_usd IS NULL
         AND live_shadow_sessions IS NULL AND live_arming_max_sessions IS NULL
         AND live_xh_entry_bps IS NULL AND live_xh_exit_bps IS NULL)
        OR
        (live_loss_fraction IS NOT NULL AND live_loss_usd IS NOT NULL
         AND live_shadow_sessions IS NOT NULL AND live_arming_max_sessions IS NOT NULL
         AND live_xh_entry_bps IS NOT NULL AND live_xh_exit_bps IS NOT NULL)
    ),
    CHECK ((account_pin IS NULL) = (account_pinned_at_ms IS NULL))
);

CREATE INDEX ix_profile_revisions_profile ON profile_revisions(profile_id, revision DESC);

-- ============================================================
-- account_nicknames — keyed to the observed broker account ID
-- ============================================================
CREATE TABLE account_nicknames (
    account_id          TEXT PRIMARY KEY,
    nickname            TEXT NOT NULL CHECK (length(nickname) > 0),
    updated_at_ms       INTEGER NOT NULL
);

-- ============================================================
-- installation_selection — one row in v1; no worker identity (ADR 0060 D5)
-- ============================================================
CREATE TABLE installation_selection (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    staged_profile_id           TEXT,
    staged_revision             INTEGER,
    apply_requested             INTEGER NOT NULL DEFAULT 0 CHECK (apply_requested IN (0, 1)),
    apply_requested_at_ms       INTEGER,
    apply_requested_generation  INTEGER,
    selection_generation        INTEGER NOT NULL CHECK (selection_generation >= 0),
    effective_profile_id        TEXT,
    effective_revision          INTEGER,
    effective_account_id        TEXT,
    effective_acknowledged_at_ms INTEGER,
    last_apply_outcome          TEXT CHECK (last_apply_outcome IN ('applied', 'refused')),
    last_apply_refusal_reason   TEXT,
    CHECK ((staged_profile_id IS NULL) = (staged_revision IS NULL)),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL)),
    FOREIGN KEY (staged_profile_id, staged_revision)
        REFERENCES profile_revisions(profile_id, revision),
    FOREIGN KEY (effective_profile_id, effective_revision)
        REFERENCES profile_revisions(profile_id, revision)
);

-- ============================================================
-- configuration_events — append-only audit; never a secret-derived hash
-- ============================================================
CREATE TABLE configuration_events (
    event_id            TEXT PRIMARY KEY,
    sequence            INTEGER NOT NULL,
    actor_owner_id      TEXT NOT NULL,
    action              TEXT NOT NULL,
    profile_id          TEXT,
    revision            INTEGER,
    previous_ref        TEXT,
    next_ref            TEXT,
    result              TEXT NOT NULL,
    recorded_at_ms      INTEGER NOT NULL
);

CREATE UNIQUE INDEX ux_configuration_events_sequence ON configuration_events(sequence);
"""

# Additive-only upgrades keyed by the ``schema_version`` they start from. v1 is
# the initial schema, so the registry is empty by construction; the machinery
# ships with it (and is exercised by
# ``tests/broker_configuration/test_schema_migration.py``) so the first real
# upgrade is a table entry rather than a new mechanism designed under pressure.
SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {}


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set to a freshly-opened connection."""
    for statement in PRAGMA_STATEMENTS:
        conn.execute(statement)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index from ``SCHEMA_DDL`` (fresh init only)."""
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

    One transaction, so a partially-applied upgrade can never be observed:
    either every statement lands and ``configuration_meta.schema_version``
    advances, or none do. Replaying it against an already-current database is a
    no-op. An unregistered version raises rather than guessing.
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
            "UPDATE configuration_meta SET schema_version = ? WHERE id = 1",
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
