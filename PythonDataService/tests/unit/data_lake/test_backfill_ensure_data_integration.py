"""run_backfill() against the real ensure_data() seam (#1836).

Everything else in this suite injects a fake ensure_fn to isolate
run_backfill's own orchestration (tests/unit/data_lake/test_backfill.py).
This file proves the real wiring: the per-day sub-specs run_backfill
builds are valid DataRunSpecs that ensure_data actually accepts, against
the real Postgres catalog (claim/complete) with Polygon + the LEAN
launcher mocked via respx — same fixture pattern as
tests/unit/data_lake/test_ensure_data.py.

Skips when POSTGRES_URL is unconfigured, matching every other
Postgres-backed test in this package.
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
from app.data_lake.backfill import BackfillDayProgress, run_backfill
from app.data_lake.types import DataRunSpec

pytestmark = pytest.mark.asyncio


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured — skipping DB-dependent route test")
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
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    return write_root


def _spec(symbol: str = "SPY") -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=[symbol],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 22),
        lean_image_digest="sha256:test",
    )


def _launcher_response() -> dict:
    mh = json.dumps(
        {"entries": {"Equity-usa-[*]": {"exchange": "NYSE", "timezone": "America/New_York", "holidays": [], "earlyCloses": {}}}}
    ).encode("utf-8")
    sp = b"SPY,equity,usd,1,0\n"
    return {
        "market_hours_database_b64": base64.b64encode(mh).decode("ascii"),
        "symbol_properties_database_b64": base64.b64encode(sp).decode("ascii"),
        "image_digest_used": "sha256:test",
    }


def _polygon_ok_payload(bar_start_ms: int) -> dict:
    return {
        "ticker": "SPY",
        "status": "OK",
        "results": [
            {"v": 1000, "vw": 500.0, "o": 500.0, "c": 500.05, "h": 500.10, "l": 499.95, "t": bar_start_ms + i * 60_000, "n": 10}
            for i in range(390)
        ],
    }


@respx.mock
async def test_run_backfill_against_real_ensure_data_reports_per_day_progress(clean_artifacts, pool, tmp_lake):
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(200, json=_launcher_response())
    )
    # 2024-05-20/21/22 09:30 ET in ms UTC.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-20.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload(1716211800000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-21.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload(1716298200000))
    )
    # 05-22 returns empty results — simulates an unknown-symbol-shaped gap
    # (provider_no_data), proving a real typed failure survives run_backfill's
    # fold into the final result and the per-day progress callback.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-22.*").mock(
        return_value=httpx.Response(200, json={"ticker": "SPY", "status": "OK", "results": []})
    )

    progress_events: list[BackfillDayProgress] = []
    result = await run_backfill(_spec(), on_day_progress=progress_events.append)

    assert result.total_sessions == 3
    assert [p.trading_date for p in progress_events] == [date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22)]
    assert result.days_completed == 2
    assert result.days_with_failures == 1
    assert result.overall_status == "partial"
    assert any(f.reason == "provider_no_data" for f in result.failures)
    # Per-day progress for the failing day carries the same typed reason.
    failing_day = next(p for p in progress_events if p.trading_date == date(2024, 5, 22))
    assert [f.reason for f in failing_day.failures] == ["provider_no_data"]

    # No factor/map/daily-trade artifacts were produced by the per-day
    # sub-calls (they opt out per _day_sub_spec) — every artifact is a
    # per-day minute-trade or quote bar.
    assert result.fetched_artifact_count >= 2  # at least the two successful days' minute bars
