"""Verify the live Postgres schema matches catalog_schema.py.

Authoritative source of the schema: EF Core migrations in Backend/Migrations.
This test asserts that the Python typed expectation is in sync with what
EF actually applied. A failure here means either:
  - the EF migration changed but catalog_schema.py wasn't updated, OR
  - catalog_schema.py was edited without a matching EF migration.

Either way, the PR should not merge until they agree.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from app.config import settings
from app.data_lake.catalog_schema import ALL_TABLES as DATA_LAKE_TABLES
from app.data_lake.catalog_schema import TableExpectation
from app.services.lifecycle_projection_schema import ALL_TABLES as LIFECYCLE_PROJECTION_TABLES

pytestmark = pytest.mark.asyncio

ALL_TABLES = (*DATA_LAKE_TABLES, *LIFECYCLE_PROJECTION_TABLES)


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured; skipping live-DB drift test")
    return url


async def _fetch_columns(conn: asyncpg.Connection, table_name: str) -> dict[str, tuple[str, bool]]:
    """Returns {column_name: (data_type, is_nullable)}."""
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1
        """,
        table_name,
    )
    return {r["column_name"]: (r["data_type"], r["is_nullable"] == "YES") for r in rows}


async def _fetch_index_names(conn: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = $1
        """,
        table_name,
    )
    return {r["indexname"] for r in rows}


async def _fetch_check_constraint_names(conn: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT conname FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.contype = 'c' AND t.relname = $1
        """,
        table_name,
    )
    return {r["conname"] for r in rows}


@pytest.mark.parametrize("table", ALL_TABLES, ids=lambda t: t.name)
async def test_schema_matches_expectation(table: TableExpectation) -> None:
    conn = await asyncpg.connect(_postgres_url())
    try:
        live_columns = await _fetch_columns(conn, table.name)
        live_indexes = await _fetch_index_names(conn, table.name)
        live_checks = await _fetch_check_constraint_names(conn, table.name)
    finally:
        await conn.close()

    # Every expected column exists with the expected nullability.
    missing: list[str] = []
    mismatched: list[str] = []
    for col in table.columns:
        if col.name not in live_columns:
            missing.append(col.name)
            continue
        live_type, live_nullable = live_columns[col.name]
        if live_nullable != col.nullable:
            mismatched.append(f"{col.name}: expected nullable={col.nullable}, got {live_nullable}")
        # Type comparison: information_schema.columns.data_type returns the
        # canonical base type name without length modifiers (e.g. `char(64)`
        # → `'character'`, `varchar(40)` → `'character varying'`). Use exact
        # equality so that `character` and `character varying` are distinct.
        if col.pg_type != live_type:
            mismatched.append(f"{col.name}: expected pg_type={col.pg_type!r}, got {live_type!r}")

    assert not missing, f"{table.name}: columns missing from live DB: {missing}"
    assert not mismatched, f"{table.name}: column mismatches: {mismatched}"

    # Every expected partial-unique index, CHECK constraint, and named index exists.
    for ix in (*table.partial_unique_indexes, *table.indexes):
        assert ix in live_indexes, f"{table.name}: missing index {ix!r}"

    for ck in table.check_constraints:
        assert ck in live_checks, f"{table.name}: missing CHECK constraint {ck!r}"
