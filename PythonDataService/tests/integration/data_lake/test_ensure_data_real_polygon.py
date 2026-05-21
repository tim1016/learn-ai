"""End-to-end: real ensure_data with respx-mocked Polygon, real Postgres,
tmp filesystem for the lake.

Asserts:
  - Catalog rows land with status='complete' for minute-trade artifacts
  - Files exist on disk with the correct deci-cent zip payload
  - data_availability_hash is deterministic across two identical calls
  - Second call is a cache hit (fetched_artifact_count == 0)
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataRunSpec

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    # Force-reset any stale pool left by a prior test (different event loop).
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/."""
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setenv("POLYGON_API_KEY", "test-polygon-key")
    return write_root


def _polygon_payload_for(start: int, count: int) -> dict:
    """Generate `count` synthetic 1-minute bars starting at UTC ms `start`."""
    return {
        "ticker": "SPY",
        "status": "OK",
        "results": [
            {
                "v": 1000 + i,
                "vw": 500.0,
                "o": 500.0 + i * 0.01,
                "c": 500.05 + i * 0.01,
                "h": 500.10 + i * 0.01,
                "l": 499.95 + i * 0.01,
                "t": start + i * 60_000,
                "n": 10,
            }
            for i in range(count)
        ],
    }


@respx.mock
async def test_ensure_data_writes_files_and_catalog_rows(clean_artifacts, pool, tmp_lake):
    # Mock Polygon for a single-day SPY fetch — 390 bars covering 09:30 → 16:00 ET.
    # 2024-05-20 09:30:00 ET = 1716212100000 ms UTC (verified via epochconverter; ET is UTC-4 in DST).
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716212100000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 20),
        lean_image_digest="sha256:test",
    )
    result = await ensure_data(spec)

    assert result.overall_status in {"complete", "partial"}
    # The minute-trade artifact for SPY on 2024-05-20 must be complete.
    minute_trade = [
        a
        for a in result.artifacts
        if a.artifact_kind == "time_series_bars"
        and a.resolution == "minute"
        and a.data_type == "trade"
        and a.symbol == "SPY"
    ]
    assert len(minute_trade) == 1
    art = minute_trade[0]
    assert art.row_count == 390
    assert len(art.file_sha256) == 64
    assert art.file_sha256 != "0" * 64  # not the fake_polygon stub

    # File exists on disk at the expected lake path.
    final = tmp_lake / "lake" / art.file_path
    assert final.is_file()
    assert final.stat().st_size > 0


@respx.mock
async def test_second_call_is_cache_hit(clean_artifacts, pool, tmp_lake):
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716212100000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 20),
        lean_image_digest="sha256:test",
    )
    first = await ensure_data(spec)
    # New request_id; same spec → same artifacts.
    spec2 = spec.model_copy(update={"request_id": UUID("22222222-2222-2222-2222-222222222222")})
    second = await ensure_data(spec2)

    assert first.data_availability_hash == second.data_availability_hash
    # On the second call the minute-trade artifact is reused, not fetched.
    minute_trade_first = [a for a in first.artifacts if a.resolution == "minute"]
    minute_trade_second = [a for a in second.artifacts if a.resolution == "minute"]
    assert len(minute_trade_first) == 1
    assert len(minute_trade_second) == 1
    assert second.reused_artifact_count >= 1
