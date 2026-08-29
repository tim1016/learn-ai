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

import asyncio
import json
import os
import re
import threading
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
from app.lean_sidecar import config as sidecar_config
from app.routers.data_lake import _bridge_ensure_fn, _bridge_status_fn

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
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    # Phase 0 reads the launcher's extracted metadata files back off its own
    # view of the shared artifacts mount (app.data_lake.lean_metadata), not
    # off the launcher's HTTP response body — point that root at a tmp_path
    # tree so _launcher_side_effect below has somewhere real to stage into.
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return artifacts_root


def _spec(symbol: str = "SPY") -> DataRunSpec:
    return DataRunSpec(
        request_id=UUID("12345678-1234-5678-1234-567812345678"),
        run_type="python_lab",
        symbols=[symbol],
        start_trading_date=date(2024, 5, 20),
        end_trading_date=date(2024, 5, 22),
        lean_image_digest="sha256:test",
    )


_MARKET_HOURS_JSON = json.dumps(
    {"entries": {"Equity-usa-[*]": {"exchange": "NYSE", "timezone": "America/New_York", "holidays": [], "earlyCloses": {}}}}
).encode("utf-8")
_SYMBOL_PROPERTIES_CSV = b"SPY,equity,usd,1,0\n"


def _stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_MARKET_HOURS_JSON)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_SYMBOL_PROPERTIES_CSV)


def _launcher_side_effect(artifacts_root: Path):
    """respx side_effect standing in for a real launcher: stages the files
    app.data_lake.lean_metadata will read back, keyed by the run_id the
    caller sent, then returns the launcher's actual (paths-only) response
    shape."""

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
        side_effect=_launcher_side_effect(tmp_lake)
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


@respx.mock
async def test_backfill_survives_a_pool_initialized_on_a_different_loop(clean_artifacts, pool, tmp_lake):
    """P1-1 regression (review round 3).

    ensure_data's asyncpg pool is keyed by the calling event loop — the
    `pool` fixture above already created one on THIS test's own loop.
    This test then runs run_backfill on a genuinely separate thread with
    its own fresh loop (mirroring app/routers/data_lake.py's
    work()/asyncio.run(_do()) inside run_in_thread's worker thread),
    wiring ensure_fn/status_fn through the exact same
    _bridge_ensure_fn/_bridge_status_fn the real job path uses to route
    every pool-touching call back onto this test's loop rather than
    paying for (and never closing) a second pool on the worker thread's
    own throwaway loop — this proves the bridge lands the calls on the
    intended loop and the backfill still completes.
    """
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-20.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload(1716211800000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-21.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload(1716298200000))
    )
    respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/2024-05-22.*").mock(
        return_value=httpx.Response(200, json=_polygon_ok_payload(1716470400000))
    )

    this_loop = asyncio.get_running_loop()
    bridged_ensure_fn = _bridge_ensure_fn(this_loop)
    bridged_status_fn = _bridge_status_fn(this_loop)

    outcome: dict[str, object] = {}
    job_done = threading.Event()

    def worker() -> None:
        async def _do():
            return await run_backfill(_spec(), ensure_fn=bridged_ensure_fn, status_fn=bridged_status_fn)

        try:
            outcome["result"] = asyncio.run(_do())
        except Exception as exc:  # the exact failure mode this test guards against
            outcome["error"] = exc
        finally:
            job_done.set()

    threading.Thread(target=worker, name="test-cross-loop-backfill").start()

    # Poll with awaited sleeps rather than a blocking thread.join(): the
    # bridge's run_coroutine_threadsafe calls need THIS loop to keep
    # spinning (processing its scheduled-callback queue) to ever run —
    # a synchronous join() here would freeze the very loop the worker
    # thread is waiting on.
    for _ in range(1000):  # up to ~10s
        if job_done.is_set():
            break
        await asyncio.sleep(0.01)
    assert job_done.is_set(), "background job did not finish in time"

    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]

    result = outcome["result"]
    assert result.overall_status == "complete"
    assert result.days_completed == 3
    assert result.failures == []
