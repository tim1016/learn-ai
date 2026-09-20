"""``GET /api/broker-clerks/aggregate/attention`` — one poll, sliced per lane (#2228).

The coordinator delivers each alpaca lane's ``attention_read`` through the
lane router and folds the answers with ``aggregate_lane_reads_async``: one
lane's failure is that lane's explicit ``ok: False`` (a bell's "unknown"),
never an omission or a failure for the page. Lanes of providers that do not
serve attention are not read — they get no bell, not a broken one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.delivery import DeliveryRequest
from app.broker.fleet.errors import ClerkUnreachable
from app.broker.fleet.routing import LaneRouter, RoutedDelivery
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from tests.broker.fleet.conftest import bind_lane, fake_alpha, provision_lane

_AGGREGATE_ROUTE = "/api/broker-clerks/aggregate/attention"
_TEST_SECRET = "test-aggregate-attention-secret"


class _ScriptedDelivery:
    """One lane's agent answering a canned attention read (or failing)."""

    def __init__(
        self, *, answer: dict | None = None, fail_with: Exception | None = None
    ) -> None:
        self._answer = answer
        self._fail_with = fail_with
        self.requests: list[DeliveryRequest] = []

    async def deliver(self, request: DeliveryRequest) -> RoutedDelivery:
        self.requests.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return RoutedDelivery(
            status_code=200,
            headers={},
            body=json.dumps(self._answer).encode("utf-8"),
        )


def _service(control_dir: Path, *, with_alpaca: bool = True) -> FleetControlService:
    adapters: dict[str, object] = {"fake_alpha": fake_alpha()}
    if with_alpaca:
        adapters["alpaca"] = AlpacaProviderAdapter()
    return FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=adapters,
    )


def _coordinator_app(
    service: FleetControlService, deliveries: dict[str, _ScriptedDelivery]
) -> FastAPI:
    from app.routers import broker_clerks

    app = FastAPI()
    app.state.fleet_service = service
    install_fleet_control_error_handler(app)
    app.state.fleet_lane_router = LaneRouter(
        service=service,
        delivery_for=lambda broker, session: deliveries[session.clerk_id],
    )
    app.include_router(broker_clerks.router)
    return app


def _alpaca_lane(service: FleetControlService, tmp_path: Path, label: str):
    lane = provision_lane(service, broker="alpaca", label=label, tmp_path=tmp_path)
    bind_lane(service, lane, account=f"acct-{label}")
    return lane


def _attention_answer(account_id: str) -> dict:
    return {
        "account_id": account_id,
        "items": [
            {
                "condition_id": "unc-1",
                "reason_code": "EXIT_NOT_FLAT",
                "kind": "uncertainty",
                "severity": "blocking",
                "strategy_instance_id": "ema-1",
                "symbol": "SPY",
                "headline": "This bot's exit has not flattened its position",
            }
        ],
    }


@pytest.mark.asyncio
async def test_each_lane_gets_its_own_slice_and_a_failure_is_unknown_not_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = _service(tmp_path / "control")
    healthy = _alpaca_lane(service, tmp_path, "attention-ok")
    broken = _alpaca_lane(service, tmp_path, "attention-broken")
    # A lane of a provider that serves no attention read: it must be absent
    # from the answer entirely, not failed — no bell is rendered for it.
    provision_lane(service, broker="fake_alpha", label="no-bell", tmp_path=tmp_path)
    healthy_delivery = _ScriptedDelivery(answer=_attention_answer("acct-attention-ok"))
    broken_delivery = _ScriptedDelivery(
        fail_with=ClerkUnreachable("agent timed out reading attention")
    )
    app = _coordinator_app(
        service, {healthy.clerk_id: healthy_delivery, broken.clerk_id: broken_delivery}
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["observed_at_ms"], int)
    lanes = {lane["clerk_id"]: lane for lane in body["lanes"]}
    assert set(lanes) == {healthy.clerk_id, broken.clerk_id}
    assert lanes[healthy.clerk_id]["ok"] is True
    # The lane's own answer, untouched by the coordinator.
    assert lanes[healthy.clerk_id]["value"] == _attention_answer("acct-attention-ok")
    assert lanes[broken.clerk_id]["ok"] is False
    assert lanes[broken.clerk_id]["error_reason"] == "clerk_unreachable"
    assert "value" not in lanes[broken.clerk_id]
    # The delivered operation is the adapter-declared attention read — the
    # same catalog entry the snapshot pins — hitting the agent's own path.
    (request,) = healthy_delivery.requests
    assert request.operation.operation_id == "attention_read"
    assert request.broker == "alpaca"
    # The broken lane was genuinely asked and failed in delivery — never
    # silently skipped.
    (failed_request,) = broken_delivery.requests
    assert failed_request.operation.operation_id == "attention_read"


@pytest.mark.asyncio
async def test_a_roster_with_no_alpaca_lanes_answers_an_explicit_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = _service(tmp_path / "control")
    app = _coordinator_app(service, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    assert response.json()["lanes"] == []


@pytest.mark.asyncio
async def test_a_deployment_without_the_alpaca_adapter_refuses_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = _service(tmp_path / "control", with_alpaca=False)
    app = _coordinator_app(service, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            _AGGREGATE_ROUTE, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 404
    assert response.json()["reason"] == "broker_not_supported"


@pytest.mark.asyncio
async def test_the_route_rejects_a_request_without_the_secret_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.broker.fleet.errors import DataPlaneControlSecretRefused
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    service = _service(tmp_path / "control")
    app = _coordinator_app(service, {})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(_AGGREGATE_ROUTE)

    assert response.status_code == 403
    assert response.json()["reason"] == DataPlaneControlSecretRefused.reason
