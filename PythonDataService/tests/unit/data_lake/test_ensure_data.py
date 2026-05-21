"""Unit tests for ensure_data dispatch logic.

Slice 1a: these tests were fixture-backed (no catalog, no Polygon).
Slice 1b: ensure_data now dispatches minute-trade through the real pipeline,
so tests that include minute-trade artifacts need pool management and a
respx-mocked Polygon response. The Slice 1a assertion invariants are
preserved; only the test infrastructure is updated.
"""

from __future__ import annotations

import os
from datetime import date
from uuid import UUID

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.ensure_data import ensure_data
from app.data_lake.types import DataRunSpec


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


def _spec(symbols: list[str]) -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=symbols,
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        lean_image_digest="sha256:test",
    )


def _polygon_ok_payload(ticker: str) -> dict:
    # 2024-05-20 09:30:00 ET (DST) = 1716212100000 ms UTC
    bar_start_ms = 1716212100000
    return {
        "ticker": ticker,
        "status": "OK",
        "results": [
            {
                "v": 1000,
                "vw": 500.0,
                "o": 500.0,
                "c": 500.05,
                "h": 500.10,
                "l": 499.95,
                "t": bar_start_ms + i * 60_000,
                "n": 10,
            }
            for i in range(390)
        ],
    }


@respx.mock
@pytest.mark.asyncio
async def test_known_symbol_produces_complete_result(clean_artifacts, pool, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    # Catch-all mock: any Polygon aggs call for SPY returns 390 bars.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    result = await ensure_data(_spec(["SPY"]))
    assert result.overall_status == "complete"
    assert result.failures == []
    assert len(result.artifacts) > 0
    assert all(a.symbol in {None, "SPY"} for a in result.artifacts)


@respx.mock
@pytest.mark.asyncio
async def test_unknown_symbol_produces_partial_with_failures(clean_artifacts, pool, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    # UNKNOWN symbol: Polygon returns no bars → provider_no_data failure.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/UNKNOWN/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "UNKNOWN", "status": "OK", "results": []})
    )

    result = await ensure_data(_spec(["UNKNOWN"]))
    assert result.overall_status in {"partial", "failed"}
    assert len(result.failures) > 0
    # Slice 1b: unknown symbols fail with provider_no_data (Polygon returns empty),
    # replacing the Slice 1a fake_polygon unknown_symbol reason.
    assert any(f.reason in {"unknown_symbol", "provider_no_data"} for f in result.failures)


@respx.mock
@pytest.mark.asyncio
async def test_two_identical_calls_produce_same_availability_hash(clean_artifacts, pool, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    a = await ensure_data(_spec(["SPY"]))
    # Second call: same artifacts (cache hits) → same hash.
    spec2 = _spec(["SPY"])
    b = await ensure_data(spec2)
    assert a.data_availability_hash == b.data_availability_hash
