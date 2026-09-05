"""Python-owned Postgres schema for research sweeps and studies.

These tables are owned and written by the Python service directly — the
first tables in this repository whose schema is declared here rather than
by an EF Core migration (ADR 0055). The DDL is idempotent and applied on
first use under a transaction-scoped advisory lock, and each applied
version is recorded in ``research_schema_migrations`` so a later change
ships as a new numbered statement list rather than an edit to an old one.

Cell identity is ``(search_id, params_hash)``: unique and idempotent, so a
retried or resumed search overwrites its own cells rather than appending.
A walk-forward study's per-fold sweeps are rows here too, owned through
``owner_kind`` / ``owner_id`` / ``fold_index`` / ``phase`` — which is how
Grid Search's history excludes them by ownership rather than by a flag a
caller could forget (PRD #1925 "Sweep invocation and ownership").

Version 3 adopts the Recency Chart's tables from EF Core (PRD #1927, ADR
0057): their names stay as EF created them, because the rows are migrated
in place, not regenerated.

Version 4 repairs databases that applied an early, since-edited draft of
version 1 and so never received ``leader_params_json``; the digest test in
``tests/research/persistence/test_schema.py`` now fails loudly if an applied
version is edited again.
"""

from __future__ import annotations

import asyncpg

SCHEMA_VERSION = 4
# Arbitrary but fixed: serializes concurrent first-use across FastAPI's loop
# and the worker loop so CREATE IF NOT EXISTS never races itself.
_ADVISORY_LOCK_KEY = 0x1926_0001

DDL_V1: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS research_schema_migrations (
        version        INTEGER PRIMARY KEY,
        applied_at_ms  BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_grid_searches (
        id                  TEXT PRIMARY KEY,
        owner_kind          TEXT NOT NULL DEFAULT 'user',
        owner_id            TEXT NULL,
        fold_index          INTEGER NULL,
        phase               TEXT NULL,
        strategy_key        TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        status              TEXT NOT NULL,
        attempt             INTEGER NOT NULL DEFAULT 0,
        job_id              TEXT NULL,
        created_at_ms       BIGINT NOT NULL,
        updated_at_ms       BIGINT NOT NULL,
        finished_at_ms      BIGINT NULL,
        request_json        JSONB NOT NULL,
        receipt_json        JSONB NOT NULL,
        expected_cells      INTEGER NOT NULL,
        completed_cells     INTEGER NOT NULL DEFAULT 0,
        failed_cells        INTEGER NOT NULL DEFAULT 0,
        leader_params_hash  TEXT NULL,
        leader_params_json  JSONB NULL,
        incomplete          BOOLEAN NOT NULL DEFAULT FALSE,
        failure_reason      TEXT NULL,
        CONSTRAINT ck_research_grid_searches_status
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
        CONSTRAINT ck_research_grid_searches_owner
            CHECK (owner_kind IN ('user', 'walk_forward'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_grid_searches_owner_created
        ON research_grid_searches (owner_kind, created_at_ms DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_grid_searches_strategy_symbol
        ON research_grid_searches (strategy_key, symbol)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_grid_searches_owner_id
        ON research_grid_searches (owner_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_grid_search_cells (
        search_id         TEXT NOT NULL REFERENCES research_grid_searches (id) ON DELETE CASCADE,
        params_hash       TEXT NOT NULL,
        params_json       JSONB NOT NULL,
        status            TEXT NOT NULL,
        attempt           INTEGER NOT NULL,
        total_trades      INTEGER NOT NULL DEFAULT 0,
        net_profit        DOUBLE PRECISION NULL,
        total_return_pct  DOUBLE PRECISION NULL,
        sharpe_ratio      DOUBLE PRECISION NULL,
        max_drawdown_pct  DOUBLE PRECISION NULL,
        win_rate          DOUBLE PRECISION NULL,
        bars_consumed     INTEGER NULL,
        error             TEXT NULL,
        exploratory       BOOLEAN NOT NULL DEFAULT FALSE,
        completed_at_ms   BIGINT NOT NULL,
        PRIMARY KEY (search_id, params_hash),
        CONSTRAINT ck_research_grid_search_cells_status CHECK (status IN ('completed', 'failed'))
    )
    """,
)


# Walk-forward studies (PRD #1925). A study owns 2 x folds sweeps in
# research_grid_searches (owner_kind = 'walk_forward'); the per-fold winner
# evidence and the verdict live on the study row.
DDL_V2: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS research_walk_forward_studies (
        id                  TEXT PRIMARY KEY,
        strategy_key        TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        status              TEXT NOT NULL,
        attempt             INTEGER NOT NULL DEFAULT 0,
        job_id              TEXT NULL,
        created_at_ms       BIGINT NOT NULL,
        updated_at_ms       BIGINT NOT NULL,
        finished_at_ms      BIGINT NULL,
        request_json        JSONB NOT NULL,
        receipt_json        JSONB NOT NULL,
        folds_json          JSONB NOT NULL DEFAULT '[]'::jsonb,
        verdict_json        JSONB NULL,
        expected_backtests  INTEGER NOT NULL,
        completed_backtests INTEGER NOT NULL DEFAULT 0,
        incomplete          BOOLEAN NOT NULL DEFAULT FALSE,
        failure_reason      TEXT NULL,
        CONSTRAINT ck_research_walk_forward_studies_status
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_walk_forward_studies_created
        ON research_walk_forward_studies (created_at_ms DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_walk_forward_studies_strategy_symbol
        ON research_walk_forward_studies (strategy_key, symbol)
    """,
)

# Version 3 adopts the Recency Chart's four tables from EF Core (ADR 0057). The
# statements reproduce the tables EF created — names, types, identity columns,
# indexes, constraints — so an upgraded database is untouched (every statement
# is IF NOT EXISTS) and a fresh Python-only database gets the same shape.
DDL_V3: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS "RecencyLaunches" (
        "Id"            VARCHAR(64) NOT NULL CONSTRAINT "PK_RecencyLaunches" PRIMARY KEY,
        "ConfigJson"    JSONB NOT NULL,
        "ExpectedRuns"  INTEGER NOT NULL,
        "SucceededRuns" INTEGER NOT NULL,
        "FailedRuns"    INTEGER NOT NULL,
        "Status"        VARCHAR(16) NOT NULL,
        "CreatedAtMs"   BIGINT NOT NULL,
        "CompletedAtMs" BIGINT NULL,
        "DeletedAtMs"   BIGINT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "RecencyRuns" (
        "Id"              INTEGER GENERATED BY DEFAULT AS IDENTITY CONSTRAINT "PK_RecencyRuns" PRIMARY KEY,
        "RecencyLaunchId" VARCHAR(64) NOT NULL
            CONSTRAINT "FK_RecencyRuns_RecencyLaunches_RecencyLaunchId" REFERENCES "RecencyLaunches"("Id") ON DELETE CASCADE,
        "StrategyKey"     VARCHAR(64) NOT NULL,
        "Symbol"          VARCHAR(20) NOT NULL,
        "ParamsJson"      JSONB NOT NULL,
        "ParamsHash"      VARCHAR(64) NOT NULL,
        "StudyId"         INTEGER NULL,
        "TotalPnl"        NUMERIC(18, 8) NOT NULL,
        "Sharpe"          NUMERIC(18, 8) NULL,
        "CreatedAtMs"     BIGINT NOT NULL,
        "DeletedAtMs"     BIGINT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "RecencyTrades" (
        "Id"              INTEGER GENERATED BY DEFAULT AS IDENTITY CONSTRAINT "PK_RecencyTrades" PRIMARY KEY,
        "RecencyRunId"    INTEGER NOT NULL
            CONSTRAINT "FK_RecencyTrades_RecencyRuns_RecencyRunId" REFERENCES "RecencyRuns"("Id") ON DELETE CASCADE,
        "Fingerprint"     VARCHAR(64) NOT NULL,
        "EntryMs"         BIGINT NOT NULL,
        "ExitMs"          BIGINT NOT NULL,
        "PnlPts"          NUMERIC(18, 8) NOT NULL,
        "PnlPct"          NUMERIC(18, 8) NOT NULL,
        "Quantity"        NUMERIC(18, 8) NOT NULL,
        "Pnl"             NUMERIC(18, 8) NOT NULL,
        "HoldingSessions" INTEGER NOT NULL,
        "IsSyntheticExit" BOOLEAN NOT NULL DEFAULT FALSE,
        "SignalReason"    TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "RecencyTradeMemberships" (
        "Id"             INTEGER GENERATED BY DEFAULT AS IDENTITY CONSTRAINT "PK_RecencyTradeMemberships" PRIMARY KEY,
        "RecencyTradeId" INTEGER NOT NULL
            CONSTRAINT "FK_RecencyTradeMemberships_RecencyTrades_RecencyTradeId" REFERENCES "RecencyTrades"("Id") ON DELETE CASCADE,
        "RecencyRunId"   INTEGER NOT NULL
            CONSTRAINT "FK_RecencyTradeMemberships_RecencyRuns_RecencyRunId" REFERENCES "RecencyRuns"("Id") ON DELETE CASCADE
    )
    """,
    'CREATE INDEX IF NOT EXISTS "IX_RecencyLaunches_DeletedAtMs" ON "RecencyLaunches" ("DeletedAtMs")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyLaunches_Status" ON "RecencyLaunches" ("Status")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyRuns_DeletedAtMs" ON "RecencyRuns" ("DeletedAtMs")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyRuns_RecencyLaunchId" ON "RecencyRuns" ("RecencyLaunchId")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyRuns_Symbol_StrategyKey" ON "RecencyRuns" ("Symbol", "StrategyKey")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyTradeMemberships_RecencyRunId" ON "RecencyTradeMemberships" ("RecencyRunId")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "IX_RecencyTradeMemberships_RecencyTradeId_RecencyRunId" ON "RecencyTradeMemberships" ("RecencyTradeId", "RecencyRunId")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyTrades_EntryMs" ON "RecencyTrades" ("EntryMs")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "IX_RecencyTrades_Fingerprint" ON "RecencyTrades" ("Fingerprint")',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyTrades_RecencyRunId" ON "RecencyTrades" ("RecencyRunId")',
)

# Version 4 — the column an early draft of version 1 lacked (see the module docstring).
DDL_V4: tuple[str, ...] = ("ALTER TABLE research_grid_searches ADD COLUMN IF NOT EXISTS leader_params_json JSONB NULL",)

VERSIONED_DDL: tuple[tuple[int, tuple[str, ...]], ...] = ((1, DDL_V1), (2, DDL_V2), (3, DDL_V3), (4, DDL_V4))


async def ensure_schema(conn: asyncpg.Connection) -> None:
    """Apply every schema version not yet recorded, under one advisory lock. Idempotent.

    Versions already in ``research_schema_migrations`` are skipped outright:
    even ``IF NOT EXISTS`` DDL takes share locks on the tables it names, and
    re-running it on every loop's first use deadlocks against writers holding
    row locks in those tables.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _ADVISORY_LOCK_KEY)
        applied: set[int] = set()
        if await conn.fetchval("SELECT to_regclass('research_schema_migrations')") is not None:
            applied = {row["version"] for row in await conn.fetch("SELECT version FROM research_schema_migrations")}
        for version, statements in VERSIONED_DDL:
            if version in applied:
                continue
            for statement in statements:
                await conn.execute(statement)
            await conn.execute(
                """
                INSERT INTO research_schema_migrations (version, applied_at_ms)
                VALUES ($1, (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::BIGINT)
                ON CONFLICT (version) DO NOTHING
                """,
                version,
            )
