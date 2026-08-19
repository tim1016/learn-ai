"""Route inventory for the retained host-capability bridge."""

from app.routers import live_instances


def test_only_host_capability_routes_remain() -> None:
    routes = {(route.path, method) for route in live_instances.router.routes for method in route.methods}

    assert routes == {
        ("/daemon-health", "GET"),
        ("/daemon-health/renew-lease", "POST"),
    }
