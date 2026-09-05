"""Connection access for Grid Search persistence, on either loop.

The asyncpg pool is ``app.data_lake.catalog_client``'s per-loop pool, so a
FastAPI handler and a worker thread never share a connection: the handler
awaits :func:`connection` on the app loop; the worker calls :func:`run_sync`,
which submits onto ``app.utils.background_loop`` — the named writer loop
and pool owner for every sweep write (review F15). The Python-owned schema
is ensured once per loop on first use.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.data_lake import catalog_client
from app.research.grid_search.schema import ensure_schema
from app.utils.background_loop import run_on_background_loop

_schema_ready_loops: set[int] = set()
DB_CALL_TIMEOUT_SECONDS = 60.0


@asynccontextmanager
async def connection() -> AsyncIterator[asyncpg.Connection]:
    """A pooled connection on the calling loop, with the sweep schema in place."""
    await catalog_client.init_pool()
    loop_id = id(asyncio.get_running_loop())
    async with catalog_client.connection() as conn:
        if loop_id not in _schema_ready_loops:
            await ensure_schema(conn)
            _schema_ready_loops.add(loop_id)
        yield conn


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a repository coroutine from a worker thread on the shared writer loop."""
    return run_on_background_loop(coroutine, timeout=DB_CALL_TIMEOUT_SECONDS)
