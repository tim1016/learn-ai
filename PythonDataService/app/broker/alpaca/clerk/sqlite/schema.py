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

SCHEMA_VERSION = 8

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
-- composite target for commands/effect_operations run ownership below —
-- run_id is already globally unique (PK), this pins the (instance, run) pair
-- so a cross-bot FK reference is rejected rather than silently satisfiable:
CREATE UNIQUE INDEX ux_runs_instance_run ON runs(strategy_instance_id, run_id);
CREATE INDEX ix_runs_started_at ON runs(started_at_ms DESC);
CREATE INDEX ix_runs_strategy_started_at
    ON runs(strategy_instance_id, started_at_ms DESC);

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
    run_id                  TEXT,                    -- null only for pre-run commands; see composite FK below
    action                  TEXT NOT NULL,           -- e.g. START, RESUME, STOP, STOP_AND_FLATTEN, decision replay
    intended_end_state      TEXT,                    -- operator-lifecycle only
    state                   TEXT NOT NULL CHECK (state IN
                             ('reserved','rejected','accepted','in_progress','unknown','succeeded','failed')),
    effect_operation_id     TEXT REFERENCES effect_operations(effect_operation_id),
    receipt_id              TEXT REFERENCES receipts(receipt_id),
    created_at_ms           INTEGER NOT NULL,
    updated_at_ms           INTEGER NOT NULL,
    -- cross-bot run link is rejected: a non-null run_id must belong to this
    -- same strategy_instance_id (SQLite leaves a multi-column FK unchecked
    -- when any member is NULL, so a pre-run command's null run_id passes):
    FOREIGN KEY (strategy_instance_id, run_id) REFERENCES runs(strategy_instance_id, run_id)
);
-- unique content-addressed command identity within the authority generation (§9.4):
CREATE UNIQUE INDEX ux_commands_idempotency
    ON commands(authority_generation, idempotency_key);
CREATE INDEX ix_commands_updated_at ON commands(updated_at_ms DESC, command_id DESC);
CREATE INDEX ix_commands_strategy_updated_at
    ON commands(strategy_instance_id, updated_at_ms DESC, command_id DESC);

-- ============================================================
-- effect_operations — Clerk-owned ENTER/EXIT/cancel/recovery work (§7)
-- ============================================================
CREATE TABLE effect_operations (
    effect_operation_id     TEXT PRIMARY KEY,
    authority_generation    INTEGER NOT NULL,
    idempotency_key         TEXT NOT NULL,           -- Clerk effect idempotency identity, distinct from command's
    command_id              TEXT NOT NULL REFERENCES commands(command_id),
    strategy_instance_id    TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    run_id                  TEXT,                    -- see composite FK below
    kind                    TEXT NOT NULL CHECK (kind IN
                             ('ENTER','EXIT','CANCEL','RECONCILE','STOP','STOP_AND_FLATTEN')),
    state                   TEXT NOT NULL CHECK (state IN
                             ('reserved','rejected','accepted','in_progress','unknown','succeeded','failed')),
    custody_owner           TEXT NOT NULL DEFAULT 'ACCOUNT_CLERK',
    created_at_ms           INTEGER NOT NULL,
    updated_at_ms           INTEGER NOT NULL,
    terminal_receipt_id     TEXT REFERENCES receipts(receipt_id),
    -- operation-claim CAS fields (Scope D): a durable owner + unique fencing
    -- token claimed before broker contact; all null while unclaimed:
    claim_owner              TEXT,
    claim_token              TEXT,
    claimed_at_ms            INTEGER,
    claim_expires_at_ms      INTEGER,
    -- a claim is all-null (unclaimed) or fully populated with a real
    -- expiry window — never partially populated (open-pr-review-2026-08-05.md
    -- "Require operation claims to be all-null or complete"):
    CHECK (
        (claim_owner IS NULL AND claim_token IS NULL
            AND claimed_at_ms IS NULL AND claim_expires_at_ms IS NULL)
        OR
        (claim_owner IS NOT NULL AND claim_token IS NOT NULL
            AND claimed_at_ms IS NOT NULL AND claim_expires_at_ms > claimed_at_ms)
    ),
    FOREIGN KEY (strategy_instance_id, run_id) REFERENCES runs(strategy_instance_id, run_id)
);
-- unique Clerk effect idempotency identity (§9.4):
CREATE UNIQUE INDEX ux_effect_operations_idempotency
    ON effect_operations(authority_generation, idempotency_key);
-- unique fencing token while claimed (§9.4 extension, Scope D):
CREATE UNIQUE INDEX ux_effect_operations_claim_token
    ON effect_operations(claim_token) WHERE claim_token IS NOT NULL;
CREATE INDEX ix_effect_operations_updated_at
    ON effect_operations(updated_at_ms DESC, effect_operation_id DESC);
CREATE INDEX ix_effect_operations_strategy_updated_at
    ON effect_operations(strategy_instance_id, updated_at_ms DESC, effect_operation_id DESC);
-- keyset-paginated by created_at_ms, not updated_at_ms (#1396 P2): the
-- latter is mutated by every fold, so an operation below the first page
-- could jump above the anchor mid-traversal and vanish from later pages.
CREATE INDEX ix_effect_operations_created_at
    ON effect_operations(created_at_ms DESC, effect_operation_id DESC);
CREATE INDEX ix_effect_operations_strategy_created_at
    ON effect_operations(strategy_instance_id, created_at_ms DESC, effect_operation_id DESC);

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
CREATE INDEX ix_orders_effect_operation_id ON orders(effect_operation_id);

-- Immutable order provenance lives on orders.effect_operation_id. Later
-- operations acquire resolution custody through replayable links instead of
-- re-parenting the order and destroying its origin.
CREATE TABLE operation_order_links (
    effect_operation_id      TEXT NOT NULL REFERENCES effect_operations(effect_operation_id),
    order_ref                TEXT NOT NULL REFERENCES orders(order_ref),
    role                     TEXT NOT NULL CHECK (role IN ('ENTRY','REDUCING')),
    linked_at_ms             INTEGER NOT NULL,
    PRIMARY KEY (effect_operation_id, order_ref)
);
CREATE UNIQUE INDEX ux_operation_order_links_one_reducing
    ON operation_order_links(effect_operation_id) WHERE role = 'REDUCING';
CREATE INDEX ix_operation_order_links_order_ref
    ON operation_order_links(order_ref);

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
    execution_id             TEXT,                   -- Alpaca execution id; null only for cumulative recovery
    evidence_source          TEXT NOT NULL DEFAULT 'cumulative_recovery'
                              CHECK (evidence_source IN ('websocket','activity_recovery','cumulative_recovery')),
    event_kind               TEXT NOT NULL DEFAULT 'fill'
                              CHECK (event_kind IN ('fill','correction')),
    superseded_execution_ref TEXT,                   -- correction target; original execution remains auditable
    fee                      REAL,
    fee_fidelity             TEXT NOT NULL DEFAULT 'not_reported'
                              CHECK (fee_fidelity IN ('reported','not_reported')),
    source_event_at_ms       INTEGER,                 -- Alpaca's fill timestamp, when supplied
    clerk_observed_at_ms     INTEGER NOT NULL,
    recorded_at_ms           INTEGER NOT NULL,
    recorded_transition_sequence INTEGER NOT NULL REFERENCES custody_transitions(sequence)
);
-- Websocket/activity executions are identity-deduplicated independently of
-- the legacy synthesized ``fill_id`` used by cumulative recovery.
CREATE UNIQUE INDEX ux_fills_execution_id ON fills(execution_id)
    WHERE execution_id IS NOT NULL;

-- ============================================================
-- external_orders — broker orders outside a registered bot namespace
-- ============================================================
CREATE TABLE external_orders (
    external_order_id        TEXT PRIMARY KEY,
    broker_order_id          TEXT NOT NULL,
    client_order_id          TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    side                     TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty                      REAL NOT NULL,
    order_type               TEXT NOT NULL,
    limit_price              REAL,
    stop_price               REAL,
    filled_avg_price         REAL,
    observed_at_ms           INTEGER NOT NULL,
    acknowledged_at_ms       INTEGER,
    ack_operator             TEXT,
    evidence_refs_json       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_external_orders_broker_order_id
    ON external_orders(broker_order_id);

-- ============================================================
-- bot_config — complete immutable configuration for a registered bot
-- ============================================================
CREATE TABLE bot_config (
    strategy_instance_id     TEXT PRIMARY KEY REFERENCES strategy_instances(strategy_instance_id),
    strategy_key             TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    config_json              TEXT NOT NULL,
    config_hash              TEXT NOT NULL,
    created_at_ms            INTEGER NOT NULL
);

-- ============================================================
-- decision_receipts — bounded per-bot decision evidence, outside custody log
-- ============================================================
CREATE TABLE decision_receipts (
    strategy_instance_id     TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
    seq                      INTEGER NOT NULL,
    outcome                  TEXT NOT NULL,
    symbol                   TEXT,
    intent_id                TEXT,
    order_ref                TEXT REFERENCES orders(order_ref),
    observed_at_ms           INTEGER NOT NULL,
    facts_json               TEXT NOT NULL,
    PRIMARY KEY (strategy_instance_id, seq)
);
CREATE INDEX ix_decision_receipts_strategy_observed_at
    ON decision_receipts(strategy_instance_id, observed_at_ms DESC, seq DESC);

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
    evidence_refs_json         TEXT,
    -- scope is truthfully coupled to bot identity, not just documented by a
    -- comment: BOT requires a strategy instance, ACCOUNT_CLERK forbids one.
    CHECK ((scope = 'BOT' AND strategy_instance_id IS NOT NULL)
        OR (scope = 'ACCOUNT_CLERK' AND strategy_instance_id IS NULL))
);
CREATE UNIQUE INDEX ux_holds_one_active_cause
    ON holds(scope, reason_code, COALESCE(strategy_instance_id, ''))
    WHERE state = 'ACTIVE';
CREATE INDEX ix_holds_active_strategy_opened_at
    ON holds(strategy_instance_id, opened_at_ms DESC) WHERE state = 'ACTIVE';

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
    facts_json                TEXT NOT NULL,
    -- same truthful scope/identity coupling as holds, above:
    CHECK ((scope = 'BOT' AND strategy_instance_id IS NOT NULL)
        OR (scope = 'ACCOUNT_CLERK' AND strategy_instance_id IS NULL))
);
CREATE UNIQUE INDEX ux_uncertainties_one_active_cause
    ON uncertainties(scope, reason_code, COALESCE(strategy_instance_id, ''))
    WHERE resolved_at_ms IS NULL;
CREATE INDEX ix_uncertainties_active_strategy_observed_at
    ON uncertainties(strategy_instance_id, observed_at_ms DESC)
    WHERE resolved_at_ms IS NULL;

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
CREATE INDEX ix_reconciliations_attempted_at
    ON reconciliations(attempted_at_ms DESC);
CREATE INDEX ix_reconciliations_effect_attempted_at
    ON reconciliations(effect_operation_id, attempted_at_ms DESC);

-- ============================================================
-- receipts — permanent terminal command/effect proof; insert once
-- ============================================================
CREATE TABLE receipts (
    receipt_id                TEXT PRIMARY KEY,
    command_id                 TEXT REFERENCES commands(command_id),
    effect_operation_id        TEXT REFERENCES effect_operations(effect_operation_id),
    terminal_state              TEXT NOT NULL CHECK (terminal_state IN ('succeeded','failed','rejected')),
    summary_code                 TEXT NOT NULL,          -- stable code; prose comes from the closed registry (§11 rule 5)
    proof_reference               TEXT,
    recorded_at_ms                 INTEGER NOT NULL,
    facts_json                      TEXT
);
CREATE INDEX ix_receipts_recorded_at ON receipts(recorded_at_ms DESC);
CREATE INDEX ix_receipts_command_recorded_at
    ON receipts(command_id, recorded_at_ms DESC);

-- ============================================================
-- custody_transitions — THE canonical, append-only, hash-chained,
-- operation-first log. Columns are the PRD §11 list verbatim.
-- ============================================================
CREATE TABLE custody_transitions (
    sequence                  INTEGER PRIMARY KEY AUTOINCREMENT,   -- serialization order, not broker time
    prev_hash                 TEXT NOT NULL,            -- literal 'GENESIS' sentinel for sequence 1, never null
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
-- order_ref is scanned by both the §3c ack idempotency guard and recovery's
-- transitions_for_order (#1377) — an index keeps both O(log n), not O(n):
CREATE INDEX ix_custody_transitions_order_ref ON custody_transitions(order_ref);
CREATE INDEX ix_custody_transitions_strategy_sequence
    ON custody_transitions(strategy_instance_id, sequence DESC);
CREATE INDEX ix_custody_transitions_effect_sequence
    ON custody_transitions(effect_operation_id, sequence DESC);
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
END;

-- ============================================================
-- Terminal-state regression backstops (§9.4 "terminal outcomes cannot
-- regress to nonterminal"): a DB-enforced floor under the fold's own
-- discipline, same posture as the immutability triggers above.
-- ============================================================
CREATE TRIGGER trg_commands_terminal_state_immutable
BEFORE UPDATE OF state ON commands
WHEN OLD.state IN ('succeeded','failed','rejected') AND NEW.state != OLD.state
BEGIN
    SELECT RAISE(ABORT, 'commands.state cannot change once terminal');
END;

CREATE TRIGGER trg_effect_operations_terminal_state_immutable
BEFORE UPDATE OF state ON effect_operations
WHEN OLD.state IN ('succeeded','failed','rejected') AND NEW.state != OLD.state
BEGIN
    SELECT RAISE(ABORT, 'effect_operations.state cannot change once terminal');
END;\
"""


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set (§2) to a freshly-opened connection."""
    for statement in PRAGMA_STATEMENTS:
        conn.execute(statement)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes/triggers from ``SCHEMA_DDL`` (fresh init only)."""
    conn.executescript(SCHEMA_DDL)


# Registered upgrades keyed by the ``schema_version`` they start from, each an
# additive-only DDL block (never a column/table rewrite) taking that exact
# prior version to the next. #1396 review: the v4 SCHEMA_VERSION bump shipped
# with no upgrade path, so an account initialized on v4 could no longer be
# opened or pass cutover-plan's exact-match check. v4 -> v5 is index-only
# (byte-for-byte the CREATE INDEX statements the v5 bump added over v4 — see
# docs/architecture/alpaca-clerk-sqlite-pinned-contracts.md §3's history);
# ``IF NOT EXISTS`` makes every statement safe to replay.
#
# v6 -> v7 is intentionally narrower than the historical index-only upgrades:
# it adds the execution-ledger columns and tables only after proving that the
# authority contains no operational rows. A data-bearing v6 authority cannot
# be made truthful by assigning synthetic execution provenance, so it fails
# closed and remains untouched for the fresh-generation cutover.
_V6_OPERATIONAL_TABLES: tuple[str, ...] = (
    "strategy_instances",
    "runs",
    "commands",
    "effect_operations",
    "orders",
    "operation_order_links",
    "fills",
    "positions",
    "holds",
    "uncertainties",
    "reconciliations",
    "receipts",
    "custody_transitions",
    "mirror_fence",
)


def v6_operational_tables_with_rows(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return occupied v6 operational tables without changing authority state."""
    return tuple(
        table
        for table in _V6_OPERATIONAL_TABLES
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
    )


def _require_empty_v6_authority(conn: sqlite3.Connection) -> None:
    """Reject a v6 upgrade unless every operational projection is empty.

    ``control_meta`` is deliberately excluded: its singleton establishes the
    authority identity and is required to advance its schema version. Every
    other v6 table is an operational fact or its materialized projection;
    checking all of them, rather than only ``fills`` or the control revision,
    makes an empty claim durable and tamper-resistant enough for this bounded
    additive migration.
    """
    occupied = v6_operational_tables_with_rows(conn)
    if occupied:
        raise ValueError(
            "v6 -> v7 migration requires an empty authority; operational rows exist in "
            + ", ".join(occupied)
        )


_V6_TO_V7_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE fills ADD COLUMN execution_id TEXT",
    "ALTER TABLE fills ADD COLUMN evidence_source TEXT NOT NULL "
    "DEFAULT 'cumulative_recovery' CHECK "
    "(evidence_source IN ('websocket','activity_recovery','cumulative_recovery'))",
    "ALTER TABLE fills ADD COLUMN event_kind TEXT NOT NULL DEFAULT 'fill' "
    "CHECK (event_kind IN ('fill','correction'))",
    "ALTER TABLE fills ADD COLUMN superseded_execution_ref TEXT",
    "ALTER TABLE fills ADD COLUMN fee REAL",
    "ALTER TABLE fills ADD COLUMN fee_fidelity TEXT NOT NULL DEFAULT 'not_reported' "
    "CHECK (fee_fidelity IN ('reported','not_reported'))",
    "CREATE UNIQUE INDEX ux_fills_execution_id ON fills(execution_id) "
    "WHERE execution_id IS NOT NULL",
    """CREATE TABLE external_orders (
        external_order_id        TEXT PRIMARY KEY,
        broker_order_id          TEXT NOT NULL,
        client_order_id          TEXT NOT NULL,
        symbol                   TEXT NOT NULL,
        side                     TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
        qty                      REAL NOT NULL,
        price                    REAL,
        observed_at_ms           INTEGER NOT NULL,
        acknowledged_at_ms       INTEGER,
        ack_operator             TEXT,
        evidence_refs_json       TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX ux_external_orders_broker_order_id "
    "ON external_orders(broker_order_id)",
    """CREATE TABLE bot_config (
        strategy_instance_id     TEXT PRIMARY KEY REFERENCES strategy_instances(strategy_instance_id),
        strategy_key             TEXT NOT NULL,
        display_name             TEXT NOT NULL,
        config_json              TEXT NOT NULL,
        config_hash              TEXT NOT NULL,
        created_at_ms            INTEGER NOT NULL
    )""",
    """CREATE TABLE decision_receipts (
        strategy_instance_id     TEXT NOT NULL REFERENCES strategy_instances(strategy_instance_id),
        seq                      INTEGER NOT NULL,
        outcome                  TEXT NOT NULL,
        symbol                   TEXT,
        intent_id                TEXT,
        order_ref                TEXT REFERENCES orders(order_ref),
        observed_at_ms           INTEGER NOT NULL,
        facts_json               TEXT NOT NULL,
        PRIMARY KEY (strategy_instance_id, seq)
    )""",
    "CREATE INDEX ix_decision_receipts_strategy_observed_at "
    "ON decision_receipts(strategy_instance_id, observed_at_ms DESC, seq DESC)",
)


SCHEMA_MIGRATIONS: dict[int, tuple[str, ...]] = {
    4: (
        "CREATE INDEX IF NOT EXISTS ix_runs_started_at ON runs(started_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_runs_strategy_started_at "
        "ON runs(strategy_instance_id, started_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_commands_updated_at "
        "ON commands(updated_at_ms DESC, command_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_commands_strategy_updated_at "
        "ON commands(strategy_instance_id, updated_at_ms DESC, command_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_effect_operations_updated_at "
        "ON effect_operations(updated_at_ms DESC, effect_operation_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_effect_operations_strategy_updated_at "
        "ON effect_operations(strategy_instance_id, updated_at_ms DESC, effect_operation_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_orders_effect_operation_id ON orders(effect_operation_id)",
        "CREATE INDEX IF NOT EXISTS ix_holds_active_strategy_opened_at "
        "ON holds(strategy_instance_id, opened_at_ms DESC) WHERE state = 'ACTIVE'",
        "CREATE INDEX IF NOT EXISTS ix_uncertainties_active_strategy_observed_at "
        "ON uncertainties(strategy_instance_id, observed_at_ms DESC) WHERE resolved_at_ms IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_reconciliations_attempted_at "
        "ON reconciliations(attempted_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_reconciliations_effect_attempted_at "
        "ON reconciliations(effect_operation_id, attempted_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_receipts_recorded_at ON receipts(recorded_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_receipts_command_recorded_at "
        "ON receipts(command_id, recorded_at_ms DESC)",
        "CREATE INDEX IF NOT EXISTS ix_custody_transitions_strategy_sequence "
        "ON custody_transitions(strategy_instance_id, sequence DESC)",
        "CREATE INDEX IF NOT EXISTS ix_custody_transitions_effect_sequence "
        "ON custody_transitions(effect_operation_id, sequence DESC)",
    ),
    # v5 -> v6: operation_page's keyset pagination moved to the immutable
    # created_at_ms (#1396 P2 — updated_at_ms could move a not-yet-paged row
    # above the anchor mid-traversal); these indexes keep that query covered.
    5: (
        "CREATE INDEX IF NOT EXISTS ix_effect_operations_created_at "
        "ON effect_operations(created_at_ms DESC, effect_operation_id DESC)",
        "CREATE INDEX IF NOT EXISTS ix_effect_operations_strategy_created_at "
        "ON effect_operations(strategy_instance_id, created_at_ms DESC, effect_operation_id DESC)",
    ),
    6: _V6_TO_V7_STATEMENTS,
    # v7 -> v8: retain the immutable custody transition that materialized each
    # execution and split external instruction prices from execution averages.
    # The new fills column is nullable on migrated files because SQLite cannot
    # add a NOT NULL column without a synthetic default. Legacy rows are
    # backfilled only when their transition facts name one exact execution;
    # unprovable sequence order remains unavailable rather than fabricated.
    7: (
        "ALTER TABLE fills ADD COLUMN recorded_transition_sequence INTEGER",
        "UPDATE fills SET recorded_transition_sequence = ("
        "SELECT ct.sequence FROM custody_transitions ct "
        "WHERE ct.order_ref = fills.order_ref "
        "AND fills.execution_id IS NOT NULL "
        "AND json_extract(ct.facts_json, '$.execution_id') = fills.execution_id "
        "ORDER BY ct.sequence ASC LIMIT 1)",
        "ALTER TABLE external_orders ADD COLUMN order_type TEXT",
        "ALTER TABLE external_orders ADD COLUMN limit_price REAL",
        "ALTER TABLE external_orders ADD COLUMN stop_price REAL",
        "ALTER TABLE external_orders ADD COLUMN filled_avg_price REAL",
    ),
}


def is_upgradable_to_current(version: int) -> bool:
    """Whether a registered, chained migration path reaches ``SCHEMA_VERSION``.

    Read-only callers (cutover ``plan``/verification) use this to accept a
    prior version without mutating the file being verified; the actual
    upgrade happens once via :func:`migrate_schema` when the account is next
    opened for real.
    """
    seen = version
    while seen < SCHEMA_VERSION:
        if seen not in SCHEMA_MIGRATIONS:
            return False
        seen += 1
    return True


def migrate_schema(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Apply every registered additive upgrade from ``from_version`` to ``SCHEMA_VERSION``.

    Runs inside one transaction so a partially-applied upgrade can never be
    observed: either every statement lands and ``control_meta.schema_version``
    advances, or none do. Raises ``ValueError`` for any version with no
    registered path — the caller's existing exact-match failure stays the
    fail-closed default for anything not proven safe here.
    """
    version = from_version
    conn.execute("BEGIN IMMEDIATE")
    try:
        while version < SCHEMA_VERSION:
            statements = SCHEMA_MIGRATIONS.get(version)
            if statements is None:
                raise ValueError(
                    f"no registered migration from schema_version={version} to {version + 1}"
                )
            if version == 6:
                _require_empty_v6_authority(conn)
            for statement in statements:
                conn.execute(statement)
            version += 1
        conn.execute("UPDATE control_meta SET schema_version = ? WHERE id = 1", (SCHEMA_VERSION,))
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


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
