"""Live-Postgres fixtures for the Grid Search repository.

Runs only against a database explicitly attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``, the same signal the data-lake catalog
tests require) — these tests drop and recreate the Python-owned sweep
tables, which is never acceptable against a developer's real catalog.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.research.grid_search import db
from app.research.grid_search.schema import ensure_schema

_TABLES = ("research_grid_search_cells", "research_grid_searches", "research_schema_migrations")


def _ephemeral_url() -> str:
    url = os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured; skipping live-DB repository tests")
    if os.getenv("POSTGRES_URL_IS_EPHEMERAL", "").strip().lower() not in ("1", "true"):
        pytest.skip("POSTGRES_URL is not attested ephemeral (POSTGRES_URL_IS_EPHEMERAL=1); refusing to touch it")
    return url


@pytest.fixture
async def conn() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(_ephemeral_url())
    try:
        for table in _TABLES:
            await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        await ensure_schema(connection)
        # Every loop re-ensures after the tables were dropped and recreated above.
        db._schema_ready_loops.clear()
        yield connection
    finally:
        await connection.close()
