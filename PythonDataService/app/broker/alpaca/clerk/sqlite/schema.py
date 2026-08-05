"""Alpaca Clerk SQLite schema — generated to match the pinned contract.

The canonical schema definition is
``docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md`` §3. This module
embeds that exact DDL block (byte-for-byte — see
``tests/broker/alpaca/clerk/sqlite/test_schema_parity.py``) as the one place
production code executes it. Do not hand-edit ``SCHEMA_DDL`` without updating
the pinned-contracts doc in the same change; the parity test enforces that.

Guiding-philosophy #5 (single source of truth): the doc is the *reference*,
this module is the *canonical implementation*, and the parity test is what
keeps them from drifting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

# Byte-for-byte the fenced ```sql block from
# docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md §3.
SCHEMA_DDL = """\
-- ============================================================
-- control_meta — guarded singleton (PRD §9.1)
-- ============================================================
CREATE TABLE control_meta (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
    schema_version          INTEGER NOT NULL,
    broker                  TEXT NOT NULL CHECK (broker = 'alpaca'),
    account_id              TEXT NOT NULL,
    db_identity_token       TEXT NOT NULL,          -- random, minted at init; rejects file substitution
    authority_generation    INTEGER NOT NULL,       -- increments only on explicit reset (§13)
    control_revision        INTEGER NOT NULL DEFAULT 0,  -- monotonic; advanced by every fold
    created_at_ms           INTEGER NOT NULL,
    last_open_at_ms         INTEGER NOT NULL,
    reset_provenance_json   TEXT,                   -- null unless this generation began via reset
    execution_lease_owner   TEXT,                   -- process identity holding the lease, null if unheld
    execution_lease_expires_at_ms INTEGER
);

-- ============================================================
-- strategy_instances — immutable configured bot identity; insert once, retire only
-- ============================================================
CREATE TABLE strategy_instances (
    strategy_instance_id    TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    config_hash             TEXT NOT NULL,          -- content hash of the immutable bot config
    created_at_ms           INTEGER NOT NULL,
    retired_at_ms           INTEGER                 -- null while active; set once, never cleared
);

-- ============================================================
-- runs — per-instance run records and the active-run fence
-- ============================================================
CREATE TABLE runs (
    run_id                  TEXT PRIMARY KEY,
    strategy_instance_id    TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    lifecycle_run_id        TEXT NOT NULL,          -- the identity Start/Resume reserves (ADR 0035 #3)
    state                   TEXT NOT NULL CHECK (state IN ('ACTIVE','STOPPED')),
    started_at_ms           INTEGER NOT NULL,
    stopped_at_ms           INTEGER
);
-- one active run fence per strategy instance (PRD §9.4):
CREATE UNIQUE INDEX ux_runs_one_active_per_instance
    ON runs(strategy_instance_id) WHERE state = 'ACTIVE';
CREATE UNIQUE INDEX ux_runs_lifecycle_run_id ON runs(strategy_instance_id, lifecycle_run_id);

-- ============================================================
-- commands — content-addressed request identity (R2)
-- ============================================================
CREATE TABLE commands (
    command_id              TEXT PRIMARY KEY,       -- opaque durable resource id (GET /commands/{command_id})
    authority_generation    INTEGER NOT NULL,
    idempotency_key         TEXT NOT NULL,           -- canonical natural key, see §3a below
    payload_hash            TEXT NOT NULL,           -- immutable once first committed
    kind                    TEXT NOT NULL CHECK (kind IN ('strategy_decision','operator_lifecycle')),
    strategy_instance_id    TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    run_id                  TEXT REFERENCES runs(run_id),          -- null only for pre-run commands
    action                  TEXT NOT NULL,           -- e.g. START, RESUME, STOP, STOP_AND_FLATTEN, decision replay
    intended_end_state      TEXT,                    -- operator-lifecycle only
    state                   TEXT NOT NULL CHECK (state IN
                             ('reserved','rejected','accepted','in_progress','unknown','succeeded','failed')),
    effect_operation_id     TEXT REFERENCES effect_operations(effect_operation_id),
    receipt_id              TEXT REFERENCES receipts(receipt_id),
    created_at_ms           INTEGER NOT NULL,
    updated_at_ms           INTEGER NOT NULL
);
-- unique content-addressed command identity within the authority generation (§9.4):
CREATE UNIQUE INDEX ux_commands_idempotency
    ON commands(authority_generation, idempotency_key);

-- ============================================================
-- effect_operations — Clerk-owned ENTER/EXIT/cancel/recovery work (§7)
-- ============================================================
CREATE TABLE effect_operations (
    effect_operation_id     TEXT PRIMARY KEY,
    authority_generation    INTEGER NOT NULL,
    idempotency_key         TEXT NOT NULL,           -- Clerk effect idempotency identity, distinct from command's
    command_id              TEXT NOT NULL REFERENCES commands(command_id),
    strategy_instance_id    TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    run_id                  TEXT REFERENCES runs(run_id),
    kind                    TEXT NOT NULL CHECK (kind IN
                             ('ENTER','EXIT','CANCEL','RECONCILE','STOP','STOP_AND_FLATTEN')),
    state                   TEXT NOT NULL CHECK (state IN
                             ('reserved','rejected','accepted','in_progress','unknown','succeeded','failed')),
    custody_owner           TEXT NOT NULL DEFAULT 'ACCOUNT_CLERK',
    created_at_ms           INTEGER NOT NULL,
    updated_at_ms           INTEGER NOT NULL,
    terminal_receipt_id     TEXT REFERENCES receipts(receipt_id)
);
-- unique Clerk effect idempotency identity (§9.4):
CREATE UNIQUE INDEX ux_effect_operations_idempotency
    ON effect_operations(authority_generation, idempotency_key);

-- ============================================================
-- orders — one row per broker/client order identity (R7)
-- ============================================================
CREATE TABLE orders (
    order_ref                TEXT PRIMARY KEY,       -- Clerk-minted, committed before submission
    effect_operation_id      TEXT NOT NULL REFERENCES effect_operations(effect_operation_id),
    client_order_id          TEXT NOT NULL,           -- Alpaca reconciliation identity (R7)
    broker_order_id          TEXT,                    -- filled in once Alpaca acknowledges
    role                     TEXT NOT NULL CHECK (role IN ('ENTRY','REDUCING','OTHER')),
    broker_state             TEXT,                    -- last proven Alpaca order status
    submitted_at_ms          INTEGER,
    updated_at_ms            INTEGER NOT NULL
);
-- unique broker client_order_id/order reference (§9.4):
CREATE UNIQUE INDEX ux_orders_client_order_id ON orders(client_order_id);
CREATE UNIQUE INDEX ux_orders_broker_order_id ON orders(broker_order_id)
    WHERE broker_order_id IS NOT NULL;

-- ============================================================
-- fills — permanent executions and corrections (append/idempotent insert)
-- ============================================================
CREATE TABLE fills (
    fill_id                  TEXT PRIMARY KEY,       -- Alpaca execution id (idempotent identity, §9.4)
    order_ref                TEXT NOT NULL REFERENCES orders(order_ref),
    qty                      REAL NOT NULL,
    price                    REAL NOT NULL,
    side                     TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    is_correction             INTEGER NOT NULL DEFAULT 0,  -- 1 = broker-issued correction, not erasure of prior fact
    source_event_at_ms       INTEGER,                 -- Alpaca's fill timestamp, when supplied
    clerk_observed_at_ms     INTEGER NOT NULL,
    recorded_at_ms           INTEGER NOT NULL
);

-- ============================================================
-- positions — current Clerk-attributed exposure; fold of the log, namespace-attributed
-- (never nets from raw broker position — preserves existing exposure.py semantics)
-- ============================================================
CREATE TABLE positions (
    strategy_instance_id     TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    symbol                   TEXT NOT NULL,
    attributed_qty           REAL NOT NULL DEFAULT 0,
    updated_at_ms             INTEGER NOT NULL,
    PRIMARY KEY (strategy_instance_id, symbol)
);

-- ============================================================
-- holds — active and resolved bot/Account-Clerk holds; fold of the log
-- ============================================================
CREATE TABLE holds (
    hold_id                  TEXT PRIMARY KEY,
    scope                    TEXT NOT NULL CHECK (scope IN ('BOT','ACCOUNT_CLERK')),
    strategy_instance_id     TEXT REFERENCES strategy_instances(strategy_instance_id),  -- null for ACCOUNT_CLERK scope
    reason_code               TEXT NOT NULL,
    state                     TEXT NOT NULL CHECK (state IN ('ACTIVE','RESOLVED')),
    opened_at_ms               INTEGER NOT NULL,
    resolved_at_ms             INTEGER,
    evidence_refs_json         TEXT
);

-- ============================================================
-- uncertainties — active nonterminal unknowns; fold of the log. Columns are
-- the R5 envelope verbatim.
-- ============================================================
CREATE TABLE uncertainties (
    uncertainty_id            TEXT PRIMARY KEY,
    scope                     TEXT NOT NULL CHECK (scope IN ('BOT','ACCOUNT_CLERK')),
    severity                  TEXT NOT NULL,
    blocks_new_exposure       INTEGER NOT NULL CHECK (blocks_new_exposure IN (0,1)),
    allows_reduction          INTEGER NOT NULL CHECK (allows_reduction IN (0,1)),
    custody_owner             TEXT NOT NULL DEFAULT 'ACCOUNT_CLERK',
    strategy_instance_id      TEXT REFERENCES strategy_instances(strategy_instance_id),  -- null for ACCOUNT_CLERK
    reason_code               TEXT NOT NULL,
    headline                  TEXT NOT NULL,
    explanation               TEXT NOT NULL,
    operator_impact           TEXT NOT NULL,
    next_step                 TEXT NOT NULL,
    observed_at_ms            INTEGER NOT NULL,
    resolved_at_ms            INTEGER,       -- null while active
    evidence_refs_json        TEXT,
    facts_schema_version      INTEGER NOT NULL,
    facts_json                TEXT NOT NULL
);

-- ============================================================
-- reconciliations — reconciliation attempts and terminal receipts
-- ============================================================
CREATE TABLE reconciliations (
    reconciliation_id        TEXT PRIMARY KEY,
    effect_operation_id      TEXT REFERENCES effect_operations(effect_operation_id),
    order_ref                TEXT REFERENCES orders(order_ref),
    trigger                  TEXT NOT NULL CHECK (trigger IN ('AUTOMATIC','OPERATOR_RECONCILE_NOW')),
    attempted_at_ms           INTEGER NOT NULL,
    outcome                   TEXT NOT NULL CHECK (outcome IN ('STILL_UNKNOWN','RESOLVED_SUCCESS','RESOLVED_FAILURE')),
    evidence_refs_json        TEXT
);

-- ============================================================
-- receipts — permanent terminal command/effect proof; insert once
-- ============================================================
CREATE TABLE receipts (
    receipt_id                TEXT PRIMARY KEY,
    command_id                 TEXT REFERENCES commands(command_id),
    effect_operation_id        TEXT REFERENCES effect_operations(effect_operation_id),
    terminal_state              TEXT NOT NULL CHECK (terminal_state IN ('succeeded','failed')),
    summary_code                 TEXT NOT NULL,          -- stable code; prose comes from the closed registry (§11 rule 5)
    proof_reference               TEXT,
    recorded_at_ms                 INTEGER NOT NULL,
    facts_json                      TEXT
);

-- ============================================================
-- custody_transitions — THE canonical, append-only, hash-chained,
-- operation-first log. Columns are the PRD §11 list verbatim.
-- ============================================================
CREATE TABLE custody_transitions (
    sequence                  INTEGER PRIMARY KEY AUTOINCREMENT,   -- serialization order, not broker time
    prev_hash                 TEXT,                     -- null only for sequence 1
    row_hash                  TEXT NOT NULL,
    authority_generation      INTEGER NOT NULL,
    strategy_instance_id      TEXT REFERENCES strategy_instances(strategy_instance_id) DEFERRABLE INITIALLY DEFERRED,
    run_id                    TEXT REFERENCES runs(run_id) DEFERRABLE INITIALLY DEFERRED,
    command_id                TEXT REFERENCES commands(command_id) DEFERRABLE INITIALLY DEFERRED,
    effect_operation_id       TEXT REFERENCES effect_operations(effect_operation_id) DEFERRABLE INITIALLY DEFERRED,
    order_ref                 TEXT REFERENCES orders(order_ref) DEFERRABLE INITIALLY DEFERRED,
    broker_order_id           TEXT,
    transition_kind           TEXT NOT NULL,
    custody_owner             TEXT NOT NULL,
    execution_authority       TEXT NOT NULL,
    operation_state           TEXT NOT NULL,
    broker_state              TEXT,
    proof_reference           TEXT,
    source_event_at_ms        INTEGER,   -- null unless Alpaca supplied one; never backfilled
    clerk_observed_at_ms      INTEGER NOT NULL,
    recorded_at_ms            INTEGER NOT NULL,
    summary_code              TEXT NOT NULL,
    facts_schema_version      INTEGER NOT NULL,
    facts_json                TEXT NOT NULL
);
-- immutable sequence/payload/hash-chain-link after commit (§9.4): enforced by
-- the triggers below, not just by Slice 2's repository boundary declining to
-- issue UPDATE/DELETE. A dedicated test asserts both hold.

-- ============================================================
-- mirror_fence — records that the SQLite transaction verified and consumed a
-- matching PREPARE line from the external mirror file (§8). It never gets a
-- FINALIZE-phase row: the external mirror's finalize fsync (R9 step 3) happens
-- *after* this transaction commits, so re-recording it in SQLite would need a
-- second, separately-fenced write — the same unbounded-regress problem the
-- two-phase fence exists to avoid. "Is sequence N finalized?" is answered by
-- reading the external mirror file's tail at startup (§9 check 9), never by a
-- SQLite column. `phase` is retained rather than dropped only so a future
-- schema version could add a durable FINALIZE marker without a migration;
-- today it is always 'PREPARE'.
-- ============================================================
CREATE TABLE mirror_fence (
    sequence                  INTEGER PRIMARY KEY REFERENCES custody_transitions(sequence),
    phase                     TEXT NOT NULL CHECK (phase = 'PREPARE'),
    row_hash                  TEXT NOT NULL,
    authority_generation      INTEGER NOT NULL,
    recorded_at_ms            INTEGER NOT NULL
);

-- ============================================================
-- Immutability backstops (§9.4): the repository boundary is the only writer
-- and never issues UPDATE/DELETE against these two invariants, but per the
-- ADR's "robustness is bought explicitly" posture, convention alone is not
-- the standard this program holds itself to elsewhere — these two get a
-- database-enforced backstop rather than resting on that convention alone.
-- ============================================================
CREATE TRIGGER trg_custody_transitions_immutable_update
BEFORE UPDATE ON custody_transitions
BEGIN
    SELECT RAISE(ABORT, 'custody_transitions is append-only: UPDATE forbidden');
END;

CREATE TRIGGER trg_custody_transitions_immutable_delete
BEFORE DELETE ON custody_transitions
BEGIN
    SELECT RAISE(ABORT, 'custody_transitions is append-only: DELETE forbidden');
END;

CREATE TRIGGER trg_commands_payload_hash_immutable
BEFORE UPDATE OF payload_hash ON commands
WHEN OLD.payload_hash IS NOT NULL AND NEW.payload_hash != OLD.payload_hash
BEGIN
    SELECT RAISE(ABORT, 'commands.payload_hash is immutable once committed');
END;\
"""


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set (§2) to a freshly-opened connection."""
    for statement in PRAGMA_STATEMENTS:
        conn.execute(statement)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes/triggers from ``SCHEMA_DDL`` (fresh init only)."""
    conn.executescript(SCHEMA_DDL)


def load_pinned_ddl(repo_root: Path) -> str:
    """Re-extract the SQL block from the pinned-contracts doc (parity test seam)."""
    import re

    doc_path = (
        repo_root
        / "docs"
        / "architecture"
        / "alpaca-clerk-sqlite-pinned-contracts.md"
    )
    text = doc_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```sql\n(.*?)\n```", text, re.DOTALL)
    matches = [block for block in blocks if "CREATE TABLE" in block]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one CREATE-TABLE SQL block in {doc_path}, found {len(matches)}"
        )
    return matches[0]
