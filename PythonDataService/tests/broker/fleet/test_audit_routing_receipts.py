"""The audit read surface (#2104): ``GET /api/broker-clerks/audit/routing-receipts``.

The durable routing-receipt trail was append-only, trigger-protected, and
reachable only by opening the coordinator's SQLite file by hand. This is the
one read-only, secret-gated, coordinator-only route that converts that into
a curl-able surface — no idempotency key, no command envelope, no new
storage, no ceremony change.

Covers, per ``.claude/rules/temporal-rigor.md`` and ``.claude/rules/
numerical-rigor.md``:

- the store's ``since_ms`` inclusive lower bound, proved in both directions
  (reddens if the bound became exclusive; reddens if it were dropped);
- the secret gate, proved in both directions (with header -> 200, without
  -> refused);
- the 503-when-uninstalled path, proved against a working positive control;
- that every timestamp on the wire is ``int64 ms UTC`` -- no ISO string, no
  ``datetime`` object, anywhere in the response.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.fleet.errors import DataPlaneControlSecretRefused, FleetControlPlaneNotInstalled
from app.broker.fleet.records import RoutingReceiptRecord, RoutingReceiptState
from app.broker.fleet.service import FleetControlService
from app.config import settings
from app.routers import broker_clerks
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.fleet.conftest import FrozenClock, bind_lane, provision_lane

ROUTE = "/api/broker-clerks/audit/routing-receipts"
_TEST_SECRET = "test-audit-secret"
_ISO_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _coordinator_app(fleet_service: FleetControlService | None) -> FastAPI:
    """The minimal coordinator surface this read needs: no lane router, no agent."""
    app = FastAPI()
    if fleet_service is not None:
        app.state.fleet_service = fleet_service
    install_fleet_control_error_handler(app)
    app.include_router(broker_clerks.router)
    return app


def _open_and_settle(
    fleet_service: FleetControlService,
    lane,
    *,
    key: str,
    target: str = "strategy/sid-audit",
    outcome: RoutingReceiptState = RoutingReceiptState.DELIVERED,
) -> RoutingReceiptRecord:
    """Open, dispatch and settle one routing attempt; return the settled receipt."""
    attempt = fleet_service.open_routing_attempt(
        broker=lane.broker,
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref=target,
        idempotency_key=key,
        pinned_routing_epoch=1,
        pinned_binding_generation=1,
        pinned_agent_instance_id="agnt_111111111111111111111111",
    )
    fleet_service.mark_routing_dispatched(correlation_id=attempt.correlation_id)
    upstream_ref = "upstream/r-1" if outcome == RoutingReceiptState.DELIVERED else None
    return fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id, outcome=outcome, upstream_receipt_ref=upstream_ref
    )


# ---- store: since_ms is an inclusive lower bound, proved both directions --


def test_store_since_ms_is_inclusive_of_the_boundary(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """A receipt created exactly at ``since_ms`` is included.

    Reddens if the store's comparison were made exclusive (``>`` instead of
    the contractually-required ``>=``).
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="bound", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-bound")

    clock.advance(1_000)
    boundary = clock()
    at_boundary = _open_and_settle(fleet_service, lane, key="bound-at")
    assert at_boundary.created_at_ms == boundary  # the fixture's own premise

    receipts = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id, since_ms=boundary)

    assert at_boundary.correlation_id in {r.correlation_id for r in receipts}


def test_store_since_ms_excludes_receipts_strictly_before_the_bound(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """A receipt created strictly before ``since_ms`` is excluded.

    Reddens if the ``since_ms`` filter were dropped entirely (the older
    receipt would then appear). The unfiltered read is asserted non-empty
    and to carry both receipts first, so this is not a vacuous pass against
    a store that returns nothing for unrelated reasons.
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="exclude", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-exclude")

    before = _open_and_settle(fleet_service, lane, key="exclude-before")
    clock.advance(1_000)
    boundary = clock()
    at_boundary = _open_and_settle(fleet_service, lane, key="exclude-at")

    # Positive control: both receipts really exist before any since_ms filter
    # is applied at all.
    unfiltered = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)
    assert {r.correlation_id for r in unfiltered} == {before.correlation_id, at_boundary.correlation_id}

    receipts = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id, since_ms=boundary)
    ids = {r.correlation_id for r in receipts}

    assert before.correlation_id not in ids
    assert at_boundary.correlation_id in ids


# ---- service: the response envelope carries exactly the audit's fields ----


def test_service_list_routing_receipts_envelope_shape(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    lane = provision_lane(fleet_service, broker="fake_alpha", label="shape", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-shape")
    settled = _open_and_settle(fleet_service, lane, key="shape-1")

    result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)

    assert set(result) == {"observed_at_ms", "receipts"}
    assert isinstance(result["observed_at_ms"], int)
    assert len(result["receipts"]) == 1
    entry = result["receipts"][0]
    assert set(entry) == {
        "correlation_id",
        "clerk_id",
        "broker",
        "operation_kind",
        "routing_state",
        "created_at_ms",
        "dispatched_at_ms",
    }
    assert entry["correlation_id"] == settled.correlation_id
    assert entry["clerk_id"] == lane.clerk_id
    assert entry["broker"] == lane.broker
    assert entry["operation_kind"] == "bot_action"
    assert entry["routing_state"] == "delivered"
    assert entry["created_at_ms"] == settled.created_at_ms
    assert entry["dispatched_at_ms"] == settled.dispatched_at_ms


# ---- HTTP: the secret gate, proved in both directions ---------------------


@pytest.mark.asyncio
async def test_http_rejects_the_request_without_the_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="gate", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-gate")
    _open_and_settle(fleet_service, lane, key="gate-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(ROUTE, params={"since_ms": 0})

    assert response.status_code == 403
    body = response.json()
    assert body["reason"] == DataPlaneControlSecretRefused.reason
    assert CONTROL_SECRET_HEADER in body["message"]
    assert "detail" not in body
    assert set(body).issubset({"reason", "message", "next_step"})


@pytest.mark.asyncio
async def test_http_accepts_the_request_with_the_correct_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="gate-ok", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-gate-ok")
    settled = _open_and_settle(fleet_service, lane, key="gate-ok-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE,
            params={"since_ms": 0, "clerk_id": lane.clerk_id},
            headers={CONTROL_SECRET_HEADER: _TEST_SECRET},
        )

    assert response.status_code == 200
    body = response.json()
    assert [r["correlation_id"] for r in body["receipts"]] == [settled.correlation_id]


@pytest.mark.asyncio
async def test_http_rejects_the_wrong_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: "wrong"}
        )

    assert response.status_code == 403
    assert response.json()["reason"] == DataPlaneControlSecretRefused.reason


# ---- HTTP: coordinator-only, 503 when no fleet_service is installed -------


@pytest.mark.asyncio
async def test_http_503s_when_fleet_service_is_absent_not_500_and_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-coordinator process (no ``app.state.fleet_service``) refuses
    with the existing 503 family -- never a raw 500, never a silent empty
    200."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == FleetControlPlaneNotInstalled.reason
    assert "detail" not in body
    assert "receipts" not in body


@pytest.mark.asyncio
async def test_http_serves_receipts_when_fleet_service_is_installed_positive_control(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the 503 test above: the identical request against
    an otherwise-identical app that *does* carry ``fleet_service`` succeeds,
    so the 503 is proven to come from the missing service, not from some
    other misconfiguration shared by both apps."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="present", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-present")
    _open_and_settle(fleet_service, lane, key="present-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    assert response.json()["receipts"]


# ---- HTTP: temporal rigor — int64 ms UTC only, nothing else on the wire ---


@pytest.mark.asyncio
async def test_http_response_carries_only_int64_ms_utc_timestamps(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="temporal", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-temporal")
    _open_and_settle(fleet_service, lane, key="temporal-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    assert not _ISO_LIKE.search(response.text), "no ISO-8601 instant may reach the wire"
    body = response.json()
    assert body["receipts"], "positive control: the response actually carries receipts"
    assert isinstance(body["observed_at_ms"], int)
    for entry in body["receipts"]:
        assert isinstance(entry["created_at_ms"], int)
        assert entry["dispatched_at_ms"] is None or isinstance(entry["dispatched_at_ms"], int)


@pytest.mark.asyncio
async def test_http_since_ms_bound_is_le_max_timestamp_ms(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``since_ms`` accepts the domain ceiling and refuses one past it --
    never ``2**63 - 1``, which would state a wire ceiling higher than what
    this route actually enforces."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        at_ceiling = await client.get(ROUTE, params={"since_ms": MAX_TIMESTAMP_MS}, headers=headers)
        past_ceiling = await client.get(
            ROUTE, params={"since_ms": MAX_TIMESTAMP_MS + 1}, headers=headers
        )
        negative = await client.get(ROUTE, params={"since_ms": -1}, headers=headers)
        missing = await client.get(ROUTE, headers=headers)

    assert at_ceiling.status_code == 200
    assert past_ceiling.status_code == 422
    assert negative.status_code == 422
    assert missing.status_code == 422


@pytest.mark.asyncio
async def test_http_limit_is_bounded_one_to_five_hundred(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        zero = await client.get(ROUTE, params={"since_ms": 0, "limit": 0}, headers=headers)
        too_many = await client.get(ROUTE, params={"since_ms": 0, "limit": 501}, headers=headers)
        max_ok = await client.get(ROUTE, params={"since_ms": 0, "limit": 500}, headers=headers)

    assert zero.status_code == 422
    assert too_many.status_code == 422
    assert max_ok.status_code == 200
