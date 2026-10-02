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
make that structurally impossible to repeat. These tests prove the wiring with
a stub pool injected into ``catalog_client._pools`` for the first test's loop:
if the autouse fixture is removed or stops running teardown on the test's own
loop, the stub is never closed and the second test fails. No POSTGRES_URL is
needed, so this runs in every shard, not just the daily.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.data_lake import catalog_client

_closed_pools: list[_StubPool] = []


class _StubPool:
    """The slice of asyncpg.Pool close_pool touches: an awaitable close()."""

    async def close(self) -> None:
        _closed_pools.append(self)


async def test_a_pool_left_on_the_tests_loop_is_closed_by_teardown() -> None:
    """Plant a pool on this test's loop, as service code does via init_pool."""
    catalog_client._pools[asyncio.get_running_loop()] = _StubPool()


def test_the_previous_tests_pool_was_closed_before_its_loop_died() -> None:
    """The autouse teardown must have closed it on that loop (#2809).

    Ordering is file order: the async test above ran first, planted a stub
    pool on its loop, and finished. Without the fixture the stub would still
    be open — exactly the leak that exhausted the daily run's Postgres.
    """
    assert len(_closed_pools) == 1, "the pool planted by the previous test was never closed"
    stub: Any = _closed_pools[0]
    remaining = [pool for pool in catalog_client._pools.values() if pool is stub]
    assert remaining == [], "the closed pool is still registered for its (dead) loop"
