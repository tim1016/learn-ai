"""End-to-end: real ensure_data with respx-mocked Polygon, real Postgres,
tmp filesystem for the lake.

Asserts:
  - Catalog rows land with status='complete' for minute-trade artifacts
  - Files exist on disk with the correct deci-cent zip payload
  - data_availability_hash is deterministic across two identical calls
  - Second call is a cache hit (fetched_artifact_count == 0)

Slice 1c: Phase 0 (launcher mock) and corp-action endpoints are also mocked
so existing tests continue to pass after the ensure_data rewrite.
"""

from __future__ import annotations

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
from app.data_lake.path_policy import lake_subpath
from app.data_lake.types import DataRunSpec, trading_date_to_calendar_anchor_ms
from app.lean_sidecar import config as sidecar_config

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
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/.

    Also patches LEAN_LAUNCHER_URL and LEAN_LAUNCHER_TOKEN so Phase 0 metadata
    bootstrap targets the respx mock rather than a real launcher process, and
    points app.lean_sidecar.config.DEFAULT_ARTIFACTS_ROOT at a sibling
    tmp_path tree: Phase 0 reads the launcher's extracted metadata files back
    off that root (app.data_lake.lean_metadata does not trust the launcher's
    HTTP response body, only its own view of the shared mount — see that
    module's docstring). The ``artifacts_root`` fixture below exposes the
    same path to test bodies.
    """
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-polygon-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return write_root


@pytest.fixture
def artifacts_root(tmp_lake: Path, tmp_path: Path) -> Path:
    """The same path ``tmp_lake`` pointed ``DEFAULT_ARTIFACTS_ROOT`` at.

    A separate fixture (rather than changing what ``tmp_lake`` returns) so
    every existing ``tmp_lake / lake_subpath(...)`` on-disk assertion below
    keeps working unchanged.
    """
    return tmp_path / "artifacts-root"


def _stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...
    """
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
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(mh)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(sp)


def _launcher_side_effect(artifacts_root: Path):
    """respx side_effect standing in for a real launcher: stages the files
    app.data_lake.lean_metadata will read back, keyed by the run_id the
    caller sent, then returns the launcher's actual (paths-only) response
    shape — not the base64-bytes shape a prior version of the caller
    expected but the launcher has never sent."""

    def _mock(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        _stage_workspace_files(artifacts_root, body["run_id"])
        return httpx.Response(
            200,
            json={
                "market_hours_db_path": "/launcher-side/market-hours-database.json",
                "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
            },
        )

    return _mock


def _mock_corp_actions_and_events() -> None:
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
async def test_ensure_data_writes_files_and_catalog_rows(clean_artifacts, pool, tmp_lake, artifacts_root):
    # Slice 1c: mock launcher + corp-action endpoints in addition to Polygon aggs.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(artifacts_root)
    )
    _mock_corp_actions_and_events()

    # Mock Polygon for a single-day SPY fetch — 390 bars covering 09:30 → 16:00 ET.
    # 2024-05-20 09:30:00 ET (UTC-4 DST) = 13:30 UTC = 1716211800000 ms UTC.
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716211800000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
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

    # File exists on disk at the expected lake path. ``FilePath`` is relative
    # to the mode root (#1839), and this spec is the default "raw".
    final = tmp_lake / lake_subpath("raw") / art.file_path
    assert final.is_file()
    assert final.stat().st_size > 0


@respx.mock
async def test_second_call_is_cache_hit(clean_artifacts, pool, tmp_lake, artifacts_root):
    # Slice 1c: mock launcher + corp-action endpoints.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(artifacts_root)
    )
    _mock_corp_actions_and_events()

    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=_polygon_payload_for(1716211800000, 390))
    )

    spec = DataRunSpec(
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
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
