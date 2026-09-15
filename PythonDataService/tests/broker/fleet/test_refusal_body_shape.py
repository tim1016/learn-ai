"""Wire-shape parity for the fleet control plane's refusal bodies (#2067).

Nine bare-detail 403/503 sites across ``data_plane_control.py``,
``broker_clerks.py`` and ``internal_fleet.py`` answered with FastAPI's bare
``{"detail": "..."}`` -- or, on ``internal_fleet.py``'s own ``_refuse``, a
*nested* ``{"detail": {reason, message, ...}}`` -- instead of the fleet
contract's flat ``{reason, message, next_step?}`` shape every other fleet
refusal already carries. Task 7c wraps all nine into three new
``FleetControlError`` families (``DataPlaneControlSecretRefused``,
``FleetControlPlaneNotInstalled``, ``FleetAgentTokenRefused``) and gives
``internal_fleet.py``'s ``_refuse`` the same flat writer
``broker_clerks.py``'s already had.

This file proves the *refuse* paths land in the flat shape and, just as
important, that the *accept* path for the exact same guards still works: a
guard rewritten to always refuse would pass every test that only checks
refusals (this is the file that guards the guard every browser call to the
data plane passes through).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.fleet.errors import (
    DataPlaneControlSecretRefused,
    FleetAgentTokenRefused,
    FleetControlPlaneNotInstalled,
)
from app.broker.fleet.refusal_vocabulary import FLEET_REFUSAL_REASONS
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from app.utils.error_handlers import install_fleet_control_error_handler


def _assert_flat_refusal(body: object, *, expected_reason: str) -> None:
    """The one wire shape every fleet refusal must carry (#2067)."""
    assert isinstance(body, dict)
    assert set(body) >= {"reason", "message"}
    assert body["reason"] == expected_reason
    assert body["reason"] in FLEET_REFUSAL_REASONS
    assert "detail" not in body
    assert isinstance(body["message"], str) and body["message"]


def _build_broker_clerks_app(
    control_dir: Path, *, install_registry: bool, install_lane_router: bool
) -> tuple[FastAPI, FleetControlService]:
    """A coordinator app mounting the public clerk-scoped router.

    ``install_registry`` / ``install_lane_router`` mirror the two ways
    ``broker_clerks.py`` can be missing a required process-state component
    (``_fleet_service`` / ``_lane_router``) -- both wrap to
    ``FleetControlPlaneNotInstalled`` as of #2067.
    """
    from app.broker.fleet.routing import LaneRouter, coordinator_delivery_for
    from app.routers import broker_clerks

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    app = FastAPI()
    install_fleet_control_error_handler(app)
    if install_registry:
        app.state.fleet_service = service
    if install_lane_router:
        app.state.fleet_lane_router = LaneRouter(
            service=service,
            delivery_for=coordinator_delivery_for(service, {}),
        )
    app.include_router(broker_clerks.router)
    return app, service


def _build_internal_fleet_app(
    control_dir: Path, tokens: dict[str, str], *, install_registry: bool = True
) -> tuple[FastAPI, FleetControlService]:
    """A coordinator app mounting the internal agent-facing router."""
    from app.routers import internal_fleet

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    app = FastAPI()
    install_fleet_control_error_handler(app)
    if install_registry:
        app.state.fleet_service = service
    app.state.fleet_agent_tokens_text = json.dumps(tokens)
    app.include_router(internal_fleet.router)
    return app, service


# ---- app/security/data_plane_control.py ------------------------------------


@pytest.mark.asyncio
async def test_a_missing_control_secret_refuses_in_the_contract_shape(monkeypatch) -> None:
    """The exact scenario named in the issue: no configured secret, refused."""
    from app.config import settings
    from app.security.data_plane_control import require_data_plane_control_secret

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    app = FastAPI()
    install_fleet_control_error_handler(app)

    @app.post("/guarded", dependencies=[Depends(require_data_plane_control_secret)])
    async def guarded() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/guarded")

    assert response.status_code == 503
    body = response.json()
    assert set(body) >= {"reason", "message"}
    assert body["reason"] in FLEET_REFUSAL_REASONS
    assert "detail" not in body
    _assert_flat_refusal(body, expected_reason=FleetControlPlaneNotInstalled.reason)


@pytest.mark.asyncio
async def test_a_wrong_control_secret_refuses_403_in_the_contract_shape(monkeypatch) -> None:
    from app.config import settings
    from app.security.data_plane_control import (
        CONTROL_SECRET_HEADER,
        require_data_plane_control_secret,
    )

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "the-real-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    app = FastAPI()
    install_fleet_control_error_handler(app)

    @app.post("/guarded", dependencies=[Depends(require_data_plane_control_secret)])
    async def guarded() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/guarded", headers={CONTROL_SECRET_HEADER: "wrong"})

    assert response.status_code == 403
    _assert_flat_refusal(response.json(), expected_reason=DataPlaneControlSecretRefused.reason)


@pytest.mark.asyncio
async def test_a_correct_control_secret_still_accepts(monkeypatch) -> None:
    """The accept path for the exact same guard the two tests above refuse."""
    from app.config import settings
    from app.security.data_plane_control import (
        CONTROL_SECRET_HEADER,
        require_data_plane_control_secret,
    )

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "the-real-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)

    app = FastAPI()
    install_fleet_control_error_handler(app)

    @app.post("/guarded", dependencies=[Depends(require_data_plane_control_secret)])
    async def guarded() -> dict[str, bool]:
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/guarded", headers={CONTROL_SECRET_HEADER: "the-real-secret"}
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# ---- app/routers/broker_clerks.py ------------------------------------------


@pytest.mark.asyncio
async def test_broker_clerks_refuses_with_no_registry_installed(control_dir: Path) -> None:
    app, service = _build_broker_clerks_app(
        control_dir, install_registry=False, install_lane_router=True
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/broker-clerks")
        assert response.status_code == 503
        _assert_flat_refusal(response.json(), expected_reason=FleetControlPlaneNotInstalled.reason)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_broker_clerks_refuses_with_no_lane_router_installed(control_dir: Path) -> None:
    """A clerk-scoped operation route needs the lane router, not the directory read."""
    from app.broker.fleet.provider import Capability
    from app.routers import broker_clerks

    adapters = production_provider_adapters()
    alpaca_ops = adapters["alpaca"].operations()
    # Register catalog routes on the module-level router *before* it is
    # mounted -- app.include_router() snapshots the router's routes at
    # call time, so registering afterward would silently 404.
    broker_clerks.register_catalog_operations({"alpaca": alpaca_ops})
    app, service = _build_broker_clerks_app(
        control_dir, install_registry=True, install_lane_router=False
    )
    try:
        read_op = next(op for op in alpaca_ops if op.capability == Capability.ACCOUNT_READ)
        path = f"/api/brokers/alpaca/clerks/clrk_probe00000000000000000000aa{read_op.path_template}"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(path)
        assert response.status_code == 503
        _assert_flat_refusal(response.json(), expected_reason=FleetControlPlaneNotInstalled.reason)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_broker_clerks_directory_accepts_with_the_registry_installed(
    control_dir: Path,
) -> None:
    """The accept path for the exact guard the test above refuses: an
    installed (even if empty) registry answers 200, not a refusal."""
    app, service = _build_broker_clerks_app(
        control_dir, install_registry=True, install_lane_router=True
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/broker-clerks")
        assert response.status_code == 200
        body = response.json()
        assert body["clerks"] == []
    finally:
        service.close()


# ---- app/routers/internal_fleet.py -----------------------------------------

_CLERK_ID = "clrk_probe00000000000000000000aa"
_TOKEN = "svct_" + "1" * 32


@pytest.mark.asyncio
async def test_internal_fleet_refuses_with_no_coordinator_installed(control_dir: Path) -> None:
    app, service = _build_internal_fleet_app(
        control_dir, {_CLERK_ID: _TOKEN}, install_registry=False
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/internal/fleet/clerks/{_CLERK_ID}/volume-expectation",
                headers={"X-Fleet-Agent-Token": _TOKEN},
            )
        assert response.status_code == 503
        _assert_flat_refusal(response.json(), expected_reason=FleetControlPlaneNotInstalled.reason)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_internal_fleet_refuses_on_a_malformed_token_mapping(control_dir: Path) -> None:
    app, service = _build_internal_fleet_app(control_dir, {_CLERK_ID: _TOKEN})
    app.state.fleet_agent_tokens_text = "{not valid json"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/internal/fleet/clerks/{_CLERK_ID}/volume-expectation",
                headers={"X-Fleet-Agent-Token": _TOKEN},
            )
        assert response.status_code == 503
        _assert_flat_refusal(response.json(), expected_reason=FleetControlPlaneNotInstalled.reason)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_internal_fleet_refuses_when_the_token_mapping_is_not_a_dict(
    control_dir: Path,
) -> None:
    app, service = _build_internal_fleet_app(control_dir, {_CLERK_ID: _TOKEN})
    app.state.fleet_agent_tokens_text = json.dumps(["not", "a", "mapping"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/internal/fleet/clerks/{_CLERK_ID}/volume-expectation",
                headers={"X-Fleet-Agent-Token": _TOKEN},
            )
        assert response.status_code == 503
        _assert_flat_refusal(response.json(), expected_reason=FleetControlPlaneNotInstalled.reason)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_internal_fleet_refuses_a_wrong_agent_token_flat_not_nested(
    control_dir: Path,
) -> None:
    """Before 7c this refused with a bare ``{"detail": "agent token refused"}``.

    ``_authorized_agent`` raises before any route-local ``try``, so this
    exact site never touches ``internal_fleet.py``'s own ``_refuse`` -- it is
    caught only by ``install_fleet_control_error_handler``'s global handler
    (mutation-proven separately: see
    ``test_internal_fleet_refuses_with_no_coordinator_installed`` for the
    site that *does* exercise ``_refuse`` and the nested-vs-flat divergence
    it used to write)."""
    app, service = _build_internal_fleet_app(control_dir, {_CLERK_ID: _TOKEN})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/internal/fleet/clerks/{_CLERK_ID}/volume-expectation",
                headers={"X-Fleet-Agent-Token": "svct_" + "9" * 32},
            )
        assert response.status_code == 403
        body = response.json()
        _assert_flat_refusal(body, expected_reason=FleetAgentTokenRefused.reason)
        # The specific regression: no "detail" key wrapping the whole body,
        # and "reason"/"message" sit at the top level.
        assert "reason" in body and "message" in body
    finally:
        service.close()


@pytest.mark.asyncio
async def test_internal_fleet_accepts_a_correctly_mapped_agent_token(
    control_dir: Path, tmp_path: Path
) -> None:
    """The accept path for the exact guard the two tests above refuse.

    Needs a real provisioned clerk -- ``clerk_volume_expectation`` 404s
    (``ClerkNotFound``) for an identity the registry has never seen, which
    would otherwise mask whether the *token* guard actually accepted.
    """
    from tests.broker.fleet.conftest import provision_lane

    app, service = _build_internal_fleet_app(control_dir, {})
    try:
        lane = provision_lane(service, broker="alpaca", label="probe", tmp_path=tmp_path)
        app.state.fleet_agent_tokens_text = json.dumps({lane.clerk_id: _TOKEN})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/internal/fleet/clerks/{lane.clerk_id}/volume-expectation",
                headers={"X-Fleet-Agent-Token": _TOKEN},
            )
        assert response.status_code == 200
        assert response.json()["clerk_id"] == lane.clerk_id
    finally:
        service.close()
