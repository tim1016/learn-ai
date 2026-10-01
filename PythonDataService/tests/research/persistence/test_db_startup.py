"""The data plane applies the research schema as it starts, and never fails or delays startup over it (#2696).

A clerk reads the Golden Search qualification tables but cannot apply DDL,
so the data plane ensures every pending version at startup. The unreachable
case needs no database; the applied case runs in a throwaway database
(``scratch_db``) so a fresh schema can be observed being created.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.research.persistence.db import ensure_schema_at_startup
from app.research.persistence.schema import SCHEMA_VERSION

LOGGER = "app.research.persistence.db"


def _actions(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER and getattr(record, "action", None)]


async def test_ensure_schema_at_startup_logs_an_unreachable_database_and_returns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await ensure_schema_at_startup()

    (record,) = _actions(caplog)
    assert record.action == "research_schema_ensure_failed"  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING and record.exc_info is not None


async def test_ensure_schema_at_startup_applies_every_version_to_a_fresh_database(
    scratch_db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    name = await scratch_db.fetchval("SELECT current_database()")
    monkeypatch.setattr(settings, "POSTGRES_URL", urlunsplit(urlsplit(os.environ["POSTGRES_URL"])._replace(path=f"/{name}")))

    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await ensure_schema_at_startup()
    finally:
        await catalog_client.close_pool()

    assert [record.action for record in _actions(caplog)] == ["research_schema_ensured"]  # type: ignore[attr-defined]
    assert await scratch_db.fetchval("SELECT max(version) FROM research_schema_migrations") == SCHEMA_VERSION
    assert await scratch_db.fetchval("SELECT to_regclass('research_golden_defaults')::text") == "research_golden_defaults"
