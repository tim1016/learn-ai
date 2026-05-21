"""End-to-end test of POST /api/data-lake/ensure-data with the feature flag on.

The data-lake router is behind DATA_LAKE_ENABLED.  Each test builds a minimal
FastAPI app from the data_lake router directly — no app.main reload needed.
The flag-off test creates a bare FastAPI app WITHOUT including the router.
This approach avoids any importlib.reload side effects on the shared session.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.data_lake import router as data_lake_router

pytestmark = pytest.mark.asyncio


def _make_app(*, include_data_lake: bool) -> FastAPI:
    """Minimal FastAPI app that mirrors main.py's conditional router wiring."""
    app = FastAPI()
    if include_data_lake:
        app.include_router(data_lake_router)
    return app


async def test_route_404_when_flag_off():
    """Route is absent when the router is not registered (flag-off behaviour)."""
    flag_off_app = _make_app(include_data_lake=False)
    async with AsyncClient(transport=ASGITransport(app=flag_off_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json={})
    assert r.status_code == 404


async def test_post_ensure_data_known_symbol():
    flag_on_app = _make_app(include_data_lake=True)
    payload = {
        "request_id": str(uuid4()),
        "run_type": "python_lab",
        "symbols": ["SPY"],
        "start_trading_date": "2024-05-20",
        "end_trading_date": "2024-05-24",
        "lean_image_digest": "sha256:test",
    }
    async with AsyncClient(transport=ASGITransport(app=flag_on_app), base_url="http://test") as client:
        r = await client.post("/api/data-lake/ensure-data", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "complete"
    assert body["data_availability_hash"]


async def test_post_ensure_data_422_on_bad_symbol():
    flag_on_app = _make_app(include_data_lake=True)
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
