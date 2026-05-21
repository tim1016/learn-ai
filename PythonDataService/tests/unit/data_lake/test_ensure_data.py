"""Unit tests for ensure_data dispatch logic.

Slice 1a: these tests were fixture-backed (no catalog, no Polygon).
Slice 1b: ensure_data now dispatches minute-trade through the real pipeline,
so tests that include minute-trade artifacts need pool management and a
respx-mocked Polygon response. The Slice 1a assertion invariants are
preserved; only the test infrastructure is updated.
Slice 1c: ensure_data now performs Phase 0 metadata bootstrap (calls the LEAN
launcher) and dispatches all artifact kinds through real implementations.
Tests updated to mock the launcher endpoint + corp-action endpoints.
"""

from __future__ import annotations

import base64
import json
import os
import re
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
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    return write_root


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
    # 2024-05-20 09:30:00 ET (DST) = 1716211800000 ms UTC (09:30 ET = 13:30 UTC = 13:30 * 3600 * 1000 + epoch)
    bar_start_ms = 1716211800000
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


def _launcher_response() -> dict:
    mh = json.dumps(
        {
            "entries": {
                "Equity-usa-[*]": {
                    "exchange": "NYSE",
                    "timezone": "America/New_York",
                    "holidays": [],
                    "earlyCloses": {},
                }
            }
        }
    ).encode("utf-8")
    sp = b"SPY,equity,usd,1,0\n"
    return {
        "market_hours_database_b64": base64.b64encode(mh).decode("ascii"),
        "symbol_properties_database_b64": base64.b64encode(sp).decode("ascii"),
        "image_digest_used": "sha256:test",
    }


def _mock_corpus_actions_and_events() -> None:
    """Register respx mocks for splits, dividends, ticker-events (all empty)."""
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/.*/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )


@respx.mock
@pytest.mark.asyncio
async def test_known_symbol_produces_complete_result(clean_artifacts, pool, tmp_lake):
    # Slice 1c: mock launcher + corp-action endpoints in addition to Polygon aggs.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )
    _mock_corpus_actions_and_events()
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
async def test_unknown_symbol_produces_partial_with_failures(clean_artifacts, pool, tmp_lake):
    # Slice 1c: mock launcher + corp-action endpoints.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )
    _mock_corpus_actions_and_events()
    # UNKNOWN symbol: Polygon returns no bars → provider_no_data failure.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/UNKNOWN/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json={"ticker": "UNKNOWN", "status": "OK", "results": []})
    )

    result = await ensure_data(_spec(["UNKNOWN"]))
    assert result.overall_status in {"partial", "failed"}
    assert len(result.failures) > 0
    # Slice 1b/1c: unknown symbols fail with provider_no_data (Polygon returns empty).
    assert any(f.reason in {"unknown_symbol", "provider_no_data"} for f in result.failures)


@respx.mock
@pytest.mark.asyncio
async def test_two_identical_calls_produce_same_availability_hash(clean_artifacts, pool, tmp_lake):
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )
    _mock_corpus_actions_and_events()
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload("SPY"))
    )

    a = await ensure_data(_spec(["SPY"]))
    # Second call: same artifacts (cache hits) → same hash.
    spec2 = _spec(["SPY"])
    b = await ensure_data(spec2)
    assert a.data_availability_hash == b.data_availability_hash
