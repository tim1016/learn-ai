"""Security guard tests for mutating data-plane control routes."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
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
    RETIRED_DATA_PLANE_CONTROL_SECRET,
    UNSAFE_HTTP_METHODS,
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)

_CONTROL_SURFACE_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "data-plane-control-surfaces.json"
)
_CONTROL_SURFACE_SCHEMA = _CONTROL_SURFACE_MANIFEST.with_suffix(".schema.json")
_CONTROL_SURFACE_MANIFEST_PAYLOAD = json.loads(_CONTROL_SURFACE_MANIFEST.read_text())
_CONTROL_SURFACE_PREFIXES = tuple(_CONTROL_SURFACE_MANIFEST_PAYLOAD["control_prefixes"])
_PROTECTED_READ_PREFIXES = tuple(_CONTROL_SURFACE_MANIFEST_PAYLOAD["protected_read_prefixes"])
_MUTATION_PATH = "/api/broker/disconnect"
_READ_PATH = "/api/broker/health"
_BROKERS_READ_PATH = "/api/brokers/alpaca/account"
_ACCOUNT_TRANSACTIONS_READ_PATH = "/api/accounts/DU1234567/transactions"
_ALPACA_CLERK_SQLITE_READ_PATH = "/api/alpaca-clerk-sqlite/accounts/PA-TEST/snapshot"

# Protected-read surfaces PR-B of #1813 (2026-08-27) retired outright: the
# IBKR order-event stream, the broker session mirror, and the live-instance
# / live-run projections. Their always-on-guard and reject-without-secret
# tests retired with them — a route that is not registered returns 404 and
# cannot leak the evidence the guard existed to protect. What replaces them
# is the absence assertion below: neither the app nor the shared manifest
# may name these paths again.
_RETIRED_PROTECTED_READ_PATHS = (
    "/api/broker/orders/stream",
    "/api/broker/session-mirror",
    "/api/live-instances",
    "/api/live-runs",
)


def _path_is_manifest_control_surface(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _CONTROL_SURFACE_PREFIXES)


def _is_manifest_control_route(route: APIRoute) -> bool:
    return _path_is_manifest_control_surface(route.path)


def _api_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _control_routes() -> list[APIRoute]:
    return [route for route in _api_routes() if _is_manifest_control_route(route)]


def _unsafe_methods(route: APIRoute) -> set[str]:
    return {method for method in route.methods or set() if method in UNSAFE_HTTP_METHODS}


def _has_control_guard(route: APIRoute) -> bool:
    return any(
        dependency.call
        in {require_data_plane_control_secret, require_data_plane_control_secret_always}
        for dependency in route.dependant.dependencies
    )


def _has_always_control_guard(route: APIRoute) -> bool:
    return any(
        dependency.call is require_data_plane_control_secret_always
        for dependency in route.dependant.dependencies
    )


def _request(method: str) -> Request:
    return Request({"type": "http", "method": method, "path": _MUTATION_PATH, "headers": []})


def test_data_plane_control_surface_manifest_matches_schema() -> None:
    schema = json.loads(_CONTROL_SURFACE_SCHEMA.read_text())

    jsonschema.validate(_CONTROL_SURFACE_MANIFEST_PAYLOAD, schema=schema)
    assert tuple(sorted(_CONTROL_SURFACE_PREFIXES)) == _CONTROL_SURFACE_PREFIXES
    assert all(prefix.startswith("/api/") for prefix in _CONTROL_SURFACE_PREFIXES)
    assert all(not prefix.endswith("/") for prefix in _CONTROL_SURFACE_PREFIXES)
    assert tuple(sorted(_PROTECTED_READ_PREFIXES)) == _PROTECTED_READ_PREFIXES
    assert all(prefix.startswith("/api/") for prefix in _PROTECTED_READ_PREFIXES)
    assert all(not prefix.endswith("/") for prefix in _PROTECTED_READ_PREFIXES)


def test_unsafe_control_routes_declare_data_plane_guard_dependency() -> None:
    unsafe_routes = [
        (route.path, sorted(_unsafe_methods(route)), _has_control_guard(route))
        for route in _control_routes()
        if _unsafe_methods(route)
    ]

    assert unsafe_routes
    assert ("/api/broker/connect", ["POST"], True) in unsafe_routes
    assert (
        "/api/accounts/{account_id}/transactions/external-orders/{external_order_id}/acknowledge",
        ["POST"],
        True,
    ) in unsafe_routes
    assert all(has_guard for _path, _methods, has_guard in unsafe_routes)


def test_guarded_control_routes_are_declared_in_shared_manifest() -> None:
    guarded_unsafe_routes = [
        (route.path, sorted(_unsafe_methods(route)))
        for route in _api_routes()
        if _unsafe_methods(route) and _has_control_guard(route)
    ]
    missing_from_manifest = [
        (path, methods)
        for path, methods in guarded_unsafe_routes
        if not _path_is_manifest_control_surface(path)
    ]

    assert guarded_unsafe_routes
    assert missing_from_manifest == []


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


def test_retired_protected_read_surfaces_are_absent_from_routes_and_manifest() -> None:
    """Successor to the always-on-guard and reject-without-secret tests for
    the order-event stream, the broker session mirror, and the live-instance
    / live-run projections (see ``_RETIRED_PROTECTED_READ_PATHS``). It fails
    if any of those paths is re-registered or re-declared in the shared
    manifest, which is what would have to happen before an unguarded
    successor could exist."""
    registered = {route.path for route in _api_routes()}

    for retired in _RETIRED_PROTECTED_READ_PATHS:
        assert not any(
            path == retired or path.startswith(f"{retired}/") for path in registered
        ), retired
        assert retired not in _CONTROL_SURFACE_PREFIXES, retired
        assert retired not in _PROTECTED_READ_PREFIXES, retired


def test_account_transaction_routes_declare_always_on_guard_and_shared_manifest() -> None:
    account_transaction_routes = [
        (route.path, sorted(route.methods or set()), _has_always_control_guard(route))
        for route in _api_routes()
        if route.path == "/api/accounts/{account_id}/transactions"
        or route.path.startswith("/api/accounts/{account_id}/transactions/")
    ]

    assert "/api/accounts" in _PROTECTED_READ_PREFIXES
    assert account_transaction_routes
    assert all(has_guard for _path, _methods, has_guard in account_transaction_routes)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "wrong"])
async def test_account_transaction_read_rejects_missing_or_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
    supplied: str | None,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_ACCOUNT_TRANSACTIONS_READ_PATH, headers=headers)

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]


def test_broker_v2_routes_declare_always_on_guard() -> None:
    broker_routes = [
        (route.path, sorted(route.methods or set()), _has_always_control_guard(route))
        for route in _api_routes()
        if route.path == "/api/brokers" or route.path.startswith("/api/brokers/")
    ]

    assert broker_routes
    assert all(has_guard for _path, _methods, has_guard in broker_routes)


def test_broker_v2_protected_reads_are_declared_in_shared_manifest() -> None:
    assert "/api/brokers" in _PROTECTED_READ_PREFIXES


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "wrong"])
async def test_broker_v2_read_rejects_missing_or_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
    supplied: str | None,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_BROKERS_READ_PATH, headers=headers)

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_broker_v2_read_accepts_valid_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _BROKERS_READ_PATH,
            headers={CONTROL_SECRET_HEADER: "test-control-secret"},
        )

    assert response.status_code != 403


def test_alpaca_clerk_sqlite_routes_declare_always_on_guard_and_shared_manifest() -> None:
    """#1396 P1: the SQLite Clerk's projection-read GETs (positions, holds,
    operation/order identities, recovery tokens, timeline proof refs) must
    require the secret unconditionally, like clerk_transactions.router's
    comparable reads — not just skip-on-GET mutation guarding."""
    routes = [
        (route.path, sorted(route.methods or set()), _has_always_control_guard(route))
        for route in _api_routes()
        if route.path == "/api/alpaca-clerk-sqlite"
        or route.path.startswith("/api/alpaca-clerk-sqlite/")
    ]

    assert "/api/alpaca-clerk-sqlite" in _PROTECTED_READ_PREFIXES
    assert routes
    assert all(has_guard for _path, _methods, has_guard in routes)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "wrong"])
async def test_alpaca_clerk_sqlite_read_rejects_missing_or_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
    supplied: str | None,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_ALPACA_CLERK_SQLITE_READ_PATH, headers=headers)

    assert response.status_code == 403
    assert CONTROL_SECRET_HEADER in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_fails_closed_when_secret_is_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH)

    assert response.status_code == 503
    assert CONTROL_SECRET_ENV_VAR in response.json()["detail"]


@pytest.mark.asyncio
async def test_control_mutation_fails_closed_for_retired_public_secret(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "DATA_PLANE_CONTROL_SECRET",
        RETIRED_DATA_PLANE_CONTROL_SECRET,
    )
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _MUTATION_PATH,
            headers={CONTROL_SECRET_HEADER: RETIRED_DATA_PLANE_CONTROL_SECRET},
        )

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
