"""``python -m app.db_schema`` applies the pending schema migrations to ``POSTGRES_URL``.

The API does the same at startup; this entry point is for CI and for preparing a
throwaway database before a test run. It reads the environment directly so it
needs none of the service's other settings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.db_schema.runner import SchemaMigrationError, migrate_database

logger = logging.getLogger("app.db_schema")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dsn = os.environ.get("POSTGRES_URL", "")
    if not dsn:
        logger.error("POSTGRES_URL is not set; there is no database to migrate.")
        return 2
    try:
        applied = asyncio.run(migrate_database(dsn))
    except SchemaMigrationError as exc:
        logger.error("Schema migration refused: %s", exc)
        return 1
    logger.info("Schema migrations applied: %s", ", ".join(applied) if applied else "none, already at head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
