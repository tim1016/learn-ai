"""Security guard tests for mutating data-plane control routes."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.broker.fleet.errors import (
    DataPlaneControlSecretRefused,
    FleetControlPlaneNotInstalled,
)
from app.config import settings
from app.main import app
from app.security.data_plane_control import (
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
# The interceptor's safe-method set (``data-plane-control-intent.interceptor.ts``
# and ``proxy.conf.js`` both use exactly these two). Kept here rather than
# imported because ``app.security.data_plane_control`` only names the unsafe
# set it guards on.
SAFE_READ_HTTP_METHODS = frozenset({"GET", "HEAD"})

_MUTATION_PATH = "/api/broker/disconnect"
_READ_PATH = "/api/broker/health"
_BROKERS_READ_PATH = "/api/brokers/alpaca/account"
_ACCOUNT_TRANSACTIONS_READ_PATH = "/api/accounts/DU1234567/transactions"
_ALPACA_CLERK_SQLITE_READ_PATH = "/api/alpaca-clerk-sqlite/accounts/PA-TEST/snapshot"

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


def _safe_read_methods(route: APIRoute) -> set[str]:
    return {method for method in route.methods or set() if method in SAFE_READ_HTTP_METHODS}


def _path_is_manifest_protected_read(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _PROTECTED_READ_PREFIXES)


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
    assert ("/api/research/options-backtest/past-chain/download", ["POST"], True) in unsafe_routes
    assert (
        "/api/accounts/{account_id}/transactions/external-orders/{external_order_id}/acknowledge",
        ["POST"],
        True,
    ) in unsafe_routes
    assert all(has_guard for _path, _methods, has_guard in unsafe_routes)


def test_only_the_past_chain_download_is_a_guarded_control_route_of_the_options_backtest() -> None:
    """The download spends vendor requests and writes the lake, so it carries the secret; its two reads and the
    setup routes beside them (which write nothing the vendor or the lake holds) are not control routes, so the
    manifest names the ``past-chain`` prefix and not the whole ``options-backtest`` one."""
    options_backtest = "/api/research/options-backtest"
    chain = {
        route.path: (sorted(_unsafe_methods(route)), _has_control_guard(route))
        for route in _api_routes()
        if route.path.startswith(f"{options_backtest}/past-chain")
    }
    setups = [
        route
        for route in _api_routes()
        if route.path.startswith(options_backtest) and not route.path.startswith(f"{options_backtest}/past-chain")
    ]

    assert chain == {
        f"{options_backtest}/past-chain/day": ([], False),
        f"{options_backtest}/past-chain/expiry": ([], False),
        f"{options_backtest}/past-chain/download": (["POST"], True),
    }
    assert setups
    assert not any(_is_manifest_control_route(route) for route in setups)


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


def test_always_guarded_reads_are_declared_in_shared_manifest() -> None:
    """Companion to the unsafe-method assertion above, for the other half of
    the manifest.

    The Frontend interceptor and ``proxy.conf.js`` decide whether to attach
    the control secret by matching the request path against
    ``control_prefixes`` for unsafe methods and ``protected_read_prefixes``
    for safe ones. A route that carries the always-on guard but sits under
    no ``protected_read_prefixes`` entry therefore 403s from the browser as
    soon as ``DATA_PLANE_CONTROL_SECRET`` is configured, while every
    server-side test still passes — and
    ``test_guarded_control_routes_are_declared_in_shared_manifest``, which
    only inspects unsafe-method routes, cannot see it.
    """
    guarded_reads = [
        (route.path, sorted(_safe_read_methods(route)))
        for route in _api_routes()
        if _safe_read_methods(route) and _has_always_control_guard(route)
    ]
    undeclared = [
        (path, methods)
        for path, methods in guarded_reads
        if not _path_is_manifest_protected_read(path)
    ]

    assert guarded_reads
    assert undeclared == []


def test_fleet_directory_read_is_declared_in_shared_manifest() -> None:
    """The route-scan assertion above is blind to the fleet directory.

    ``/api/broker-clerks`` carries the always-on guard but mounts only when
    the coordinator surface installs (``FLEET_CONTROL_DIR`` set), so the
    default test posture never registers it and the scan cannot catch a
    missing manifest entry. Pin the entry directly: the browser's lane
    directory loads this exact path through the dev proxy, which attaches
    the control secret only for ``protected_read_prefixes`` matches — an
    undeclared always-guarded read 403s from the browser the moment
    ``DATA_PLANE_CONTROL_SECRET`` is configured.
    """
    from app.routers import broker_clerks

    directory_routes = [
        (route.path, sorted(route.methods or set()), _has_always_control_guard(route))
        for route in broker_clerks.router.routes
        if isinstance(route, APIRoute)
    ]

    assert ("/api/broker-clerks", ["GET"], True) in directory_routes
    assert "/api/broker-clerks" in _PROTECTED_READ_PREFIXES


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "wrong"], ids=["missing_secret_header", "wrong_secret_header"])
async def test_control_mutation_rejects_missing_or_wrong_secret_header(monkeypatch, supplied) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH, headers=headers)

    assert response.status_code == 403, "mutation without the right secret is refused with 403"
    body = response.json()
    assert body["reason"] == DataPlaneControlSecretRefused.reason, "refusal carries the secret-refused reason"
    assert CONTROL_SECRET_HEADER in body["message"], "refusal names the control secret header"
    assert "detail" not in body, "refusal has no FastAPI detail envelope"


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
        assert CONTROL_SECRET_ENV_VAR not in response.json()["message"]


@pytest.mark.asyncio
async def test_control_get_does_not_require_secret_header(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_READ_PATH)

    assert response.status_code != 403
    if response.status_code == 503:
        assert CONTROL_SECRET_ENV_VAR not in response.json()["message"]


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
@pytest.mark.parametrize(
    "read_path",
    [
        pytest.param(_ACCOUNT_TRANSACTIONS_READ_PATH, id="account_transaction_read"),
        pytest.param(_BROKERS_READ_PATH, id="broker_v2_read"),
        pytest.param(_ALPACA_CLERK_SQLITE_READ_PATH, id="alpaca_clerk_sqlite_read"),
    ],
)
@pytest.mark.parametrize("supplied", [None, "wrong"], ids=["missing_secret", "wrong_secret"])
async def test_always_guarded_read_rejects_missing_or_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
    read_path: str,
    supplied: str | None,
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(read_path, headers=headers)

    assert response.status_code == 403, "read without the right secret is refused with 403"
    body = response.json()
    assert body["reason"] == DataPlaneControlSecretRefused.reason, "refusal carries the secret-refused reason"
    assert CONTROL_SECRET_HEADER in body["message"], "refusal names the control secret header"
    assert "detail" not in body, "refusal has no FastAPI detail envelope"


def test_broker_v2_routes_declare_always_on_guard() -> None:
    broker_routes = [
        (route.path, sorted(route.methods or set()), _has_always_control_guard(route))
        for route in _api_routes()
        if route.path == "/api/brokers" or route.path.startswith("/api/brokers/")
    ]

    assert broker_routes
    assert all(has_guard for _path, _methods, has_guard in broker_routes)


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
@pytest.mark.parametrize(
    ("secret", "supplied"),
    [
        pytest.param("", None, id="secret_is_not_configured"),
        pytest.param(
            RETIRED_DATA_PLANE_CONTROL_SECRET,
            RETIRED_DATA_PLANE_CONTROL_SECRET,
            id="retired_public_secret",
        ),
    ],
)
async def test_control_mutation_fails_closed(monkeypatch, secret, supplied) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", secret)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    headers = {} if supplied is None else {CONTROL_SECRET_HEADER: supplied}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH, headers=headers)

    assert response.status_code == 503, "mutation fails closed with 503"
    body = response.json()
    assert body["reason"] == FleetControlPlaneNotInstalled.reason, "failure carries the control-plane-not-installed reason"
    assert CONTROL_SECRET_ENV_VAR in body["message"], "failure names the control secret environment variable"
    assert "detail" not in body, "failure has no FastAPI detail envelope"


@pytest.mark.asyncio
async def test_control_mutation_local_dev_opt_out_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_MUTATION_PATH)

    assert response.status_code != 403
    if response.status_code == 503:
        assert CONTROL_SECRET_ENV_VAR not in response.json()["message"]


@pytest.mark.asyncio
async def test_control_mutation_compares_header_as_bytes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "tëst-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    with pytest.raises(DataPlaneControlSecretRefused) as exc_info:
        await require_data_plane_control_secret(_request("POST"), supplied="test-control-secret")
    assert exc_info.value.status_code == 403

    await require_data_plane_control_secret(_request("POST"), supplied="tëst-control-secret")


def test_internal_fleet_prefix_is_absent_from_the_browser_control_surface_manifest() -> None:
    """Issue #2204: the coordinator-only history-batch route
    (``/internal/fleet/history/batch``) is guarded by the fleet agent-token
    scheme (``X-Fleet-Clerk-Id`` + ``X-Fleet-Agent-Token``), never the browser
    control secret. This manifest's schema is for browser ``/api/*``
    prefixes; an ``/internal`` entry here would wrongly attach
    ``X-Data-Plane-Control-Secret`` policy to a surface that must never
    accept it."""
    assert not any(
        prefix == "/internal" or prefix.startswith("/internal/")
        for prefix in _CONTROL_SURFACE_PREFIXES
    )
    assert not any(
        prefix == "/internal" or prefix.startswith("/internal/")
        for prefix in _PROTECTED_READ_PREFIXES
    )


@pytest.mark.asyncio
async def test_disallowed_host_header_is_rejected() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"host": "evil.example"})

    assert response.status_code == 400
