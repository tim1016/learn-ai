"""End-to-end integration test for ensure_data Slice 1c — all artifact kinds.

Mocks:
  - LEAN launcher POST /extract-metadata → sentinel bytes for both metadata files
  - Polygon GET /v2/aggs/... → 390 synthetic bars per trading day
  - Polygon GET /v3/reference/splits → empty results
  - Polygon GET /v3/reference/dividends → empty results
  - Polygon GET /v3/reference/tickers/SPY/events → empty results

Asserts:
  - overall_status == 'complete'
  - 15 artifacts total:
      2 metadata (market-hours + symbol-properties)
      5 minute-trade (one per session, Mon 20 May – Fri 24 May 2024)
      5 minute-quote (derived from same-day trade)
      1 daily-trade (derived from all 5 minute-trade)
      1 factor_file
      1 map_file
  - All 15 artifact files exist on disk under tmp_lake/lake/
  - Second call: identical data_availability_hash + fetched_artifact_count == 0

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5, 4.6
"""

from __future__ import annotations

import base64
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
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-polygon-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    return write_root


# -----------------------------------------------------------------------
# Polygon mock payloads
# -----------------------------------------------------------------------

# 2024-05-20 09:30:00 ET  = UTC 2024-05-20 13:30:00 = 1716211800000 ms UTC
# (UTC-4 EDT: 09:30 ET = 13:30 UTC)
_DAY_OFFSETS_MS = {
    date(2024, 5, 20): 1716211800000,
    date(2024, 5, 21): 1716298200000,  # +86400000
    date(2024, 5, 22): 1716384600000,
    date(2024, 5, 23): 1716471000000,
    date(2024, 5, 24): 1716557400000,
}


def _polygon_aggs_for(start_ms: int, count: int = 390) -> dict:
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
                "t": start_ms + i * 60_000,
                "n": 10,
            }
            for i in range(count)
        ],
    }


def _minimal_market_hours_json() -> bytes:
    """Minimal LEAN market-hours-database.json with no extra holidays in the test window."""
    import json as _json

    return _json.dumps(
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


def _minimal_symbol_properties_csv() -> bytes:
    return b"SPY,equity,usd,1,0\n"


def _launcher_response() -> dict:
    mh = _minimal_market_hours_json()
    sp = _minimal_symbol_properties_csv()
    return {
        "market_hours_database_b64": base64.b64encode(mh).decode("ascii"),
        "symbol_properties_database_b64": base64.b64encode(sp).decode("ascii"),
        "image_digest_used": "sha256:test-image-digest",
    }


def _make_spec(request_id: str, include_quote: bool = True) -> DataRunSpec:
    data_types = ["trade", "quote"] if include_quote else ["trade"]
    return DataRunSpec(
        request_id=UUID(request_id),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 24),
        data_types=data_types,
        lean_image_digest="sha256:test-image-digest",
    )


# -----------------------------------------------------------------------
# Test: full write cycle, all 15 artifacts
# -----------------------------------------------------------------------


@respx.mock
async def test_ensure_data_all_kinds_complete(clean_artifacts, pool, tmp_lake):
    """Run ensure_data for SPY over 2024-05-20 to 2024-05-24.

    Expects 15 artifacts, all complete, all files on disk.
    """
    # Mock launcher /extract-metadata
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )

    # Mock Polygon aggregate fetches for all 5 days
    for trading_date, start_ms in _DAY_OFFSETS_MS.items():
        respx.get(
            url__regex=(
                rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/"
                rf"{trading_date.strftime('%Y-%m-%d')}/.*"
            )
        ).mock(return_value=httpx.Response(200, json=_polygon_aggs_for(start_ms)))

    # Mock splits / dividends / ticker-events (empty — SPY is stable)
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )

    spec = _make_spec("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    result = await ensure_data(spec)

    assert result.overall_status == "complete", (
        f"expected 'complete' but got {result.overall_status!r}; failures: {result.failures}"
    )

    # 15 artifacts total.
    assert len(result.artifacts) == 15, f"expected 15 artifacts, got {len(result.artifacts)}: " + ", ".join(
        f"{a.artifact_kind}/{a.resolution}/{a.data_type}/{a.trading_date}" for a in result.artifacts
    )

    # Breakdown by kind.
    metadata = [a for a in result.artifacts if a.artifact_kind == "metadata"]
    minute_trade = [
        a
        for a in result.artifacts
        if a.artifact_kind == "time_series_bars" and a.resolution == "minute" and a.data_type == "trade"
    ]
    minute_quote = [
        a
        for a in result.artifacts
        if a.artifact_kind == "time_series_bars" and a.resolution == "minute" and a.data_type == "quote"
    ]
    daily_trade = [a for a in result.artifacts if a.artifact_kind == "time_series_bars" and a.resolution == "daily"]
    factor_files = [a for a in result.artifacts if a.artifact_kind == "factor_file"]
    map_files = [a for a in result.artifacts if a.artifact_kind == "map_file"]

    assert len(metadata) == 2, f"expected 2 metadata, got {len(metadata)}"
    assert len(minute_trade) == 5, f"expected 5 minute-trade, got {len(minute_trade)}"
    assert len(minute_quote) == 5, f"expected 5 minute-quote, got {len(minute_quote)}"
    assert len(daily_trade) == 1, f"expected 1 daily-trade, got {len(daily_trade)}"
    assert len(factor_files) == 1, f"expected 1 factor_file, got {len(factor_files)}"
    assert len(map_files) == 1, f"expected 1 map_file, got {len(map_files)}"

    # All files must exist on disk.
    lake_root = tmp_lake / "lake"
    for art in result.artifacts:
        on_disk = lake_root / Path(*art.file_path.replace("\\", "/").split("/"))
        assert on_disk.is_file(), f"missing on disk: {art.file_path}"
        assert on_disk.stat().st_size > 0, f"empty file: {art.file_path}"

    # All artifacts have real (non-zero) sha256 digests.
    for art in result.artifacts:
        assert len(art.file_sha256) == 64, f"bad sha256 on {art.file_path}"
        assert art.file_sha256 != "0" * 64, f"stub sha on {art.file_path}"

    # data_contract_hash must be 64-hex-char for all artifacts.
    for art in result.artifacts:
        assert len(art.data_contract_hash) == 64, f"bad dch on {art.file_path}"
        assert art.data_contract_hash != "x" * 64, f"placeholder dch on {art.file_path}"


# -----------------------------------------------------------------------
# Test: idempotent re-run (cache hit)
# -----------------------------------------------------------------------


@respx.mock
async def test_ensure_data_second_call_is_cache_hit(clean_artifacts, pool, tmp_lake):
    """Second ensure_data call with the same content spec is a pure cache hit."""
    # Mock launcher — called on first run; should not be called on second.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )

    for trading_date, start_ms in _DAY_OFFSETS_MS.items():
        respx.get(
            url__regex=(
                rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/"
                rf"{trading_date.strftime('%Y-%m-%d')}/.*"
            )
        ).mock(return_value=httpx.Response(200, json=_polygon_aggs_for(start_ms)))

    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )

    spec1 = _make_spec("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    first = await ensure_data(spec1)
    assert first.overall_status == "complete", f"first call failed: {first.failures}"

    # Second call: different request_id, same content spec.
    spec2 = spec1.model_copy(update={"request_id": UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")})
    second = await ensure_data(spec2)
    assert second.overall_status == "complete", f"second call failed: {second.failures}"

    # data_availability_hash must be identical (same artifacts, same bytes on disk).
    assert first.data_availability_hash == second.data_availability_hash

    # Second call must not fetch any new artifacts.
    assert second.fetched_artifact_count == 0, f"expected 0 fetched on second call, got {second.fetched_artifact_count}"

    # 15 artifacts on both calls.
    assert len(first.artifacts) == 15
    assert len(second.artifacts) == 15
