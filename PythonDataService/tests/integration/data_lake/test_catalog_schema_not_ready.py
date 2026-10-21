"""Regression test: claim_minute_bar raises CatalogSchemaNotReadyError, not a
raw asyncpg exception, when the database schema is behind the code.

The data plane migrates its own schema before it serves (ADR 0081), so the old
deploy race (Python serving while another process still migrated) is gone. The
translation stays for the cases that remain: a database restored from an older
backup, or a process pointed at a database the data plane has not migrated yet.
A claim's INSERT names ``LeaseGeneration`` unconditionally and Postgres validates
that column list before it evaluates the ON CONFLICT target, so a schema without
the column surfaces SQLSTATE 42703 (undefined column) -- the same
schema-not-ready symptom as 42P10, translated by the same
``CatalogSchemaNotReadyError``; see its docstring.

The schema behind is built from the baseline migration with the newest column
removed, in a scratch database on the disposable Postgres. Skips cleanly (same
convention as test_schema_drift.py) unless ``POSTGRES_URL`` is set and attested
as disposable with ``POSTGRES_URL_IS_EPHEMERAL=1``. Never points at my-postgres.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.catalog_client import CatalogSchemaNotReadyError
from app.data_lake.types import ArtifactIdentity
from app.db_schema import apply_pending
from tests.research.conftest import scratch_db as scratch_db

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def schema_behind_dsn(scratch_db: asyncpg.Connection) -> AsyncIterator[str]:
    """A DSN for a scratch database with the baseline schema minus ``LeaseGeneration``."""
    await apply_pending(scratch_db)
    await scratch_db.execute('ALTER TABLE "DataLakeArtifacts" DROP COLUMN "LeaseGeneration"')
    name = await scratch_db.fetchval("SELECT current_database()")
    parts = urlsplit(os.environ["POSTGRES_URL"])
    yield urlunsplit(parts._replace(path=f"/{name}"))


def _minute_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


async def test_claim_minute_bar_raises_catalog_schema_not_ready_when_the_schema_is_behind(
    schema_behind_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", schema_behind_dsn)
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    try:
        with pytest.raises(CatalogSchemaNotReadyError) as exc_info:
            await catalog_client.claim_minute_bar(
                identity=_minute_identity(),
                worker_id="w-1",
                lease_ttl_ms=300_000,
                data_contract_hash="a" * 64,
                file_path="equity/usa/minute/spy/20240520_trade.zip",
            )

        # Translated, not the raw driver error -- and the chained cause is
        # exactly the SQLSTATE this test pins: 42703 (undefined column).
        assert not isinstance(exc_info.value, asyncpg.PostgresError)
        assert "42703" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, asyncpg.PostgresError)
        assert exc_info.value.__cause__.sqlstate == "42703"
    finally:
        await catalog_client.close_pool()
