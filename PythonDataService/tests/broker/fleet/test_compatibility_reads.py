"""Coordinator-only retained broker read aliases."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers import fleet_compatibility_reads as compatibility_reads


async def test_legacy_live_verdict_delegates_to_the_canonical_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility route keeps its existing semantics, not a copy."""
    expected = {"mode": "safe"}

    async def canonical_read(broker: str) -> dict[str, str]:
        assert broker == "alpaca"
        return expected

    monkeypatch.setattr(compatibility_reads.brokers, "get_live_verdict", canonical_read)

    assert await compatibility_reads.get_legacy_live_verdict("alpaca") == expected


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
