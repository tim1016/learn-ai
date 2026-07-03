"""Security guard tests for mutating data-plane control routes."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.config import settings
from app.main import app
from app.security.data_plane_control import (
    CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR,
    CONTROL_SECRET_ENV_VAR,
    CONTROL_SECRET_HEADER,
    UNSAFE_HTTP_METHODS,
    require_data_plane_control_secret,
)

_CONTROL_SURFACE_PREFIXES = (
    "/api/live-instances",
    "/api/live-runs",
    "/api/broker",
    "/api/accounts",
)
_MUTATION_PATH = "/api/broker/orders/what-if"
_READ_PATH = "/api/broker/health"


def _control_routes() -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and any(
            route.path == prefix or route.path.startswith(f"{prefix}/")
            for prefix in _CONTROL_SURFACE_PREFIXES
        )
    ]


def _unsafe_methods(route: APIRoute) -> set[str]:
    return {method for method in route.methods or set() if method in UNSAFE_HTTP_METHODS}


def _has_control_guard(route: APIRoute) -> bool:
    return any(dependency.call is require_data_plane_control_secret for dependency in route.dependant.dependencies)


def _request(method: str) -> Request:
    return Request({"type": "http", "method": method, "path": _MUTATION_PATH, "headers": []})


def test_unsafe_control_routes_declare_data_plane_guard_dependency() -> None:
    unsafe_routes = [
        (route.path, sorted(_unsafe_methods(route)), _has_control_guard(route))
        for route in _control_routes()
        if _unsafe_methods(route)
    ]

    assert unsafe_routes
    assert ("/api/broker/connect", ["POST"], True) in unsafe_routes
    assert ("/api/live-instances/runs/{run_id}/start", ["POST"], True) in unsafe_routes
    assert ("/api/accounts/{account_id}/reconciliation", ["POST"], True) in unsafe_routes
    assert all(has_guard for _path, _methods, has_guard in unsafe_routes)


@pytest.mark.asyncio
async def test_control_mutation_rejects_missing_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH)

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_rejects_wrong_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _MUTATION_PATH,
            headers={CONTROL_SECRET_HEADER: "wrong"},
        )

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_accepts_valid_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _MUTATION_PATH,
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        )

    assert response.status_code != 403
    if response.status_code == 503:
        assert CONTROL_SECRET_ENV_VAR not in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_get_does_not_require_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_READ_PATH)

    assert response.status_code != 403
    if response.status_code == 503:
        assert CONTROL_SECRET_ENV_VAR not in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_fails_closed_when_secret_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH)

    assert response.status_code == 503
    assert CONTROL_SECRET_ENV_VAR in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_local_dev_opt_out_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH)

    assert response.status_code != 403
    if response.status_code == 503:
        assert CONTROL_SECRET_ENV_VAR not in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_compares_header_as_bytes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "tëst-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    with pytest.raises(HTTPException) as exc_info:
        await require_data_plane_control_secret(_request("POST"), supplied="test-control-secret")
    assert exc_info.value.status_code == 403

    await require_data_plane_control_secret(_request("POST"), supplied="tëst-control-secret")


def test_local_dev_opt_out_has_named_environment_switch() -> None:
    assert CONTROL_ALLOW_UNAUTHENTICATED_ENV_VAR == "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL"


@pytest.mark.asyncio
async def test_disallowed_host_header_is_rejected() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"host": "evil.example"})

    assert response.status_code == 400
