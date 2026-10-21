"""The API migrates the database before it serves, and a refusal stops the start (ADR 0081)."""

from __future__ import annotations

import pytest

import app.db_schema as db_schema
from app.config import settings
from app.db_schema import SchemaMigrationError
from app.main import _apply_database_schema


async def test_a_deployment_without_a_database_url_migrates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "")

    async def must_not_run(_dsn: str) -> list[str]:
        raise AssertionError("no database to migrate")

    monkeypatch.setattr(db_schema, "migrate_database", must_not_run)

    await _apply_database_schema()


async def test_a_refused_migration_aborts_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", "postgres://example/db")

    async def refuses(_dsn: str) -> list[str]:
        raise SchemaMigrationError("the database was migrated by a newer release")

    monkeypatch.setattr(db_schema, "migrate_database", refuses)

    with pytest.raises(SchemaMigrationError, match="newer release"):
        await _apply_database_schema()
