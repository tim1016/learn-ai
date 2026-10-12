"""``ensure_schema``'s versioned ledger: an applied version is never edited, and a database that applied an early draft is repaired by a later version.

The ledger skips recorded versions outright, so a column added to an
already-applied version never reaches a database that recorded it. The drift
tests run in a throwaway database (``scratch_db``) so the schema can be
reshaped without disturbing the shared ephemeral schema other tests write to.
"""

from __future__ import annotations

import hashlib

import asyncpg
import pytest

from app.research.persistence.schema import SCHEMA_VERSION, VERSIONED_DDL, ensure_schema

# sha256 of each applied version's statements. Editing an applied version cannot reach a database
# that already recorded it (that is how version 4 became necessary); a change belongs in a new version.
APPLIED_VERSION_DIGESTS: dict[int, str] = {
    1: "81efcf39cd1dc464",
    2: "cf43cb591e5cc90e",
    3: "9710c4b2f398e124",
    4: "af50760fa9b256df",
    5: "5bafa2856e347d82",
    6: "ff9c91a7adf7ac30",
    7: "69a0fae284dcaa08",
    8: "bb246cd8379cf33e",
    9: "af0c45ee7f3d5e29",
    10: "9051f189ab014f1e",
    11: "f7c290d147980eb7",
    12: "82be8428a0c3936a",
    13: "d55e48c00bddb801",
    14: "c640fe2ce26a70e1",
    15: "740c3fb259daa2fd",
    16: "3daafaddfe68c5ad",
    17: "b179559cd44c2260",
    18: "81e46d16b20848a1",
    19: "d0a8cb7cc114a7bb",
    20: "c4a8170ac6c104c2",
}


def _digest(statements: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(statements).encode()).hexdigest()[:16]


def test_an_applied_schema_version_is_never_edited() -> None:
    assert {version: _digest(statements) for version, statements in VERSIONED_DDL} == APPLIED_VERSION_DIGESTS, (
        "a recorded schema version changed; databases that applied it will never see the edit — add a new version instead"
    )
    assert max(APPLIED_VERSION_DIGESTS) == SCHEMA_VERSION


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = $1 AND column_name = $2", table, column
        )
    )


async def test_a_database_that_applied_the_early_version_1_draft_gains_leader_params_json(scratch_db: asyncpg.Connection) -> None:
    """Stage the drift a dev database showed on 2026-09-05: version 1 recorded, column absent, version 4 unknown."""
    await ensure_schema(scratch_db)
    await scratch_db.execute("ALTER TABLE research_grid_searches DROP COLUMN leader_params_json")
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 4")
    assert not await _column_exists(scratch_db, "research_grid_searches", "leader_params_json")

    await ensure_schema(scratch_db)

    assert await _column_exists(scratch_db, "research_grid_searches", "leader_params_json")
    versions = await scratch_db.fetch("SELECT version FROM research_schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == list(range(1, SCHEMA_VERSION + 1))


async def test_version_4_is_a_no_op_where_version_1_was_complete(scratch_db: asyncpg.Connection) -> None:
    await ensure_schema(scratch_db)
    before = await scratch_db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'research_grid_searches' ORDER BY ordinal_position"
    )

    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 4")
    await ensure_schema(scratch_db)

    after = await scratch_db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'research_grid_searches' ORDER BY ordinal_position"
    )
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


async def test_version_6_cascades_a_parity_verdict_with_its_lean_side(scratch_db: asyncpg.Connection) -> None:
    await ensure_schema(scratch_db)

    delete_rule = await scratch_db.fetchval(
        "SELECT confdeltype FROM pg_constraint WHERE conname = 'research_parity_verdicts_right_run_id_fkey'"
    )

    assert delete_rule == b"c"  # CASCADE (asyncpg returns the "char" column as bytes); version 5 had SET NULL


async def test_version_7_declares_append_only_golden_validation_ledgers(scratch_db: asyncpg.Connection) -> None:
    await ensure_schema(scratch_db)

    tables = await scratch_db.fetch(
        """
        SELECT tablename FROM pg_tables
         WHERE tablename IN ('research_validation_golden_runs', 'research_golden_validation_reviews')
         ORDER BY tablename
        """
    )
    triggers = await scratch_db.fetch(
        """
        SELECT tgname FROM pg_trigger
         WHERE tgname IN (
            'research_validation_golden_runs_no_update_or_delete',
            'research_golden_validation_reviews_no_update_or_delete'
         )
         ORDER BY tgname
        """
    )

    assert [row["tablename"] for row in tables] == [
        "research_golden_validation_reviews",
        "research_validation_golden_runs",
    ]
    assert [row["tgname"] for row in triggers] == [
        "research_golden_validation_reviews_no_update_or_delete",
        "research_validation_golden_runs_no_update_or_delete",
    ]


async def test_version_8_preserves_a_previously_legal_rejected_authorization(
    scratch_db: asyncpg.Connection,
) -> None:
    """V7 history is immutable, so the stricter rule applies prospectively."""
    await ensure_schema(scratch_db)
    await scratch_db.execute(
        "ALTER TABLE research_golden_validation_reviews "
        "DROP CONSTRAINT ck_research_golden_validation_reviews_authorized_only_on_accept"
    )
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 8")
    source_run_id = await scratch_db.fetchval(
        """
        INSERT INTO research_backtest_runs (
            source, strategy_name, symbol, start_ms, end_ms, timespan,
            fill_mode, executed_at_ms, total_trades, winning_trades,
            losing_trades, win_rate, total_pnl, initial_cash, final_equity,
            total_fees
        ) VALUES (
            'engine', 'ema_crossover_signal', 'SPY', 1, 2, 'minute',
            'signal_bar_close', 3, 0, 0, 0, 0, 0, 100000, 100000, 0
        )
        RETURNING id
        """
    )
    golden_run_id = await scratch_db.fetchval(
        """
        INSERT INTO research_validation_golden_runs (
            source_run_id, command_id, command_sha256, strategy_name, symbol,
            validation_case_json, case_sha256, rationale, designated_by,
            designated_at_ms
        ) VALUES ($1, 'v7-designation', 'command-sha', 'ema_crossover_signal',
                  'SPY', '{}'::jsonb, 'case-sha', 'historical', 'local:test', 4)
        RETURNING id
        """,
        source_run_id,
    )
    await scratch_db.execute(
        """
        INSERT INTO research_golden_validation_reviews (
            golden_run_id, command_id, command_sha256,
            expected_evidence_revision, decision, classification,
            evidence_state, evidence_json, evidence_sha256, reason,
            authorized_program_version, reviewed_by, reviewed_at_ms
        ) VALUES ($1, 'v7-review', 'review-sha', 'revision', 'reject', NULL,
                  'missing', '{}'::jsonb, 'evidence-sha', 'historical refusal',
                  'ema-crossover-signal/v1', 'local:test', 5)
        """,
        golden_run_id,
    )

    await ensure_schema(scratch_db)

    assert await scratch_db.fetchval(
        "SELECT authorized_program_version FROM research_golden_validation_reviews "
        "WHERE command_id = 'v7-review'"
    ) == "ema-crossover-signal/v1"
    with pytest.raises(asyncpg.CheckViolationError):
        await scratch_db.execute(
            """
            INSERT INTO research_golden_validation_reviews (
                golden_run_id, command_id, command_sha256,
                expected_evidence_revision, decision, classification,
                evidence_state, evidence_json, evidence_sha256, reason,
                authorized_program_version, reviewed_by, reviewed_at_ms
            ) VALUES ($1, 'v8-review', 'review-sha-2', 'revision', 'reject', NULL,
                      'missing', '{}'::jsonb, 'evidence-sha-2', 'new refusal',
                      'ema-crossover-signal/v1', 'local:test', 6)
            """,
            golden_run_id,
        )


async def test_version_5_nulls_every_recency_study_reference(scratch_db: asyncpg.Connection) -> None:
    """Old study ids named rows of the EF table; under a fresh identity sequence they would point at unrelated runs."""
    await ensure_schema(scratch_db)
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 5")
    await scratch_db.execute(
        """
        INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
        VALUES ('launch-v5', '{}'::jsonb, 1, 1, 0, 'COMPLETED', 1)
        """
    )
    await scratch_db.execute(
        """
        INSERT INTO "RecencyRuns" ("RecencyLaunchId", "StrategyKey", "Symbol", "ParamsJson", "ParamsHash", "StudyId", "TotalPnl", "CreatedAtMs")
        VALUES ('launch-v5', 'sma_crossover', 'SPY', '{}'::jsonb, 'h', 77, 0, 1)
        """
    )

    await ensure_schema(scratch_db)

    assert await scratch_db.fetchval('SELECT count(*) FROM "RecencyRuns" WHERE "StudyId" IS NOT NULL') == 0
    assert await scratch_db.fetchval("SELECT to_regclass('research_backtest_runs')") is not None


async def test_version_14_returns_a_study_waiting_with_a_locked_candidate_to_compare(scratch_db: asyncpg.Connection) -> None:
    """Both fits sit the final test (#2930), so the state a pick used to leave a study in is gone."""
    await ensure_schema(scratch_db)
    # Stage a database that recorded version 13: its state rule still admits the locked-candidate state.
    await scratch_db.execute("ALTER TABLE research_golden_search_studies DROP CONSTRAINT ck_research_golden_search_studies_state")
    await scratch_db.execute(
        """
        ALTER TABLE research_golden_search_studies ADD CONSTRAINT ck_research_golden_search_studies_state
            CHECK (state IN ('candidate_locked', 'awaiting_candidate', 'awaiting_review'))
        """
    )
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 14")
    for study_id, state, key in (("locked-pick", "candidate_locked", "recent"), ("reviewed", "awaiting_review", "all_period")):
        await scratch_db.execute(
            """
            INSERT INTO research_golden_search_studies (
                id, strategy_key, symbol, state, created_at_ms, updated_at_ms, protocol_json, protocol_hash,
                receipt_json, candidate_key, budget_cap
            ) VALUES ($1, 'ema_crossover_signal', 'SPY', $2, 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, $3, 5000)
            """,
            study_id,
            state,
            key,
        )

    await ensure_schema(scratch_db)

    rows = await scratch_db.fetch("SELECT id, state, candidate_key FROM research_golden_search_studies ORDER BY id")
    # The pick made before the test is void; a study already past its final test keeps the candidate it tested.
    assert [tuple(row) for row in rows] == [("locked-pick", "awaiting_candidate", None), ("reviewed", "awaiting_review", "all_period")]
    with pytest.raises(asyncpg.CheckViolationError):
        await scratch_db.execute("UPDATE research_golden_search_studies SET state = 'candidate_locked' WHERE id = 'reviewed'")


async def test_version_15_leaves_recorded_evaluations_without_a_curve_and_keeps_the_two_arrays_the_same_length(scratch_db: asyncpg.Connection) -> None:
    """Every run keeps its daily equity from here on (#2930); a run recorded before has none, and is not given one."""
    await ensure_schema(scratch_db)
    await scratch_db.execute("ALTER TABLE research_golden_search_evaluations DROP CONSTRAINT ck_research_golden_search_evaluations_equity_arrays")
    await scratch_db.execute("ALTER TABLE research_golden_search_evaluations DROP COLUMN equity_close_ms, DROP COLUMN equity_values")
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 15")
    await scratch_db.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms, protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ('s1', 'ema_crossover_signal', 'SPY', 'awaiting_candidate', 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 5000)
        """
    )
    await scratch_db.execute(
        """
        INSERT INTO research_golden_search_evaluations (
            study_id, evaluation_key, point_hash, point_json, window_start_ms, window_end_ms, stage, status, attempt, created_at_ms
        ) VALUES ('s1', 'old-run', 'p', '{}'::jsonb, 1, 2, 'search', 'completed', 1, 1)
        """
    )

    await ensure_schema(scratch_db)

    old = await scratch_db.fetchrow("SELECT equity_close_ms, equity_values FROM research_golden_search_evaluations WHERE evaluation_key = 'old-run'")
    assert (old["equity_close_ms"], old["equity_values"]) == (None, None)
    await scratch_db.execute("UPDATE research_golden_search_evaluations SET equity_close_ms = '{1,2}', equity_values = '{10.0,11.0}' WHERE evaluation_key = 'old-run'")
    for close_ms, values in (("'{1,2}'", "'{10.0}'"), ("'{1}'", "NULL"), ("NULL", "'{10.0}'")):
        with pytest.raises(asyncpg.CheckViolationError):
            await scratch_db.execute(f"UPDATE research_golden_search_evaluations SET equity_close_ms = {close_ms}, equity_values = {values} WHERE evaluation_key = 'old-run'")


async def test_version_16_leaves_recorded_cells_without_a_curve_and_keeps_the_two_arrays_the_same_length(scratch_db: asyncpg.Connection) -> None:
    """Every Grid Search cell keeps its daily equity from here on (#2935); a cell recorded before has none, and is not given one."""
    await ensure_schema(scratch_db)
    await scratch_db.execute("ALTER TABLE research_grid_search_cells DROP CONSTRAINT ck_research_grid_search_cells_equity_arrays")
    await scratch_db.execute("ALTER TABLE research_grid_search_cells DROP COLUMN equity_close_ms, DROP COLUMN equity_values")
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 16")
    await scratch_db.execute(
        """
        INSERT INTO research_grid_searches (
            id, owner_kind, strategy_key, symbol, status, attempt, created_at_ms, updated_at_ms, request_json, receipt_json, expected_cells
        ) VALUES ('g1', 'user', 'sma_crossover', 'SPY', 'completed', 1, 1, 1, '{}'::jsonb, '{}'::jsonb, 1)
        """
    )
    await scratch_db.execute(
        """
        INSERT INTO research_grid_search_cells (search_id, params_hash, params_json, status, attempt, completed_at_ms)
        VALUES ('g1', 'old-cell', '{}'::jsonb, 'completed', 1, 1)
        """
    )

    await ensure_schema(scratch_db)

    old = await scratch_db.fetchrow("SELECT equity_close_ms, equity_values FROM research_grid_search_cells WHERE params_hash = 'old-cell'")
    assert (old["equity_close_ms"], old["equity_values"]) == (None, None)
    await scratch_db.execute("UPDATE research_grid_search_cells SET equity_close_ms = '{1,2}', equity_values = '{10.0,11.0}' WHERE params_hash = 'old-cell'")
    for close_ms, values in (("'{1,2}'", "'{10.0}'"), ("'{1}'", "NULL"), ("NULL", "'{10.0}'")):
        with pytest.raises(asyncpg.CheckViolationError):
            await scratch_db.execute(f"UPDATE research_grid_search_cells SET equity_close_ms = {close_ms}, equity_values = {values} WHERE params_hash = 'old-cell'")


async def test_version_18_gives_recorded_evaluations_a_generation_and_a_start_marker(scratch_db: asyncpg.Connection) -> None:
    """A pending row records before v18 as never dispatched; the columns arrive without touching its result (#3045)."""
    await ensure_schema(scratch_db)
    await scratch_db.execute(
        """
        ALTER TABLE research_golden_search_evaluations
            DROP COLUMN IF EXISTS task_gen, DROP COLUMN IF EXISTS started_at_ms
        """
    )
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 18")
    await scratch_db.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms, protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ('s18', 'ema_crossover_signal', 'SPY', 'search_running', 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 5000)
        """
    )
    await scratch_db.execute(
        """
        INSERT INTO research_golden_search_evaluations (
            study_id, evaluation_key, point_hash, point_json, window_start_ms, window_end_ms, stage, status, attempt, created_at_ms
        ) VALUES ('s18', 'pending-run', 'p', '{}'::jsonb, 1, 2, 'search', 'pending', 1, 1)
        """
    )

    await ensure_schema(scratch_db)

    row = await scratch_db.fetchrow(
        "SELECT task_gen, started_at_ms FROM research_golden_search_evaluations WHERE evaluation_key = 'pending-run'"
    )
    # Generation 0, never started: the safe direction — a legacy pending row re-runs without spending a retry.
    assert (row["task_gen"], row["started_at_ms"]) == (0, None)


_OPTION_RUN_TABLES = [
    "research_options_backtest_run_sessions",
    "research_options_backtest_run_trades",
    "research_options_backtest_runs",
]


async def _option_run_tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'research_options_backtest_run%' ORDER BY tablename"
    )
    return [row["tablename"] for row in rows]


async def test_version_20_gives_an_empty_database_the_three_option_run_tables(scratch_db: asyncpg.Connection) -> None:
    await ensure_schema(scratch_db)

    assert await _option_run_tables(scratch_db) == _OPTION_RUN_TABLES
    versions = await scratch_db.fetch("SELECT version FROM research_schema_migrations ORDER BY version")
    assert [row["version"] for row in versions] == list(range(1, SCHEMA_VERSION + 1))


async def test_version_20_arrives_on_top_of_version_19_and_leaves_a_saved_setup_alone(scratch_db: asyncpg.Connection) -> None:
    """Stage a database that recorded version 19 and never saw 20: the setups table has a row, the run tables are absent."""
    await ensure_schema(scratch_db)
    for table in _OPTION_RUN_TABLES:
        await scratch_db.execute(f"DROP TABLE {table} CASCADE")
    await scratch_db.execute("DELETE FROM research_schema_migrations WHERE version = 20")
    await scratch_db.execute(
        "INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms) VALUES ('kept', '{}'::jsonb, 5)"
    )
    assert await _option_run_tables(scratch_db) == []

    await ensure_schema(scratch_db)

    assert await _option_run_tables(scratch_db) == _OPTION_RUN_TABLES
    assert await scratch_db.fetchval("SELECT saved_at_ms FROM research_options_backtest_setups WHERE name = 'kept'") == 5
    assert await scratch_db.fetchval("SELECT max(version) FROM research_schema_migrations") == 20
