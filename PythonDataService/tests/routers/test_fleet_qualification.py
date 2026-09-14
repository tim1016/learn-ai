"""Guards for the Compose-only serving-lane qualification hook."""

from __future__ import annotations

from app.routers.fleet_qualification import qualification_router


def test_qualification_router_is_absent_without_each_strict_guard() -> None:
    """Normal, coordinator, and non-random namespaces cannot expose the hook."""
    assert qualification_router(role="combined", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="host:local", secret="s", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="", broker_url="http://fake", market_data_url="http://market") is None
    assert qualification_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="s", broker_url="http://fake", market_data_url="") is None


def test_qualification_router_has_no_mutation_or_provider_registration_route() -> None:
    """The opt-in surface is read-only observation and held ASGI work only."""
    router = qualification_router(
        role="clerk_agent",
        namespace="compose:fleetqualificationx",
        secret="qualification-secret",
        broker_url="http://fake",
        market_data_url="http://market",
    )
    assert router is not None
    paths = {route.path for route in router.routes}
    assert paths == {
        "/internal/fleet-qualification/dependency/broker",
        "/internal/fleet-qualification/dependency/market-data",
        "/internal/fleet-qualification/hold/request",
        "/internal/fleet-qualification/hold/stream",
    }
    assert all("POST" not in route.methods for route in router.routes)
