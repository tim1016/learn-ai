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

Version 5 declares the backtest-run tables (PRD #1929, ADR 0058): every
engine and LEAN sidecar run, its trades, and the parity verdict comparing a
Python run against its LEAN companion. These replace the EF-owned
``StrategyExecutions`` / ``BacktestTrades`` / ``ParityVerdicts`` tables,
whose rows are not migrated. The run's start and end are date-anchored
values stored as ``int64 ms UTC`` at ET midnight of the date (the same anchor
the Recency window uses; a run may start on a non-trading day, so the
calendar's session open is not always defined). Version 5 also nulls every
``RecencyRuns.StudyId``: those ids named rows of the old table, and under a
fresh identity sequence they would resolve to unrelated runs.

Version 7 adds immutable Validation Golden Run cases and their append-only
human reviews.  A case freezes the exact scientific scope copied from one
Python history run.  A review preserves the computed parity evidence as a
separate fact from the human decision, including explicit risk acceptance
when evidence is missing or unhealthy.

Version 8 closes the review invariant at rest: only an accepting review may
carry an explicitly authorized program version.

Version 9 gives ``RecencyLaunches`` the attempt-fence columns
(``Attempt`` / ``JobId`` / ``Incomplete`` / ``FailureReason``) so a resumed
launch binds a new job id to the existing durable record through the shared
fence contract, as Grid Search and Walk-Forward Study rows always have
(#1938).
"""

from __future__ import annotations

import asyncpg

SCHEMA_VERSION = 13
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

# Version 5 — backtest runs, their trades and parity verdicts (PRD #1929, ADR 0058).
DDL_V5: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS research_backtest_runs (
        id                        INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        source                    TEXT NOT NULL,
        requested_engine          TEXT NULL,
        lean_run_id               TEXT NULL,
        parity_group_id           TEXT NULL,
        strategy_name             TEXT NOT NULL,
        symbol                    TEXT NOT NULL,
        parameters_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
        start_ms                  BIGINT NOT NULL,
        end_ms                    BIGINT NOT NULL,
        timespan                  TEXT NOT NULL,
        fill_mode                 TEXT NOT NULL,
        executed_at_ms            BIGINT NOT NULL,
        duration_ms               BIGINT NOT NULL DEFAULT 0,
        total_trades              INTEGER NOT NULL,
        winning_trades            INTEGER NOT NULL,
        losing_trades             INTEGER NOT NULL,
        win_rate                  DOUBLE PRECISION NOT NULL,
        total_pnl                 DOUBLE PRECISION NOT NULL,
        initial_cash              DOUBLE PRECISION NOT NULL,
        final_equity              DOUBLE PRECISION NOT NULL,
        total_fees                DOUBLE PRECISION NOT NULL,
        max_drawdown              DOUBLE PRECISION NOT NULL DEFAULT 0,
        sharpe_ratio              DOUBLE PRECISION NULL,
        sortino_ratio             DOUBLE PRECISION NULL,
        profit_factor             DOUBLE PRECISION NULL,
        commission_per_order      DOUBLE PRECISION NULL,
        brokerage_policy          TEXT NULL,
        data_policy_json          JSONB NULL,
        lean_statistics_json      JSONB NULL,
        lean_analysis_json        JSONB NULL,
        run_verdict_json          JSONB NULL,
        verdict_version           INTEGER NULL,
        verdict_grade             TEXT NULL,
        verdict_signal            TEXT NULL,
        equity_curve_json         JSONB NULL,
        validation_analytics_json JSONB NULL,
        insight_summary_json      JSONB NULL,
        metric_documentation_json JSONB NULL,
        notes                     TEXT NULL,
        CONSTRAINT ck_research_backtest_runs_source
            CHECK (source IN ('engine', 'lean-sidecar')),
        CONSTRAINT ck_research_backtest_runs_requested_engine
            CHECK (requested_engine IS NULL OR requested_engine IN ('python', 'lean', 'both'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_backtest_runs_executed
        ON research_backtest_runs (executed_at_ms DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_backtest_runs_source_executed
        ON research_backtest_runs (source, executed_at_ms DESC, id DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_research_backtest_runs_lean_run_id
        ON research_backtest_runs (lean_run_id) WHERE lean_run_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_backtest_runs_parity_group
        ON research_backtest_runs (parity_group_id) WHERE parity_group_id IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS research_backtest_run_trades (
        id                INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        run_id            INTEGER NOT NULL REFERENCES research_backtest_runs (id) ON DELETE CASCADE,
        trade_number      INTEGER NOT NULL,
        entry_ms          BIGINT NOT NULL,
        exit_ms           BIGINT NOT NULL,
        entry_price       DOUBLE PRECISION NOT NULL,
        exit_price        DOUBLE PRECISION NOT NULL,
        quantity          DOUBLE PRECISION NOT NULL,
        pnl               DOUBLE PRECISION NOT NULL,
        signal_reason     TEXT NOT NULL DEFAULT '',
        is_synthetic_exit BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_backtest_run_trades_run_entry
        ON research_backtest_run_trades (run_id, entry_ms, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_parity_verdicts (
        id               INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        parity_group_id  TEXT NOT NULL UNIQUE,
        left_run_id      INTEGER NOT NULL REFERENCES research_backtest_runs (id) ON DELETE CASCADE,
        right_run_id     INTEGER NULL REFERENCES research_backtest_runs (id) ON DELETE SET NULL,
        verdict_version  INTEGER NOT NULL,
        status           TEXT NOT NULL,
        verdict_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at_ms    BIGINT NOT NULL,
        CONSTRAINT ck_research_parity_verdicts_status
            CHECK (status IN ('pending', 'unavailable', 'agree', 'diverged', 'run_failed', 'persist_failed'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_parity_verdicts_left
        ON research_parity_verdicts (left_run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_parity_verdicts_right
        ON research_parity_verdicts (right_run_id)
    """,
    # The old study ids no longer name anything; a null reference renders as no link.
    'UPDATE "RecencyRuns" SET "StudyId" = NULL WHERE "StudyId" IS NOT NULL',
)

# Version 6 — a parity verdict goes with either of its runs. Version 5 kept the
# verdict and nulled ``right_run_id`` when the LEAN companion was hard-deleted,
# which left a terminal "agree" on the Python run after the evidence it was
# judged against was gone. The retired .NET schema cascaded on both sides.
DDL_V6: tuple[str, ...] = (
    """
    ALTER TABLE research_parity_verdicts
        DROP CONSTRAINT IF EXISTS research_parity_verdicts_right_run_id_fkey,
        ADD CONSTRAINT research_parity_verdicts_right_run_id_fkey
            FOREIGN KEY (right_run_id) REFERENCES research_backtest_runs (id) ON DELETE CASCADE
    """,
)

# Version 7 — immutable, referenceable Golden Validation cases.  The two
# ledgers are deliberately append-only: a later judgment is another review,
# never an edit that makes a historical mismatch appear to have agreed.
DDL_V7: tuple[str, ...] = (
    "ALTER TABLE research_backtest_runs ADD COLUMN IF NOT EXISTS program_version TEXT NULL",
    "ALTER TABLE research_backtest_runs ADD COLUMN IF NOT EXISTS execution_config_json JSONB NULL",
    """
    CREATE TABLE IF NOT EXISTS research_validation_golden_runs (
        id                   INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        source_run_id        INTEGER NOT NULL UNIQUE
            REFERENCES research_backtest_runs (id) ON DELETE RESTRICT,
        command_id           TEXT NOT NULL UNIQUE,
        command_sha256       TEXT NOT NULL,
        label                TEXT NULL,
        strategy_name        TEXT NOT NULL,
        symbol               TEXT NOT NULL,
        validation_case_json JSONB NOT NULL,
        case_sha256          TEXT NOT NULL,
        rationale            TEXT NOT NULL,
        designated_by        TEXT NOT NULL,
        designated_at_ms     BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_validation_golden_runs_designated
        ON research_validation_golden_runs (designated_at_ms DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_validation_golden_runs_strategy_symbol
        ON research_validation_golden_runs (strategy_name, symbol)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_validation_reviews (
        id                         INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        golden_run_id              INTEGER NOT NULL
            REFERENCES research_validation_golden_runs (id) ON DELETE RESTRICT,
        command_id                 TEXT NOT NULL UNIQUE,
        command_sha256             TEXT NOT NULL,
        expected_evidence_revision TEXT NOT NULL,
        decision                   TEXT NOT NULL,
        classification             TEXT NULL,
        evidence_state             TEXT NOT NULL,
        parity_verdict_id           INTEGER NULL
            REFERENCES research_parity_verdicts (id) ON DELETE RESTRICT,
        evidence_json              JSONB NOT NULL,
        evidence_sha256            TEXT NOT NULL,
        reason                     TEXT NOT NULL,
        quantconnect_backtest_id   TEXT NULL,
        authorized_program_version TEXT NULL,
        reviewed_by                TEXT NOT NULL,
        reviewed_at_ms             BIGINT NOT NULL,
        CONSTRAINT ck_research_golden_validation_reviews_decision
            CHECK (decision IN ('accept', 'reject')),
        CONSTRAINT ck_research_golden_validation_reviews_classification
            CHECK (
                (decision = 'reject' AND classification IS NULL)
                OR
                (decision = 'accept' AND classification IN (
                    'engine_agreement', 'reviewed_deviations', 'manual_override'
                ))
            ),
        CONSTRAINT ck_research_golden_validation_reviews_evidence_state
            CHECK (evidence_state IN (
                'agreement', 'deviations', 'pending', 'unavailable',
                'run_failed', 'persist_failed', 'missing', 'corrupt'
            )),
        CONSTRAINT ck_research_golden_validation_reviews_authorized_program_version
            CHECK (authorized_program_version IS NULL OR btrim(authorized_program_version) <> '')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_validation_reviews_case
        ON research_golden_validation_reviews (golden_run_id, reviewed_at_ms DESC, id DESC)
    """,
    """
    CREATE OR REPLACE FUNCTION reject_golden_validation_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'Golden Validation evidence is append-only';
    END;
    $$
    """,
    """
    CREATE TRIGGER research_validation_golden_runs_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_validation_golden_runs
        FOR EACH ROW EXECUTE FUNCTION reject_golden_validation_mutation()
    """,
    """
    CREATE TRIGGER research_golden_validation_reviews_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_validation_reviews
        FOR EACH ROW EXECUTE FUNCTION reject_golden_validation_mutation()
    """,
)

DDL_V8: tuple[str, ...] = (
    """
    ALTER TABLE research_golden_validation_reviews
        ADD CONSTRAINT ck_research_golden_validation_reviews_authorized_only_on_accept
        CHECK (decision = 'accept' OR authorized_program_version IS NULL)
        NOT VALID
    """,
)

# Version 9 gives "RecencyLaunches" the attempt-fence columns (ADR 0055 §4)
# so a resumed launch claims a new attempt generation bound to a new job id
# (#1938), exactly as research_grid_searches / research_walk_forward_studies
# already do. Pre-existing rows read as attempt 0 with no job id, which the
# fence treats as an interrupted launch nothing is writing.
DDL_V9: tuple[str, ...] = (
    'ALTER TABLE "RecencyLaunches" ADD COLUMN IF NOT EXISTS "Attempt" INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE "RecencyLaunches" ADD COLUMN IF NOT EXISTS "JobId" VARCHAR(64) NULL',
    'ALTER TABLE "RecencyLaunches" ADD COLUMN IF NOT EXISTS "Incomplete" BOOLEAN NOT NULL DEFAULT FALSE',
    'ALTER TABLE "RecencyLaunches" ADD COLUMN IF NOT EXISTS "FailureReason" TEXT NULL',
    'CREATE INDEX IF NOT EXISTS "IX_RecencyLaunches_JobId" ON "RecencyLaunches" ("JobId")',
)

# Nullable on purpose: no current conventions are backfilled into old evidence.
DDL_V10: tuple[str, ...] = (
    "ALTER TABLE research_backtest_runs ADD COLUMN IF NOT EXISTS evidence_provenance_json JSONB NULL",
)

# Version 11 — Golden Search (#2696, ADR 0074): studies, their evaluation
# cache and trial ledger, the cross-study exposure ledger, and the immutable
# qualified versions with their per-(program, symbol) default pointer. Every
# fact a later review relies on is append-only: hiding a study never deletes
# its trials, exposures or qualification, and a qualification is revoked or
# re-proved by another event, never by an edit.
DDL_V11: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS research_golden_search_studies (
        id                   TEXT PRIMARY KEY,
        parent_study_id      TEXT NULL
            REFERENCES research_golden_search_studies (id) ON DELETE RESTRICT,
        strategy_key         TEXT NOT NULL,
        symbol               TEXT NOT NULL,
        state                TEXT NOT NULL,
        revision             INTEGER NOT NULL DEFAULT 0,
        status               TEXT NOT NULL DEFAULT 'idle',
        attempt              INTEGER NOT NULL DEFAULT 0,
        job_id               TEXT NULL,
        pending_stage        TEXT NULL,
        stage_token          TEXT NULL,
        created_at_ms        BIGINT NOT NULL,
        updated_at_ms        BIGINT NOT NULL,
        finished_at_ms       BIGINT NULL,
        protocol_json        JSONB NOT NULL,
        protocol_hash        TEXT NOT NULL,
        receipt_json         JSONB NOT NULL,
        results_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
        candidate_key        TEXT NULL,
        exam_locked          BOOLEAN NOT NULL DEFAULT FALSE,
        decision_json        JSONB NULL,
        budget_cap           INTEGER NOT NULL,
        consumed_evaluations INTEGER NOT NULL DEFAULT 0,
        cache_hits           INTEGER NOT NULL DEFAULT 0,
        invalid_points       INTEGER NOT NULL DEFAULT 0,
        incomplete           BOOLEAN NOT NULL DEFAULT FALSE,
        failure_reason       TEXT NULL,
        hidden               BOOLEAN NOT NULL DEFAULT FALSE,
        CONSTRAINT ck_research_golden_search_studies_state CHECK (state IN (
            'locked', 'search_running', 'awaiting_validation', 'validation_running',
            'awaiting_candidate', 'candidate_locked', 'exam_running', 'awaiting_review',
            'qualification_pending', 'approved', 'qualification_failed', 'retained', 'closed'
        )),
        CONSTRAINT ck_research_golden_search_studies_status CHECK (status IN (
            'idle', 'queued', 'running', 'completed', 'failed', 'cancelled'
        )),
        CONSTRAINT ck_research_golden_search_studies_pending_stage CHECK (
            pending_stage IS NULL OR pending_stage IN ('search', 'validation', 'exam', 'qualification')
        ),
        CONSTRAINT ck_research_golden_search_studies_budget CHECK (
            budget_cap BETWEEN 1 AND 5000 AND consumed_evaluations BETWEEN 0 AND budget_cap
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_search_studies_subject
        ON research_golden_search_studies (strategy_key, symbol, created_at_ms DESC)
    """,
    "CREATE INDEX IF NOT EXISTS ix_research_golden_search_studies_job ON research_golden_search_studies (job_id)",
    """
    CREATE TABLE IF NOT EXISTS research_golden_search_evaluations (
        study_id         TEXT NOT NULL
            REFERENCES research_golden_search_studies (id) ON DELETE RESTRICT,
        evaluation_key   TEXT NOT NULL,
        point_hash       TEXT NOT NULL,
        point_json       JSONB NOT NULL,
        window_start_ms  BIGINT NOT NULL,
        window_end_ms    BIGINT NOT NULL,
        scenario         TEXT NOT NULL DEFAULT 'base',
        detail           BOOLEAN NOT NULL DEFAULT FALSE,
        stage            TEXT NOT NULL,
        fold_index       INTEGER NULL,
        status           TEXT NOT NULL,
        attempt          INTEGER NOT NULL,
        retries          INTEGER NOT NULL DEFAULT 0,
        total_trades     INTEGER NULL,
        net_profit       DOUBLE PRECISION NULL,
        total_return_pct DOUBLE PRECISION NULL,
        sharpe_ratio     DOUBLE PRECISION NULL,
        max_drawdown_pct DOUBLE PRECISION NULL,
        win_rate         DOUBLE PRECISION NULL,
        error            TEXT NULL,
        detail_json      JSONB NULL,
        created_at_ms    BIGINT NOT NULL,
        completed_at_ms  BIGINT NULL,
        PRIMARY KEY (study_id, evaluation_key),
        CONSTRAINT ck_research_golden_search_evaluations_status
            CHECK (status IN ('pending', 'completed', 'failed')),
        CONSTRAINT ck_research_golden_search_evaluations_window
            CHECK (window_start_ms < window_end_ms)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_search_evaluations_window
        ON research_golden_search_evaluations (study_id, stage, fold_index)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_search_trials (
        id            BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        study_id      TEXT NOT NULL
            REFERENCES research_golden_search_studies (id) ON DELETE RESTRICT,
        stage         TEXT NOT NULL,
        fold_index    INTEGER NULL,
        kind          TEXT NOT NULL,
        payload_json  JSONB NOT NULL,
        created_at_ms BIGINT NOT NULL,
        CONSTRAINT ck_research_golden_search_trials_kind CHECK (kind IN (
            'evaluated', 'cache_hit', 'invalid', 'round', 'stop', 'selection', 'pick',
            'exam_open', 'decision', 'approval'
        ))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_search_trials_study
        ON research_golden_search_trials (study_id, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_search_commands (
        study_id       TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        command        TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        response_json  JSONB NOT NULL,
        created_at_ms  BIGINT NOT NULL,
        PRIMARY KEY (study_id, idempotency_key)
    )
    """,
    # No foreign key to the studies on purpose: an exposure outlives every study.
    """
    CREATE TABLE IF NOT EXISTS research_golden_search_exposures (
        id                   BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        symbol               TEXT NOT NULL,
        interval_start_ms    BIGINT NOT NULL,
        interval_end_ms      BIGINT NOT NULL,
        study_id             TEXT NOT NULL,
        strategy_key         TEXT NOT NULL,
        kind                 TEXT NOT NULL,
        state_at_reservation TEXT NOT NULL,
        claim                TEXT NOT NULL,
        candidate_point_hash TEXT NOT NULL,
        payload_json         JSONB NOT NULL,
        created_at_ms        BIGINT NOT NULL,
        CONSTRAINT ck_research_golden_search_exposures_kind CHECK (kind IN ('reserved', 'result')),
        CONSTRAINT ck_research_golden_search_exposures_state CHECK (
            state_at_reservation IN ('not_opened', 'previously_used', 'history_unknown')
        ),
        CONSTRAINT ck_research_golden_search_exposures_claim CHECK (claim IN ('confirmatory', 'exploratory')),
        CONSTRAINT ck_research_golden_search_exposures_interval CHECK (interval_start_ms < interval_end_ms)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_search_exposures_symbol
        ON research_golden_search_exposures (symbol, interval_start_ms)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_qualifications (
        id                       TEXT PRIMARY KEY,
        program_key              TEXT NOT NULL,
        program_version          TEXT NOT NULL,
        parameter_schema_version TEXT NOT NULL,
        symbol                   TEXT NOT NULL,
        params_json              JSONB NOT NULL,
        params_sha256            TEXT NOT NULL,
        artifact_digest          TEXT NOT NULL,
        wiring_digest            TEXT NOT NULL,
        study_id                 TEXT NOT NULL UNIQUE
            REFERENCES research_golden_search_studies (id) ON DELETE RESTRICT,
        golden_run_id            INTEGER NOT NULL
            REFERENCES research_validation_golden_runs (id) ON DELETE RESTRICT,
        golden_review_id         INTEGER NOT NULL
            REFERENCES research_golden_validation_reviews (id) ON DELETE RESTRICT,
        proof_json               JSONB NOT NULL,
        proof_sha256             TEXT NOT NULL,
        research_json            JSONB NOT NULL,
        note                     TEXT NOT NULL,
        approved_by              TEXT NOT NULL,
        created_at_ms            BIGINT NOT NULL,
        CONSTRAINT ck_research_golden_qualifications_note CHECK (btrim(note) <> '')
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_qualifications_subject
        ON research_golden_qualifications (program_key, symbol, params_sha256)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_qualification_events (
        id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        qualification_id TEXT NOT NULL
            REFERENCES research_golden_qualifications (id) ON DELETE RESTRICT,
        kind             TEXT NOT NULL,
        artifact_digest  TEXT NULL,
        wiring_digest    TEXT NULL,
        proof_json       JSONB NULL,
        reason           TEXT NULL,
        actor            TEXT NOT NULL,
        command_id       TEXT NOT NULL UNIQUE,
        created_at_ms    BIGINT NOT NULL,
        CONSTRAINT ck_research_golden_qualification_events_kind CHECK (kind IN ('reproved', 'revoked')),
        CONSTRAINT ck_research_golden_qualification_events_shape CHECK (
            (kind = 'reproved' AND artifact_digest IS NOT NULL AND wiring_digest IS NOT NULL AND proof_json IS NOT NULL)
            OR (kind = 'revoked' AND reason IS NOT NULL AND btrim(reason) <> '')
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_research_golden_qualification_events_qualification
        ON research_golden_qualification_events (qualification_id, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_defaults (
        program_key      TEXT NOT NULL,
        symbol           TEXT NOT NULL,
        qualification_id TEXT NULL
            REFERENCES research_golden_qualifications (id) ON DELETE RESTRICT,
        revision         INTEGER NOT NULL,
        updated_at_ms    BIGINT NOT NULL,
        PRIMARY KEY (program_key, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS research_golden_default_history (
        id                         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        program_key                TEXT NOT NULL,
        symbol                     TEXT NOT NULL,
        previous_qualification_id  TEXT NULL,
        qualification_id           TEXT NULL,
        revision                   INTEGER NOT NULL,
        reason                     TEXT NOT NULL,
        actor                      TEXT NOT NULL,
        created_at_ms              BIGINT NOT NULL
    )
    """,
    """
    CREATE OR REPLACE FUNCTION reject_golden_search_ledger_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'Golden Search ledgers are append-only';
    END;
    $$
    """,
    """
    CREATE TRIGGER research_golden_search_trials_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_search_trials
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
    """
    CREATE TRIGGER research_golden_search_exposures_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_search_exposures
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
    """
    CREATE TRIGGER research_golden_qualifications_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_qualifications
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
    """
    CREATE TRIGGER research_golden_qualification_events_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_qualification_events
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
    """
    CREATE TRIGGER research_golden_default_history_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_golden_default_history
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
)

# Exposure survives deleting research (#2696): a Grid Search or Walk-Forward Study
# that read a symbol's window leaves that window here when it is deleted, so a
# final test over it never reads as fresh again. Append-only.
DDL_V12: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS research_retired_research_windows (
        id                BIGSERIAL PRIMARY KEY,
        symbol            TEXT NOT NULL,
        interval_start_ms BIGINT NOT NULL,
        interval_end_ms   BIGINT NOT NULL,
        source            TEXT NOT NULL CHECK (source IN ('grid_search', 'walk_forward_study')),
        source_id         TEXT NOT NULL,
        retired_at_ms     BIGINT NOT NULL,
        CHECK (interval_start_ms < interval_end_ms)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS research_retired_research_windows_symbol_idx
        ON research_retired_research_windows (symbol, interval_start_ms, interval_end_ms)
    """,
    """
    CREATE TRIGGER research_retired_research_windows_no_update_or_delete
        BEFORE UPDATE OR DELETE ON research_retired_research_windows
        FOR EACH ROW EXECUTE FUNCTION reject_golden_search_ledger_mutation()
    """,
)

# Run research (#2811): the study's standing intent to hand Search on to Test over time and pause at Compare.
DDL_V13: tuple[str, ...] = (
    """
    ALTER TABLE research_golden_search_studies
        ADD COLUMN IF NOT EXISTS run_to_compare BOOLEAN NOT NULL DEFAULT FALSE
    """,
)

VERSIONED_DDL: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, DDL_V1),
    (2, DDL_V2),
    (3, DDL_V3),
    (4, DDL_V4),
    (5, DDL_V5),
    (6, DDL_V6),
    (7, DDL_V7),
    (8, DDL_V8),
    (9, DDL_V9),
    (10, DDL_V10),
    (11, DDL_V11),
    (12, DDL_V12),
    (13, DDL_V13),
)


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
