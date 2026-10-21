"""The SQL schema runner (ADR 0081): file rules, a fresh database, adopting an EF-migrated one, refusals.

The file rules run on every PR. The rest need a database attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one; ``scratch_db`` gives each test a
fresh, empty database that is dropped afterwards.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from app.db_schema import runner as runner_module
from app.db_schema.runner import (
    BASELINE_VERSION,
    LAST_EF_MIGRATION_ID,
    LEDGER_TABLE,
    SchemaMigrationError,
    apply_pending,
    load_migrations,
    migrate_database,
)
from tests.research.conftest import scratch_db as scratch_db

# The tables the 36 EF Core migrations left behind, less the history table and the four Recency* tables
# (the research schema owns those, ADR 0057).
BASELINE_TABLES = {
    "Accounts",
    "DataLabSessions",
    "DataLakeArtifacts",
    "OptionContracts",
    "OptionLegs",
    "Orders",
    "PortfolioSnapshots",
    "PortfolioTrades",
    "PositionLots",
    "Positions",
    "ResearchExperiments",
    "RiskRules",
    "SignalExperiments",
    "StockAggregates",
    "Tickers",
    "clerk_transaction_events",
    "clerk_transaction_feed_status",
    "clerk_transaction_projection_cursors",
    "clerk_transactions",
}


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row["tablename"] for row in rows} - {LEDGER_TABLE}


def test_shipped_migrations_start_at_the_baseline() -> None:
    assert load_migrations()[0].version == BASELINE_VERSION


def test_a_gap_in_the_numbering_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.sql", "SELECT 1;")
    _write(tmp_path, "0003_third.sql", "SELECT 1;")
    with pytest.raises(SchemaMigrationError, match="expected 0002"):
        load_migrations(tmp_path)


def test_a_misnamed_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "0001_First.sql", "SELECT 1;")
    with pytest.raises(SchemaMigrationError, match="is not named"):
        load_migrations(tmp_path)


async def test_an_empty_database_gets_the_whole_baseline_once(scratch_db: asyncpg.Connection) -> None:
    assert await apply_pending(scratch_db) == [BASELINE_VERSION]

    assert await _tables(scratch_db) == BASELINE_TABLES
    row = await scratch_db.fetchrow(f"SELECT adopted, applied_at_ms FROM {LEDGER_TABLE}")
    assert row is not None and row["adopted"] is False and row["applied_at_ms"] > 1_700_000_000_000
    assert await apply_pending(scratch_db) == []


async def test_a_database_ef_finished_is_adopted_without_running_the_baseline(scratch_db: asyncpg.Connection) -> None:
    await scratch_db.execute(
        'CREATE TABLE "__EFMigrationsHistory" ("MigrationId" varchar(150) PRIMARY KEY, "ProductVersion" varchar(32))'
    )
    await scratch_db.execute(
        'INSERT INTO "__EFMigrationsHistory" VALUES ($1, $2), ($3, $2)',
        "20260519052007_InitialSchema",
        "10.0.2",
        LAST_EF_MIGRATION_ID,
    )
    await scratch_db.execute('CREATE TABLE "DataLakeArtifacts" ("Id" bigint PRIMARY KEY)')

    assert await apply_pending(scratch_db) == [BASELINE_VERSION]

    assert await _tables(scratch_db) == {"__EFMigrationsHistory", "DataLakeArtifacts"}
    assert await scratch_db.fetchval(f"SELECT adopted FROM {LEDGER_TABLE}") is True


async def test_a_database_ef_left_part_way_is_refused_with_the_remedy(scratch_db: asyncpg.Connection) -> None:
    await scratch_db.execute(
        'CREATE TABLE "__EFMigrationsHistory" ("MigrationId" varchar(150) PRIMARY KEY, "ProductVersion" varchar(32))'
    )
    await scratch_db.execute(
        'INSERT INTO "__EFMigrationsHistory" VALUES ($1, $2)', "20260926120000_AddCommittedLakeFileLookup", "10.0.2"
    )

    with pytest.raises(SchemaMigrationError, match="run it once"):
        await apply_pending(scratch_db)

    assert await _tables(scratch_db) == {"__EFMigrationsHistory"}


async def test_catalog_tables_without_ef_history_are_refused(scratch_db: asyncpg.Connection) -> None:
    await scratch_db.execute('CREATE TABLE "DataLakeArtifacts" ("Id" bigint PRIMARY KEY)')

    with pytest.raises(SchemaMigrationError, match="no EF migration history"):
        await apply_pending(scratch_db)


async def test_a_later_file_applies_after_the_baseline_and_a_failing_one_leaves_nothing(
    scratch_db: asyncpg.Connection, tmp_path: Path
) -> None:
    _write(tmp_path, "0001_baseline.sql", "CREATE TABLE a_table (id int);")
    _write(tmp_path, "0002_add_b.sql", "CREATE TABLE b_table (id int);")
    assert await apply_pending(scratch_db, load_migrations(tmp_path)) == ["0001_baseline", "0002_add_b"]

    _write(tmp_path, "0003_half_done.sql", "CREATE TABLE c_table (id int); SELECT 1/0;")
    with pytest.raises(asyncpg.DivisionByZeroError):
        await apply_pending(scratch_db, load_migrations(tmp_path))

    assert await _tables(scratch_db) == {"a_table", "b_table"}
    assert await scratch_db.fetchval(f"SELECT count(*) FROM {LEDGER_TABLE}") == 2


async def test_an_applied_file_that_was_edited_is_refused(scratch_db: asyncpg.Connection, tmp_path: Path) -> None:
    _write(tmp_path, "0001_baseline.sql", "CREATE TABLE a_table (id int);")
    await apply_pending(scratch_db, load_migrations(tmp_path))

    _write(tmp_path, "0001_baseline.sql", "CREATE TABLE a_table (id bigint);")
    with pytest.raises(SchemaMigrationError, match="were edited"):
        await apply_pending(scratch_db, load_migrations(tmp_path))


async def test_a_database_from_a_newer_release_is_refused(scratch_db: asyncpg.Connection, tmp_path: Path) -> None:
    _write(tmp_path, "0001_baseline.sql", "CREATE TABLE a_table (id int);")
    _write(tmp_path, "0002_add_b.sql", "CREATE TABLE b_table (id int);")
    await apply_pending(scratch_db, load_migrations(tmp_path))
    (tmp_path / "0002_add_b.sql").unlink()

    with pytest.raises(SchemaMigrationError, match="newer release"):
        await apply_pending(scratch_db, load_migrations(tmp_path))


async def test_the_research_schema_may_create_its_tables_before_or_after_the_runner(
    scratch_db: asyncpg.Connection,
) -> None:
    from app.research.persistence.schema import ensure_schema

    await ensure_schema(scratch_db)
    assert await apply_pending(scratch_db) == [BASELINE_VERSION]
    await ensure_schema(scratch_db)

    assert {"RecencyLaunches", "RecencyRuns", "RecencyTrades", "RecencyTradeMemberships"} <= await _tables(scratch_db)
    assert await _tables(scratch_db) >= BASELINE_TABLES


async def test_two_runs_at_once_apply_the_baseline_exactly_once(scratch_db: asyncpg.Connection) -> None:
    other = await asyncpg.connect(
        os.environ["POSTGRES_URL"], database=await scratch_db.fetchval("SELECT current_database()")
    )
    try:
        first, second = await asyncio.gather(apply_pending(scratch_db), apply_pending(other))
    finally:
        await other.close()

    assert sorted([*first, *second]) == [BASELINE_VERSION]
    assert await scratch_db.fetchval(f"SELECT count(*) FROM {LEDGER_TABLE}") == 1


async def test_a_database_that_is_still_starting_is_retried_until_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    class _Conn:
        async def close(self) -> None:
            return None

    async def flaky_connect(dsn: str, **_: object) -> _Conn:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionRefusedError("starting up")
        return _Conn()

    async def nothing_to_apply(_conn: object) -> list[str]:
        return []

    monkeypatch.setattr(runner_module.asyncpg, "connect", flaky_connect)
    monkeypatch.setattr(runner_module, "apply_pending", nothing_to_apply)

    assert await migrate_database("postgres://example/db", attempts=5, delay_seconds=0) == []
    assert attempts["count"] == 3


async def test_a_database_that_never_answers_stops_the_start_with_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuses(dsn: str, **_: object) -> None:
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(runner_module.asyncpg, "connect", refuses)

    with pytest.raises(SchemaMigrationError, match="did not accept a connection after 2 attempts"):
        await migrate_database("postgres://example/db", attempts=2, delay_seconds=0)


@pytest.mark.parametrize(
    ("url", "outcome", "exit_code"),
    [
        ("", None, 2),
        ("postgres://example/db", SchemaMigrationError("refused"), 1),
        ("postgres://example/db", ["0001_baseline"], 0),
    ],
)
def test_the_command_exits_with_the_code_ci_relies_on(
    monkeypatch: pytest.MonkeyPatch, url: str, outcome: object, exit_code: int
) -> None:
    from app.db_schema import __main__ as command

    async def migrate(_dsn: str) -> list[str]:
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome or [])

    monkeypatch.setenv("POSTGRES_URL", url)
    monkeypatch.setattr(command, "migrate_database", migrate)

    assert command.main() == exit_code
