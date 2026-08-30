"""End-to-end test of POST /api/data-lake/ensure-data with the feature flag on.

The data-lake router is behind DATA_LAKE_ENABLED.  Each test builds a minimal
FastAPI app from the data_lake router directly — no app.main reload needed.
The flag-off test creates a bare FastAPI app WITHOUT including the router.
This approach avoids any importlib.reload side effects on the shared session.

``test_post_ensure_data_known_symbol`` mocks the LEAN launcher and Polygon at
the HTTP layer with respx, same pattern as test_ensure_data_all_kinds.py —
this test used to only run when a developer had POSTGRES_URL configured
locally against a real launcher/Polygon; #1862 gave CI its own Postgres, so
it now runs unconditionally there and must not depend on live external
services (LEAN_DATA_WRITE_ROOT's default, /lean-data-writer, doesn't exist on
a bare CI runner either — tmp_lake below points it at a real tmp dir).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.data_lake import catalog_client
from app.lean_sidecar import config as sidecar_config

pytestmark = pytest.mark.asyncio


def _requires_postgres():
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured — skipping DB-dependent route test")


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
    """Same fixture as test_ensure_data_all_kinds.py: point the write root and
    the LEAN metadata artifacts root at real tmp dirs, and the launcher/Polygon
    settings at values the respx mocks below intercept."""
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
    return artifacts_root


def _minimal_market_hours_json() -> bytes:
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
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_minimal_market_hours_json())
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_minimal_symbol_properties_csv())


def _launcher_side_effect(artifacts_root: Path):
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


def _polygon_aggs_for(start_ms: int) -> dict:
    return {
        "ticker": "SPY",
        "status": "OK",
        "results": [
            {"v": 1000, "vw": 500.0, "o": 500.0, "c": 500.05, "h": 500.10, "l": 499.95, "t": start_ms + i * 60_000, "n": 10}
            for i in range(390)
        ],
    }


async def test_route_404_when_flag_off(make_data_lake_app):
    """Route is absent when the router is not registered (flag-off behaviour)."""
    flag_off_app = make_data_lake_app(include_data_lake=False)
    async with AsyncClient(transport=ASGITransport(app=flag_off_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json={})
    assert r.status_code == 404


@respx.mock
async def test_post_ensure_data_known_symbol(make_data_lake_app, tmp_lake):
    _requires_postgres()
    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    # 2024-05-20..24 09:30 ET in ms UTC — same window the request body asks for.
    for trading_date, start_ms in {
        "2024-05-20": 1716211800000,
        "2024-05-21": 1716298200000,
        "2024-05-22": 1716384600000,
        "2024-05-23": 1716471000000,
        "2024-05-24": 1716557400000,
    }.items():
        respx.get(url__regex=rf"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/{trading_date}.*").mock(
            return_value=httpx.Response(200, json=_polygon_aggs_for(start_ms))
        )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/SPY/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )

    flag_on_app = make_data_lake_app(include_data_lake=True)
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
            r = await client.post("/api/data-lake/ensure-data", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["overall_status"] in {"complete", "partial"}
        assert body["data_availability_hash"]
    finally:
        # ensure_data calls init_pool(); close it so subsequent tests get a fresh pool.
        await catalog_client.close_pool()


async def test_post_ensure_data_422_on_bad_symbol(make_data_lake_app):
    flag_on_app = make_data_lake_app(include_data_lake=True)
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["spy"],  # lowercase — rejected by validator
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 422
