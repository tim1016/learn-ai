"""Offline middleware qualification: reject stale pins before an effect."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.fleet.agent_identity import (
    SERVED_IDENTITY_STATE_KEY,
    FleetIdentityMiddleware,
)
from app.config import fleet_settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "expected"),
    [({}, 200), ({"x-fleet-clerk-id": "lane-b"}, 409),
     ({"x-fleet-broker": "wrong"}, 409),
     ({"x-fleet-routing-epoch": "6"}, 409),
     ({"x-fleet-binding-generation": "2"}, 409),
     ({"x-fleet-coordinator-token": "wrong"}, 400)],
)
async def test_identity_before_effect(monkeypatch, changed, expected):
    """A matching positive control distinguishes fencing from blanket refusal."""
    monkeypatch.setattr(fleet_settings, "COORDINATOR_SERVICE_TOKEN", "synthetic-token")
    app = FastAPI()
    app.add_middleware(FleetIdentityMiddleware, refuse_unpinned_mutations=True)
    setattr(app.state, SERVED_IDENTITY_STATE_KEY, lambda: {
        "broker": "alpaca", "clerk_id": "lane-a",
        "routing_epoch": 7, "binding_generation": 3,
    })
    effects = []

    @app.post("/effect")
    async def effect():
        effects.append("executed")
        return {"accepted": True}

    headers = {
        "x-fleet-broker": "alpaca", "x-fleet-clerk-id": "lane-a",
        "x-fleet-routing-epoch": "7", "x-fleet-binding-generation": "3",
        "x-fleet-coordinator-token": "synthetic-token", **changed,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.post("/effect", headers=headers)
    assert response.status_code == expected
    assert len(effects) == (1 if expected == 200 else 0)
