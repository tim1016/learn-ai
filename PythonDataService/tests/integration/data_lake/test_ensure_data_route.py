"""End-to-end test of POST /api/data-lake/ensure-data with the feature flag on."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

# Force the flag on for this test module BEFORE main is imported.
os.environ["DATA_LAKE_ENABLED"] = "true"

from app.main import app

pytestmark = pytest.mark.asyncio


async def test_route_404_when_flag_off(monkeypatch):
    """Sanity check: when flag is off, the route is not registered."""
    from importlib import reload

    monkeypatch.setenv("DATA_LAKE_ENABLED", "false")
    import app.config as config_module
    import app.main as main_module

    reload(config_module)
    reload(main_module)
    fresh_app = main_module.app

    async with AsyncClient(transport=ASGITransport(app=fresh_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json={})
        assert r.status_code == 404

    # Restore the flag for the rest of the module.
    monkeypatch.setenv("DATA_LAKE_ENABLED", "true")
    reload(config_module)
    reload(main_module)


async def test_post_ensure_data_known_symbol():
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["data_availability_hash"]


async def test_post_ensure_data_422_on_bad_symbol():
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["spy"],  # lowercase — rejected by validator
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 422
