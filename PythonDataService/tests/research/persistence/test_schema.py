"""``ensure_schema``'s versioned ledger: an applied version is never edited, and a database that applied an early draft is repaired by a later version.

The ledger skips recorded versions outright, so a column added to an
already-applied version never reaches a database that recorded it. The drift
tests run in a throwaway database (``scratch_db``) so the schema can be
reshaped without disturbing the shared ephemeral schema other tests write to.
"""

from __future__ import annotations

import hashlib

import asyncpg

from app.research.persistence.schema import SCHEMA_VERSION, VERSIONED_DDL, ensure_schema

# sha256 of each applied version's statements. Editing an applied version cannot reach a database
# that already recorded it (that is how version 4 became necessary); a change belongs in a new version.
APPLIED_VERSION_DIGESTS: dict[int, str] = {
    1: "81efcf39cd1dc464",
    2: "cf43cb591e5cc90e",
    3: "9710c4b2f398e124",
    4: "af50760fa9b256df",
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
