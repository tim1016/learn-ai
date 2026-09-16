"""Coordinator-only retained broker read aliases."""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.routers import fleet_compatibility_reads as compatibility_reads


async def test_legacy_live_verdict_always_refuses_retired() -> None:
    """#2140: the coordinator has no clerk runtime, so it must not report one
    process's empty runtime as the installation's verdict -- it refuses
    every broker, unconditionally, naming the lane-scoped replacement."""
    response = await compatibility_reads.get_legacy_live_verdict("alpaca")

    assert isinstance(response, JSONResponse)
    assert response.status_code == 410
    body = json.loads(response.body)
    assert body["reason"] == "compatibility_read_retired"
    assert "clerks/{clerk_id}/live-verdict" in body["next_step"]

    # Unconditional: an unsupported broker name gets the same refusal, not a
    # different unsupported-broker error -- there is no topology where this
    # route could ever answer any broker correctly.
    other = await compatibility_reads.get_legacy_live_verdict("unknown")
    assert isinstance(other, JSONResponse)
    assert other.status_code == 410


async def test_legacy_panel_profile_is_closed_to_the_existing_broker_set() -> None:
    """The bridge does not turn unknown broker names into a generic surface."""
    with pytest.raises(HTTPException) as error:
        await compatibility_reads.get_legacy_panel_profile("unknown")

    assert error.value.status_code == 404


def test_compatibility_router_exposes_only_the_two_retained_safe_reads() -> None:
    """No compatibility alias can grow into an implicit mutation target."""
    routes = {
        (route.path, method)
        for route in compatibility_reads.router.routes
        for method in route.methods
    }
    assert routes == {
        ("/api/brokers/{broker}/live-verdict", "GET"),
        ("/api/brokers/{broker}/panel-profile", "GET"),
    }
