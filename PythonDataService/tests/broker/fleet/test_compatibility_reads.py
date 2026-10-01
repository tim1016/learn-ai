"""Coordinator-only retained broker read aliases."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers import fleet_compatibility_reads as compatibility_reads


async def test_legacy_panel_profile_is_closed_to_the_existing_broker_set() -> None:
    """The bridge does not turn unknown broker names into a generic surface."""
    with pytest.raises(HTTPException) as error:
        await compatibility_reads.get_legacy_panel_profile("unknown")

    assert error.value.status_code == 404


def test_compatibility_router_exposes_only_the_retained_safe_read() -> None:
    """No compatibility alias can grow into an implicit mutation target."""
    routes = {
        (route.path, method)
        for route in compatibility_reads.router.routes
        for method in route.methods
    }
    assert routes == {
        ("/api/brokers/{broker}/panel-profile", "GET"),
    }
