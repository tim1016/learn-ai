"""Shared fixtures for research module tests.

The live-Postgres fixtures (``conn``, ``second_conn``, ``unique``) run only
against a database explicitly attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``, the same signal the data-lake catalog tests
require). The Python-owned research tables are ensured once and never
dropped: every test works on ids and symbols of its own, so the suites are
safe under xdist's ``--dist load`` (CI runs the change-driven paths in
parallel). Shared by the Grid Search, Walk-Forward Study and Recency suites.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import numpy as np
import pytest

from app.research.persistence.schema import ensure_schema


@pytest.fixture
def sample_bars_single_day() -> list[dict]:
    """Generate 200 1-minute bars within a single trading day.

    All bars share the same date so cross-day contamination logic can
    be tested with a separate multi-day fixture.
    """
    bars: list[dict] = []
    base_price = 150.0
    base_ts = 1704117000000  # 2024-01-01 13:50 UTC (inside trading hours)

    rng = np.random.default_rng(42)

    for i in range(200):
        noise = rng.normal(0, 0.3)
        trend = np.sin(i * 0.05) * 2
        price = base_price + trend + noise

        bars.append(
            {
                "timestamp": base_ts + i * 60_000,  # 1-minute spacing
                "open": round(price - 0.05, 4),
                "high": round(price + 0.3, 4),
                "low": round(price - 0.3, 4),
                "close": round(price, 4),
                "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
            }
        )

    return bars


@pytest.fixture
def sample_bars_multi_day() -> list[dict]:
    """Generate bars spanning 3 trading days (50 bars per day).

    Timestamps jump across midnight boundaries to test cross-day masking.
    """
    bars: list[dict] = []
    rng = np.random.default_rng(123)

    day_starts = [
        1704117000000,  # 2024-01-01 13:50 UTC
        1704203400000,  # 2024-01-02 13:50 UTC
        1704289800000,  # 2024-01-03 13:50 UTC
    ]
    base_price = 150.0

    for day_start in day_starts:
        for i in range(50):
            noise = rng.normal(0, 0.2)
            price = base_price + i * 0.01 + noise
            bars.append(
                {
                    "timestamp": day_start + i * 60_000,
                    "open": round(price - 0.05, 4),
                    "high": round(price + 0.3, 4),
                    "low": round(price - 0.3, 4),
                    "close": round(price, 4),
                    "volume": round(1_000_000 + rng.normal(0, 50_000), 2),
                }
            )

    return bars


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
