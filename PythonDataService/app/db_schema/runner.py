"""Ordered plain-SQL migrations for the shared Postgres database (ADR 0081).

Python owns the schema. Every ``migrations/NNNN_name.sql`` file is applied once,
in filename order, each inside its own transaction, and recorded in
``schema_migrations`` with a checksum. An applied file is never edited: change
the schema with a new file. The files are plain SQL; there is no down step.

``0001_baseline`` is the end state of the 36 EF Core migrations the retired C#
backend applied. A database EF already migrated is *adopted*: the baseline is
recorded as applied without running it, because its tables exist. An adopted
database must be at EF's last migration; one behind it, or one that has the
tables but no EF history, is refused with the remediation, never guessed at.

The research tables (``research_*``) keep their own ledger and create
themselves (``app.research.persistence.schema``); this runner owns the rest.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LEDGER_TABLE = "schema_migrations"
BASELINE_VERSION = "0001_baseline"
# The last migration the C# backend applied; the baseline stands for everything up to it.
LAST_EF_MIGRATION_ID = "20261001045334_DropUnwrittenMarketDataTables"
# A table only the baseline creates: its presence without EF history marks a legacy database.
_BASELINE_PROBE_TABLE = "DataLakeArtifacts"
# Serialises concurrent runs (the API starting while a manual run is going).
_ADVISORY_LOCK_KEY = 0x5343_4845_4D41_0001

_FILE_PATTERN = re.compile(r"^(?P<number>\d{4})_[a-z0-9_]+\.sql$")
_CONNECT_ATTEMPTS = 10
_CONNECT_DELAY_SECONDS = 2.0
_CONNECT_TIMEOUT_SECONDS = 10.0


class SchemaMigrationError(RuntimeError):
    """The database cannot be brought to the schema this code expects."""


@dataclass(frozen=True)
class Migration:
    version: str
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read ``NNNN_name.sql`` files in order; the numbers must run 0001.. with no gap or repeat."""
    files = sorted(path for path in directory.iterdir() if path.suffix == ".sql")
    migrations: list[Migration] = []
    for position, path in enumerate(files, start=1):
        match = _FILE_PATTERN.match(path.name)
        if match is None:
            raise SchemaMigrationError(f"{path.name} is not named NNNN_lowercase_name.sql")
        if int(match["number"]) != position:
            raise SchemaMigrationError(
                f"migration files must be numbered 0001.. without gaps or repeats; "
                f"expected {position:04d}_*, found {path.name}"
            )
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(
            Migration(version=path.stem, sql=sql, checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest())
        )
    return migrations


async def apply_pending(conn: asyncpg.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Bring the database to the newest migration; return the versions recorded now (adopted included)."""
    migrations = load_migrations() if migrations is None else migrations
    await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
    try:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                version text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at_ms bigint NOT NULL,
                adopted boolean NOT NULL DEFAULT false
            )
            """
        )
        recorded = {
            row["version"]: row["checksum"] for row in await conn.fetch(f"SELECT version, checksum FROM {LEDGER_TABLE}")
        }
        _require_ledger_matches_files(recorded, migrations)

        done: list[str] = []
        for migration in migrations:
            if migration.version in recorded:
                continue
            if migration.version == BASELINE_VERSION and await _database_is_ef_migrated(conn):
                await _record(conn, migration, adopted=True)
                logger.info(
                    "Adopted the EF-migrated database as the schema baseline.",
                    extra={"action": "db_schema_adopted", "version": migration.version},
                )
            else:
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await _record(conn, migration, adopted=False)
                logger.info(
                    "Applied schema migration.",
                    extra={"action": "db_schema_applied", "version": migration.version},
                )
            done.append(migration.version)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)


async def migrate_database(
    dsn: str,
    *,
    attempts: int = _CONNECT_ATTEMPTS,
    delay_seconds: float = _CONNECT_DELAY_SECONDS,
) -> list[str]:
    """Connect (waiting for a database that is still starting) and run :func:`apply_pending`."""
    for attempt in range(1, attempts + 1):
        try:
            conn = await asyncpg.connect(dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
        except (OSError, TimeoutError, asyncpg.CannotConnectNowError, asyncpg.PostgresConnectionError) as exc:
            if attempt == attempts:
                raise SchemaMigrationError(
                    f"the database did not accept a connection after {attempts} attempts: {exc}"
                ) from exc
            logger.warning(
                "Database not ready for schema migration; retrying.",
                extra={"action": "db_schema_connect_retry", "attempt": attempt, "attempts": attempts},
            )
            await asyncio.sleep(delay_seconds)
            continue
        try:
            return await apply_pending(conn)
        finally:
            await conn.close()
    raise AssertionError("unreachable: the loop returns or raises")


def _require_ledger_matches_files(recorded: dict[str, str], migrations: list[Migration]) -> None:
    by_version = {migration.version: migration for migration in migrations}
    missing = sorted(set(recorded) - set(by_version))
    if missing:
        raise SchemaMigrationError(
            f"the database records migrations this code does not have ({', '.join(missing)}); "
            "it was migrated by a newer release. Deploy that release instead of this one."
        )
    changed = sorted(version for version, checksum in recorded.items() if by_version[version].checksum != checksum)
    if changed:
        raise SchemaMigrationError(
            f"applied migration files were edited ({', '.join(changed)}). An applied file is final: "
            "restore it and put the change in a new numbered file."
        )


async def _database_is_ef_migrated(conn: asyncpg.Connection) -> bool:
    """True for a database EF finished; raise for one it left half done or built without history."""
    has_history = await conn.fetchval("SELECT to_regclass('public.\"__EFMigrationsHistory\"') IS NOT NULL")
    if has_history:
        latest = await conn.fetchval('SELECT max("MigrationId") FROM public."__EFMigrationsHistory"')
        if latest != LAST_EF_MIGRATION_ID:
            raise SchemaMigrationError(
                f"this database was migrated by EF only as far as {latest!r}, but the baseline stands for "
                f"{LAST_EF_MIGRATION_ID}. Check out the last release that still contains Backend/, run it once "
                "to finish its migrations, then start this release."
            )
        return True
    if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f'public."{_BASELINE_PROBE_TABLE}"'):
        raise SchemaMigrationError(
            f'the database has the "{_BASELINE_PROBE_TABLE}" table but no EF migration history, so its schema '
            "cannot be trusted to match the baseline. Restore it from a backup taken after EF finished, or "
            "start from an empty database."
        )
    return False


async def _record(conn: asyncpg.Connection, migration: Migration, *, adopted: bool) -> None:
    await conn.execute(
        f"INSERT INTO {LEDGER_TABLE} (version, checksum, applied_at_ms, adopted) VALUES ($1, $2, $3, $4)",
        migration.version,
        migration.checksum,
        now_ms_utc(),
        adopted,
    )
