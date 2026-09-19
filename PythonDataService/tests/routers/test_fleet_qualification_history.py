"""Guards for the coordinator-side recorded-history mode control (issue #2206)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.fleet_qualification_history import (
    qualification_history_router,
    qualification_history_router_from_environment,
)
from app.services.broker_v2_panel.qualification_recorded_history import (
    reset_recorded_history_mode_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_mode() -> None:
    reset_recorded_history_mode_for_testing()
    yield
    reset_recorded_history_mode_for_testing()


def test_router_is_absent_without_every_guard() -> None:
    """Mirrors the clerk-side gate's shape: role, namespace, and secret must
    all hold, and only for the coordinator role -- never the clerk's."""
    assert qualification_history_router(role="combined", namespace="compose:fleetqualificationx", secret="s") is None
    assert qualification_history_router(role="clerk_agent", namespace="compose:fleetqualificationx", secret="s") is None
    assert qualification_history_router(role="fleet_coordinator", namespace="host:local", secret="s") is None
    assert qualification_history_router(role="fleet_coordinator", namespace="compose:fleetqualificationx", secret="") is None


def test_router_from_environment_reads_the_probe_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLEET_QUALIFICATION_PROBE_SECRET", raising=False)
    assert qualification_history_router_from_environment("fleet_coordinator", "compose:fleetqualificationx") is None

    monkeypatch.setenv("FLEET_QUALIFICATION_PROBE_SECRET", "minted-secret")
    router = qualification_history_router_from_environment("fleet_coordinator", "compose:fleetqualificationx")
    assert router is not None
    assert {route.path for route in router.routes} == {"/internal/fleet-qualification-history/mode"}


def _build_app(secret: str) -> FastAPI:
    router = qualification_history_router(
        role="fleet_coordinator", namespace="compose:fleetqualificationx", secret=secret
    )
    assert router is not None
    app = FastAPI()
    app.include_router(router)
    return app


async def test_mode_requires_the_secret_header() -> None:
    app = _build_app("s3cr3t")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        get_response = await client.get("/internal/fleet-qualification-history/mode")
        post_response = await client.post(
            "/internal/fleet-qualification-history/mode", json={"mode": "unavailable"}
        )

    assert get_response.status_code == 404
    assert post_response.status_code == 404


async def test_mode_defaults_to_healthy_and_can_be_set_and_read_back() -> None:
    app = _build_app("s3cr3t")
    headers = {"X-Fleet-Qualification-Secret": "s3cr3t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get("/internal/fleet-qualification-history/mode", headers=headers)
        set_unavailable = await client.post(
            "/internal/fleet-qualification-history/mode", json={"mode": "unavailable"}, headers=headers
        )
        read_back = await client.get("/internal/fleet-qualification-history/mode", headers=headers)

    assert initial.status_code == 200
    assert initial.json() == {"mode": "healthy"}
    assert set_unavailable.status_code == 200
    assert set_unavailable.json() == {"mode": "unavailable"}
    assert read_back.json() == {"mode": "unavailable"}


async def test_mode_rejects_a_value_outside_the_closed_vocabulary() -> None:
    app = _build_app("s3cr3t")
    headers = {"X-Fleet-Qualification-Secret": "s3cr3t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/internal/fleet-qualification-history/mode", json={"mode": "bogus"}, headers=headers
        )

    assert response.status_code == 422
