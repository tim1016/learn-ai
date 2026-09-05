"""Live-Postgres fixtures for the Grid Search repository.

Runs only against a database explicitly attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``, the same signal the data-lake catalog
tests require). The tables are ensured once and never dropped: every test
works on ids and symbols of its own, so the suite is safe under xdist's
``--dist load`` (CI runs the change-driven paths in parallel).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from app.research.persistence.schema import ensure_schema


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
        await ensure_schema(connection)
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def second_conn() -> AsyncIterator[asyncpg.Connection]:
    """A second session, for fence tests that need two writers."""
    connection = await asyncpg.connect(_ephemeral_url())
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def unique() -> str:
    """A per-test tag for ids and symbols, so parallel tests never see each other's rows."""
    return uuid.uuid4().hex[:10]
