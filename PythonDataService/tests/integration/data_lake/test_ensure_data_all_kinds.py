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
from app.data_lake.types import DataRunSpec
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
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/.

    Also points app.lean_sidecar.config.DEFAULT_ARTIFACTS_ROOT at a sibling
    tmp_path tree: Phase 0 reads the launcher's extracted metadata files back
    off that root (app.data_lake.lean_metadata does not trust the launcher's
    HTTP response body, only its own view of the shared mount — see that
    module's docstring), so the respx launcher mock must stage files there
    rather than return them base64-encoded. The ``artifacts_root`` fixture
    below exposes the same path to test bodies.
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

# A distinct holiday-free week for test_ensure_data_second_call_is_cache_hit.
# The two tests in this module share no other state, but under
# pytest-xdist they can run concurrently on different workers against the
# same Postgres catalog. test_ensure_data_second_call_is_cache_hit uses a
# distinct trading-date range (this dict), a distinct symbol (QQQ, see
# that test), and a distinct lean_image_digest (also that test) so it
# shares no artifact identity at all with test_ensure_data_all_kinds_complete
# — daily-trade/minute-bar identity includes TradingDate, corp-action
# identity (factor_file/map_file) includes Symbol but not TradingDate, and
# metadata identity is keyed by lean_image_digest alone, so all three had
# to move to fully avoid a claim race (ensure_data.py's non-minute-bar/
# non-metadata claim paths have no reclaim-on-failure — see
# app.data_lake.ensure_data's "polling not implemented in Slice 1c").
# 2024-06-03 09:30:00 ET = UTC 2024-06-03 13:30:00 = 1717421400000 ms UTC
_SECOND_CALL_DAY_OFFSETS_MS = {
    date(2024, 6, 3): 1717421400000,
    date(2024, 6, 4): 1717507800000,  # +86400000
    date(2024, 6, 5): 1717594200000,
    date(2024, 6, 6): 1717680600000,
    date(2024, 6, 7): 1717767000000,
}


def _polygon_aggs_for(start_ms: int, count: int = 390, ticker: str = "SPY") -> dict:
    return {
        "ticker": ticker,
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
    return json.dumps(
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


def _stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_minimal_market_hours_json())
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_minimal_symbol_properties_csv())


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


def _make_spec(
    request_id: str,
    include_quote: bool = True,
    start_date: date = date(2024, 5, 20),
    end_date: date = date(2024, 5, 24),
    symbol: str = "SPY",
    lean_image_digest: str = "sha256:test-image-digest",
) -> DataRunSpec:
    data_types = ["trade", "quote"] if include_quote else ["trade"]
    return DataRunSpec(
        request_id=UUID(request_id),
        run_type="python_lab",
        symbols=[symbol],
        start_trading_date=start_date,
        end_trading_date=end_date,
        data_types=data_types,
        lean_image_digest=lean_image_digest,
    )


# -----------------------------------------------------------------------
# Test: full write cycle, all 15 artifacts
# -----------------------------------------------------------------------


@respx.mock
async def test_ensure_data_all_kinds_complete(clean_artifacts, pool, tmp_lake, artifacts_root):
    """Run ensure_data for SPY over 2024-05-20 to 2024-05-24.

    Expects 15 artifacts, all complete, all files on disk.
    """
    # Mock launcher /extract-metadata
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(artifacts_root)
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

    # All files must exist on disk. ``FilePath`` is relative to the *mode*
    # root, not the lake container -- #1839 put an adjustment segment above
    # the LEAN tree, and this spec is the default "raw".
    lake_root = tmp_lake / lake_subpath("raw")
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
async def test_ensure_data_second_call_is_cache_hit(clean_artifacts, pool, tmp_lake, artifacts_root):
    """Second ensure_data call with the same content spec is a pure cache hit.

    Uses a distinct week (_SECOND_CALL_DAY_OFFSETS_MS) from
    test_ensure_data_all_kinds_complete's — see that constant's docstring.
    """
    # Mock launcher — called on first run; should not be called on second.
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(artifacts_root)
    )

    for trading_date, start_ms in _SECOND_CALL_DAY_OFFSETS_MS.items():
        respx.get(
            url__regex=(
                rf"https://api\.polygon\.io/v2/aggs/ticker/QQQ/range/1/minute/"
                rf"{trading_date.strftime('%Y-%m-%d')}/.*"
            )
        ).mock(return_value=httpx.Response(200, json=_polygon_aggs_for(start_ms, ticker="QQQ")))

    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/QQQ/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )

    spec1 = _make_spec(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        start_date=date(2024, 6, 3),
        end_date=date(2024, 6, 7),
        symbol="QQQ",
        lean_image_digest="sha256:test-image-digest-second-call",
    )
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
