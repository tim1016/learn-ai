"""Shared fixtures for data-lake integration tests.

Both test_ensure_data_route.py (POST /ensure-data) and
test_observatory_endpoints.py (the Task 5 GET endpoints) need the same
minimal FastAPI app to exercise data_lake router behavior — flag-on and
flag-off — without an app.main reload or a settings override. Factored here
so the two files don't carry duplicate copies of the helper.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI

from app.main import DATA_PLANE_CONTROL_DEPENDENCIES
from app.routers.data_lake import router as data_lake_router


@pytest.fixture
def make_data_lake_app() -> Callable[..., FastAPI]:
    """Factory fixture: build a FastAPI app mirroring main.py's conditional
    data_lake router wiring. ``include_data_lake=False`` reproduces the
    flag-off 404 behavior (the router is never registered at all) without
    touching app.main or DATA_LAKE_ENABLED.

    The guard dependency is imported from ``app.main`` rather than restated,
    so this app cannot quietly become more permissive than production. A
    fixture that dropped it would leave every POST test passing against an
    unguarded route while the real one rejected the same call.
    """

    def _make(*, include_data_lake: bool) -> FastAPI:
        app = FastAPI()
        if include_data_lake:
            app.include_router(data_lake_router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
        return app

    return _make
