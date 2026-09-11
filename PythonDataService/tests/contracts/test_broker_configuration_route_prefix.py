"""No ``/api/brokers/{broker}/…`` route shadows the configuration prefix.

Contract §4 and §8. Five routers already share the ``/api/brokers`` prefix, and
one of them serves ``/{broker}/…`` wildcards, so which handler answers
``/api/brokers/alpaca/configuration/owner`` would otherwise depend on the order
``app/main.py`` happens to register them in. This resolves every configuration
path through the real app router and asserts the endpoint that answers belongs
to the configuration router — a property, not a registration order.

The reverse direction matters too: the configuration router must not swallow a
``{broker}`` route that existed before it.
"""

from __future__ import annotations

import pytest
from starlette.routing import Match, Route

from app.main import app
from app.routers.broker_configuration import PREFIX
from app.routers.broker_configuration import router as configuration_router

_CONFIGURATION_ENDPOINTS = {
    route.endpoint for route in configuration_router.routes if isinstance(route, Route)
}

_CONFIGURATION_PATHS = [
    (route.path, sorted(route.methods or set())[0])
    for route in configuration_router.routes
    if isinstance(route, Route)
]


def _resolve(path: str, method: str) -> Route:
    """The route the app would actually dispatch, in registration order."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for route in app.routes:
        match, _child_scope = route.matches(scope)
        if match is Match.FULL:
            assert isinstance(route, Route)
            return route
    raise AssertionError(f"no route matched {method} {path}")


@pytest.mark.parametrize(("path", "method"), _CONFIGURATION_PATHS)
def test_the_configuration_router_answers_its_own_paths(path: str, method: str) -> None:
    concrete = (
        path.replace("{profile_id}", "profile_example")
        .replace("{revision}", "1")
        .replace("{account_id}", "PA000EXAMPLE")
    )

    resolved = _resolve(concrete, method)

    assert resolved.endpoint in _CONFIGURATION_ENDPOINTS, (
        f"{method} {concrete} is answered by {resolved.path!r}, not the configuration router"
    )


def test_no_broker_wildcard_route_claims_the_configuration_segment() -> None:
    """A ``{broker}`` route whose next segment is a wildcard would shadow us."""
    shadowing = [
        route.path
        for route in app.routes
        if isinstance(route, Route)
        and route.path.startswith("/api/brokers/{broker}/")
        and route.path.split("/")[4].startswith("{")
    ]

    assert shadowing == []


def test_the_configuration_router_does_not_swallow_an_existing_broker_route() -> None:
    """Every pre-existing ``/api/brokers/…`` route still answers itself."""
    broker_routes = [
        route
        for route in app.routes
        if isinstance(route, Route)
        and route.path.startswith("/api/brokers/")
        and route.endpoint not in _CONFIGURATION_ENDPOINTS
    ]
    assert broker_routes, "expected the Broker v2 read surface to be registered"

    for route in broker_routes:
        concrete = "/".join(
            "alpaca" if segment == "{broker}" else ("1" if segment.startswith("{") else segment)
            for segment in route.path.split("/")
        )
        method = sorted(route.methods or {"GET"})[0]
        resolved = _resolve(concrete, method)

        assert resolved.endpoint not in _CONFIGURATION_ENDPOINTS, (
            f"{method} {concrete} is now answered by the configuration router"
        )


def test_the_prefix_is_the_one_the_contract_names() -> None:
    assert PREFIX == "/api/brokers/alpaca/configuration"
