"""A pooled connection to the shared Postgres on the calling event loop, for stores that read and write its tables."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.data_lake import catalog_client


@asynccontextmanager
async def pooled_connection() -> AsyncIterator[asyncpg.Connection]:
    """A connection from the calling loop's pool, which this opens first if it is not open.

    Raises ``CatalogUnavailableError`` when ``POSTGRES_URL`` is empty or the database cannot be reached; a route
    built on ``StoreUnreachableIsA503`` answers that as a 503.
    """
    await catalog_client.init_pool()
    async with catalog_client.connection() as conn:
        yield conn
