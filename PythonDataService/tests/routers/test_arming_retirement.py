"""Retired arming cannot be reached by HTTP, fleet dispatch or executable CLI."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.main import app


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/brokers/alpaca/accounts/9LIVE0001/bots/test-bot/arming"),
    ("POST", "/api/brokers/alpaca/accounts/9LIVE0001/bots/test-bot/arming/plan"),
    ("POST", "/api/brokers/alpaca/accounts/9LIVE0001/bots/test-bot/arming/apply"),
    ("POST", "/api/brokers/alpaca/accounts/9LIVE0001/bots/test-bot/arming/disarm"),
])
async def test_no_arming_route_or_fleet_operation_survives(method: str, path: str) -> None:
    isolated = FastAPI()
    isolated.router.routes.extend(app.router.routes)
    async with AsyncClient(transport=ASGITransport(app=isolated), base_url="http://test") as client:
        response = await client.request(method, path, json={})
    assert response.status_code == 404
    assert not any("arming" in operation.operation_id or "arming" in operation.path_template for operation in ALPACA_OPERATIONS)
    root = Path(__file__).resolve().parents[2]
    assert not (root / "scripts/manage_alpaca_arming.py").exists()
