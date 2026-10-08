"""Regression tests for the per-test catalog-pool teardown (#2809).

The daily full suite went red with ``asyncpg.exceptions.TooManyConnectionsError``
across every Postgres-backed test from tests/routers onward. Root cause:
``app.data_lake.catalog_client`` keys an asyncpg pool per event loop, and only
a live loop can close its own pool — pytest-asyncio hands each test a fresh
loop, and nothing closed the pool when the test ended, so connections
accumulated until the dead loops were garbage collected. The suite crossed
Postgres' default max_connections (100) once the golden-search suites (#2696)
added ~150 live-DB tests.

tests/conftest.py's autouse ``_close_catalog_pool_per_test`` fixture exists to
make that structurally impossible to repeat. A stub pool is planted on the
test's loop and checked by a module-scoped fixture after function teardown.
The proof stays within one test and worker: it depends on neither test order
nor process-global state. If the autouse fixture stops closing the pool on
its own loop, the final check fails. No POSTGRES_URL is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.data_lake import catalog_client


class _StubPool:
    """The slice of asyncpg.Pool close_pool touches: an awaitable close()."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.closed_on_loop: asyncio.AbstractEventLoop | None = None

    async def close(self) -> None:
        self.closed_on_loop = asyncio.get_running_loop()


@pytest.fixture(scope="module")
def pool_checked_after_teardown() -> Iterator[_StubPool]:
    """A longer-lived check runs after the per-test autouse fixture closes the pool (#2809)."""
    stub = _StubPool()
    yield stub
    assert stub.loop is not None
    assert stub.closed_on_loop is stub.loop, "the pool was not closed on its owning live loop"
    assert all(pool is not stub for pool in catalog_client._pools.values()), "the closed pool is still registered"


async def test_a_pool_left_on_the_tests_loop_is_closed_by_teardown(pool_checked_after_teardown: _StubPool) -> None:
    """Plant a pool on this test's loop, as service code does via init_pool."""
    stub = pool_checked_after_teardown
    stub.loop = asyncio.get_running_loop()
    catalog_client._pools[stub.loop] = stub
