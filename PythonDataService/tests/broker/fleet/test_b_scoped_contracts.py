"""Delivery B qualification: complete scoped contracts (PRD §10.1–§10.4).

A real coordinator surface (registry + clerk-scoped router) forwards to a
real agent process over a real socket — the same qualification posture as
the A2 lane tests, now through the public routes the frontend will call.
Every §10.4 refusal family, the §10.3 command envelope, per-event stream
provenance and the composed auth policy are pinned here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.delivery import (
    COORDINATOR_TOKEN_HEADER,
    DeliveryRequest,
)
from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.provider import FLEET_PROTOCOL_VERSION
from app.broker.fleet.records import RoutingReceiptState
from app.broker.fleet.routing import LaneRouter
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from tests.broker.fleet.conftest import FrozenClock
from tests.broker.fleet.test_a2_alpaca_lane import _RealServer

ACCOUNT = "abcdef01-1234-abcd-5678-ef0123456789"
AGENT_TOKEN = "svct_" + "a" * 32
COORDINATOR_TOKEN = "svct_" + "c" * 32
#: Fixed identities for the in-process local-delivery fixture agent.
CLERK_ID = "clrk_bagent00000000000000aa"
EPOCH = 7


def _build_agent_app(
    identity: dict[str, object],
    *,
    flip_identity_after_first_event: bool = False,
    minted_receipts: list[str] | None = None,
    refuse_unpinned_mutations: bool = False,
) -> FastAPI:
    """A production-shaped agent serving one lane's identity.

    ``identity`` is the runtime's live identity state — shared by reference
    so the stale-stream test can move it the way a re-registration would.
    ``minted_receipts`` is the agent's own durable-receipt ledger — shared by
    reference so a caller can assert against what the agent actually minted,
    not a string reflected back from the request. ``refuse_unpinned_mutations``
    mirrors the ``clerk_agent`` posture (#2075); the default mirrors
    ``combined``, where the browser legitimately mutates without a pin.
    """
    from app.broker.fleet.agent_identity import (
        SERVED_IDENTITY_STATE_KEY,
        FleetIdentityMiddleware,
    )
    from app.security.data_plane_control import require_data_plane_control_secret_always
    from app.utils.error_handlers import install_fleet_control_error_handler

    agent = FastAPI()
    agent.add_middleware(
        FleetIdentityMiddleware, refuse_unpinned_mutations=refuse_unpinned_mutations
    )
    # Production-shaped: the coordinator's real app registers this handler
    # too (app.main), because the control-secret guard raises outside any
    # route-local try/except (#2067).
    install_fleet_control_error_handler(agent)
    ledger = minted_receipts if minted_receipts is not None else []

    def _served() -> dict[str, object] | None:
        return identity

    setattr(agent.state, SERVED_IDENTITY_STATE_KEY, _served)

    @agent.get(
        "/api/brokers/alpaca/account",
        dependencies=[
            # Production shape: the real lane-side guard. The combined
            # posture's raw-ASGI dispatch must present the coordinator
            # token so this guard authorizes the forwarded read.
            Depends(require_data_plane_control_secret_always),
        ],
    )
    async def account() -> JSONResponse:
        return JSONResponse({"account_id": ACCOUNT, "status": "ACTIVE"})

    @agent.get("/api/brokers/alpaca/activities")
    async def activities() -> JSONResponse:
        return JSONResponse([{"activity_type": "FILL"}])

    @agent.get("/api/brokers/alpaca/portfolio-history")
    async def portfolio_history() -> JSONResponse:
        return JSONResponse({"timestamps": [], "equity": []})

    @agent.get("/api/brokers/alpaca/portfolio-history-proof")
    async def portfolio_history_proof() -> JSONResponse:
        return JSONResponse({"history": {"timestamps": [], "equity": []}})

    @agent.get("/api/brokers/alpaca/clerk/status")
    async def clerk_status() -> JSONResponse:
        return JSONResponse({"account_id": ACCOUNT, "outstanding_intents": 0})

    @agent.get("/api/brokers/alpaca/clerk/custody-diagnosis")
    async def custody_diagnosis() -> JSONResponse:
        return JSONResponse({"divergences": []})

    @agent.get(
        "/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/current"
    )
    async def current_run(account_id: str, sid: str) -> JSONResponse:
        return JSONResponse({"account_id": account_id, "sid": sid, "run_id": "current"})

    @agent.get(
        "/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/runs/history"
    )
    async def run_history(account_id: str, sid: str) -> JSONResponse:
        return JSONResponse({"account_id": account_id, "sid": sid, "runs": []})

    @agent.get("/api/brokers/alpaca/configuration/selection")
    async def selection() -> JSONResponse:
        return JSONResponse({"effective_account_id": ACCOUNT.upper()})

    @agent.post("/api/brokers/alpaca/configuration/profiles")
    async def create_profile(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"profile_id": "prof_new", "created": True})

    @agent.get(
        "/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/panel",
        dependencies=None,
    )
    async def panel(account_id: str, sid: str) -> JSONResponse:
        return JSONResponse({"account_id": account_id, "sid": sid, "state": "running"})

    @agent.post(
        "/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/actions",
        dependencies=None,
    )
    async def action(account_id: str, sid: str, request: Request) -> JSONResponse:
        body = await request.json()
        # A real provider's durable receipt is minted by the provider. Deriving
        # it from the caller's idempotency key would make the coordinator's
        # "carried, never fabricated" contract unfalsifiable.
        receipt_id = f"provider/receipt-{len(ledger) + 1:04d}-{uuid.uuid4().hex[:8]}"
        ledger.append(receipt_id)
        return JSONResponse(
            {
                "outcome": "confirmed",
                "account_id": account_id,
                "sid": sid,
                "idempotency_key": body.get("idempotency_key"),
                "receipt_id": receipt_id,
            }
        )

    @agent.post(
        "/api/brokers/alpaca/accounts/{account_id}/manual-orders/{order_ref:path}/cancel",
    )
    async def manual_cancel(order_ref: str) -> JSONResponse:
        return JSONResponse({"cancelled": order_ref})

    @agent.get("/api/brokers/alpaca/accounts/{account_id}/gallery/stream")
    async def gallery(account_id: str) -> StreamingResponse:
        async def events():
            yield f'event: update\ndata: {{"i": 0, "account": "{account_id}"}}\n\n'.encode()
            await asyncio.sleep(0.02)
            if flip_identity_after_first_event:
                # The lane's session moved: the stream keeps sending bytes,
                # but the provenance the middleware stamps now names the new
                # identity — the coordinator must close the stream.
                identity["routing_epoch"] = 8
            yield f'event: update\ndata: {{"i": 1, "account": "{account_id}"}}\n\n'.encode()
            await asyncio.sleep(0.02)
            yield f'event: update\ndata: {{"i": 2, "account": "{account_id}"}}\n\n'.encode()

        return StreamingResponse(events(), media_type="text/event-stream")

    return agent


class _Lane:
    """One routable alpaca lane in a fresh registry."""

    def __init__(self, control_dir: Path) -> None:
        self.service = FleetControlService(
            store=FleetRegistryStore.open(control_dir=control_dir),
            provider_adapters=production_provider_adapters(),
        )
        volume_root = control_dir.parent / "volumes" / "paper"
        volume_root.mkdir(parents=True, exist_ok=True)
        provisioned = self.service.provision_clerk(
            broker="alpaca",
            display_label="Paper",
            volume_root=volume_root,
            attestation_id="learn-ai-alpaca-paper",
            deployment_namespace="compose:test",
        )
        self.clerk_id = provisioned.clerk.clerk_id
        self.worker_key = provisioned.clerk.worker_key

    def approve(self, base_url: str) -> None:
        self.service.approve_endpoint(
            clerk_id=self.clerk_id, endpoint_ref="agent:paper-1", base_url=base_url
        )

    def register_and_confirm(self, coordinator_base_url: str) -> dict[str, object]:
        """Register as an agent would and return the *response* it received.

        The agent never reads the registry; it knows only what the
        registration answered. This calls the coordinator's real internal
        registration route (``app/routers/internal_fleet.py``, unmodified)
        over the test's own socket and parses the JSON body it returns — the
        same wire contract ``RemotePresence.register`` parses on the agent
        side (``app/broker/fleet/presence.py:377-378``). Building the served
        identity from that body — not from the ``ClerkSessionRecord`` the
        coordinator also reads — is what lets ``verify_identity_echo`` fail
        when the coordinator pins a different epoch than the lane serves. A
        direct call to ``LocalPresence.register`` would still read the same
        in-memory session object the confirm below reads; only the real HTTP
        round trip forces the two paths apart.
        """
        with httpx.Client(base_url=coordinator_base_url, timeout=10.0) as client:
            response = client.post(
                "/internal/fleet/sessions",
                headers={
                    "X-Fleet-Agent-Token": AGENT_TOKEN,
                    "X-Fleet-Clerk-Id": self.clerk_id,
                },
                json={
                    "clerk_id": self.clerk_id,
                    "worker_key": self.worker_key,
                    "agent_instance_id": "agnt_b00000000000000000000000a",
                    "endpoint_ref": "agent:paper-1",
                    "fleet_protocol_version": FLEET_PROTOCOL_VERSION,
                },
            )
        response.raise_for_status()
        registration_response: dict[str, object] = response.json()
        self.service.reserve_assignment(
            broker="alpaca", clerk_id=self.clerk_id, external_account_id=ACCOUNT
        )
        self.service.confirm_assignment(
            broker="alpaca",
            clerk_id=self.clerk_id,
            external_account_id=ACCOUNT,
            binding_generation=3,
            agent_instance_id=str(registration_response["agent_instance_id"]),
            routing_epoch=int(registration_response["routing_epoch"]),
            effective_profile_id="prof_b",
            effective_revision=1,
        )
        return registration_response

    def close(self) -> None:
        self.service.close()


def _coordinator_app(lane: _Lane) -> FastAPI:
    """The public surface: registry + clerk-scoped router + lane router.

    ``delivery_for`` is the production resolver (``coordinator_delivery_for``):
    it reads the approved-endpoint row the host ceremony writes and the
    clerk's coordinator token, and refuses before any socket is opened for a
    session citing an endpoint the row does not back. Also mounts the
    coordinator's real internal registration route (``/internal/fleet``,
    unmodified) so ``_Lane.register_and_confirm`` can register the way a real
    agent does — over HTTP, parsing the JSON body — instead of reading the
    registry's session object directly.
    """
    from app.broker.fleet.routing import coordinator_delivery_for
    from app.routers import broker_clerks, internal_fleet

    coordinator = FastAPI()
    coordinator.state.fleet_service = lane.service
    coordinator.state.fleet_agent_tokens_text = json.dumps(
        {lane.clerk_id: AGENT_TOKEN}
    )
    coordinator.state.fleet_lane_router = LaneRouter(
        service=lane.service,
        # The production resolver: it reads the approved-endpoint row and the
        # clerk's coordinator token, and refuses before any socket is opened.
        delivery_for=coordinator_delivery_for(
            lane.service, {lane.clerk_id: COORDINATOR_TOKEN}
        ),
    )
    broker_clerks.register_catalog_operations(
        {broker: adapter.operations() for broker, adapter in production_provider_adapters().items()}
    )
    coordinator.include_router(broker_clerks.router)
    coordinator.include_router(internal_fleet.router)
    return coordinator


class _Fleet:
    """One agent and one coordinator, both on real loopback sockets."""

    def __init__(
        self, tmp_path: Path, *, flip_identity_after_first_event: bool = False
    ) -> None:
        self.lane = _Lane(tmp_path / "control")
        # The agent's live identity: starts at a placeholder epoch until the
        # coordinator's own registration route (below) answers for real, so
        # the echo check pins the real lane rather than a value the fixture
        # read off the registry itself.
        self.identity: dict[str, object] = {
            "broker": "alpaca",
            "clerk_id": self.lane.clerk_id,
            "routing_epoch": 1,
            "binding_generation": 3,
        }
        #: The agent's own durable-receipt ledger: what it actually minted,
        #: not a string reflected back from the caller's request.
        self.minted_receipts: list[str] = []
        # Both servers are constructed (but not yet listening) before the
        # try below, so the except branch can always reach them — an
        # exception anywhere from the first socket bind through
        # registration must not leak a listening server or the lane's
        # registry connection past this constructor.
        self.agent = _RealServer(
            _build_agent_app(
                self.identity,
                flip_identity_after_first_event=flip_identity_after_first_event,
                minted_receipts=self.minted_receipts,
            )
        )
        self.coordinator = _RealServer(_coordinator_app(self.lane))
        try:
            self.agent.start()
            self.lane.approve(self.agent.base_url)
            self.coordinator.start()
            registration_response = self.lane.register_and_confirm(
                self.coordinator.base_url
            )
        except Exception:
            self.coordinator.stop()
            self.agent.stop()
            self.lane.close()
            raise
        self.identity["routing_epoch"] = int(registration_response["routing_epoch"])

    @property
    def base(self) -> str:
        return f"{self.coordinator.base_url}/api/brokers/alpaca/clerks/{self.lane.clerk_id}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.coordinator.base_url, timeout=10.0)

    def stop(self) -> None:
        self.coordinator.stop()
        self.agent.stop()
        self.lane.close()


@pytest.fixture
def fleet(tmp_path: Path) -> _Fleet:
    """One qualified fleet: real registry, agent and coordinator processes."""
    handle = _Fleet(tmp_path)
    yield handle
    handle.stop()


async def test_the_directory_lists_lanes_with_catalog_capabilities(
    fleet: _Fleet,
) -> None:
    """§10.1: the broker-neutral directory derives from the registry."""
    async with fleet.client() as client:
        directory = await client.get("/api/broker-clerks")
        assert directory.status_code == 200
        entry = directory.json()["clerks"][0]
        assert entry["broker"] == "alpaca"
        assert entry["clerk_id"] == fleet.lane.clerk_id
        assert entry["lifecycle_state"] == "ready"
        capabilities = set(entry["capabilities"])
        assert "custody_command" in capabilities
        assert "manual_orders" in capabilities
        assert "deploy" in capabilities
        assert "market_status_read" in capabilities

        filtered = await client.get("/api/brokers/alpaca/clerks")
        assert [c["clerk_id"] for c in filtered.json()["clerks"]] == [
            fleet.lane.clerk_id
        ]
        unregistered = await client.get("/api/brokers/nosuch/clerks")
        assert unregistered.status_code == 404
        assert unregistered.json()["reason"] == "broker_not_supported"

        described = await client.get(
            f"/api/brokers/alpaca/clerks/{fleet.lane.clerk_id}"
        )
        assert described.status_code == 200
        wrong = await client.get(
            f"/api/brokers/nosuch/clerks/{fleet.lane.clerk_id}"
        )
        assert wrong.status_code == 409
        assert wrong.json()["reason"] == "clerk_broker_mismatch"


async def test_an_unregistered_broker_is_a_typed_404_not_an_empty_lane_list(
    fleet: _Fleet,
) -> None:
    """`GET /brokers/{broker}/clerks` for a broker with no production adapter
    must refuse the same way `_lookup_operation` already does for the same
    condition — not answer 200 with an empty ``clerks`` list, which is
    indistinguishable from "this provider has zero lanes today"."""
    async with fleet.client() as client:
        response = await client.get("/api/brokers/nosuch/clerks")
        assert response.status_code == 404
        assert response.json()["reason"] == "broker_not_supported"


async def test_lane_reads_route_through_the_public_surface(fleet: _Fleet) -> None:
    """A lane read forwards, echoes identity and returns the provider body."""
    async with fleet.client() as client:
        response = await client.get(f"{fleet.base}/account")
        assert response.status_code == 200
        assert response.json()["account_id"] == ACCOUNT


async def test_b2_desk_reads_and_account_bound_run_evidence_route_through_the_lane(
    fleet: _Fleet,
) -> None:
    """The C desk's full operational read set stays inside one lane route."""
    async with fleet.client() as client:
        for path in (
            "/activities?current_session=true",
            "/portfolio-history?range=1D",
            "/portfolio-history-proof?range=1D",
            "/clerk/status",
            "/clerk/custody-diagnosis",
            f"/accounts/{ACCOUNT}/bots/sid-9/runs/current",
            f"/accounts/{ACCOUNT}/bots/sid-9/runs/history?limit=1",
        ):
            response = await client.get(f"{fleet.base}{path}")
            assert response.status_code == 200, path
            assert response.headers["x-fleet-broker"] == "alpaca", path
            assert response.headers["x-fleet-clerk-id"] == fleet.lane.clerk_id, path

        configuration = await client.get(f"{fleet.base}/configuration/selection")
        assert configuration.status_code == 200
        assert configuration.json()["effective_account_id"] == ACCOUNT.upper()


async def test_a_wrong_target_account_refuses_without_dispatch(fleet: _Fleet) -> None:
    """§10.4: the path's account is checked against the confirmed assignment."""
    async with fleet.client() as client:
        wrong = await client.get(
            f"{fleet.base}/accounts/00000000-0000-0000-0000-000000000000"
            f"/bots/sid-1/panel"
        )
        assert wrong.status_code == 409
        assert wrong.json()["reason"] == "clerk_account_mismatch"


async def test_an_unsupported_broker_or_lane_refuses(fleet: _Fleet) -> None:
    """§10.4: broker_not_supported and clerk_not_found at their pinned statuses."""
    async with fleet.client() as client:
        unsupported = await client.get(
            f"/api/brokers/ibkr/clerks/{fleet.lane.clerk_id}/account"
        )
        assert unsupported.status_code == 404
        assert unsupported.json()["reason"] == "broker_not_supported"

        unknown = await client.get(
            "/api/brokers/alpaca/clerks/clrk_0000000000000000000000zz/account"
        )
        assert unknown.status_code == 404
        assert unknown.json()["reason"] == "clerk_not_found"


async def test_the_command_envelope_is_required_and_coherent(fleet: _Fleet) -> None:
    """§10.3: no implicit canonical command — the envelope is the contract."""
    actions = f"{fleet.base}/accounts/{ACCOUNT}/bots/sid-9/actions"
    async with fleet.client() as client:
        missing = await client.post(actions, json={"action_id": "arm"})
        assert missing.status_code == 422
        assert missing.json()["reason"] == "command_envelope_invalid"

        wrong_capability = await client.post(
            actions,
            json={
                "action_id": "arm",
                "command_context": {
                    "capability": "custody_command",
                    "idempotency_key": "dk-1",
                    "expected_effective_binding_generation": 3,
                },
            },
        )
        assert wrong_capability.status_code == 422

        no_key = await client.post(
            actions,
            json={
                "action_id": "arm",
                "command_context": {
                    "capability": "bot_action",
                    "expected_effective_binding_generation": 3,
                },
            },
        )
        assert no_key.status_code == 422

        disagreement = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "dk-2",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "dk-3",
                    "expected_effective_binding_generation": 3,
                },
            },
        )
        assert disagreement.status_code == 422

        stale_generation = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "dk-4",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "dk-4",
                    "expected_effective_binding_generation": 99,
                },
            },
        )
        assert stale_generation.status_code == 409
        assert stale_generation.json()["reason"] == "clerk_binding_generation_conflict"


async def test_a_delivered_command_persists_its_routing_receipt(fleet: _Fleet) -> None:
    """D11: pinned attempt before dispatch; delivered is terminal with receipt."""
    actions = f"{fleet.base}/accounts/{ACCOUNT}/bots/sid-9/actions"
    async with fleet.client() as client:
        delivered = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "dk-42",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "dk-42",
                    "expected_effective_binding_generation": 3,
                },
            },
        )
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["receipt_id"] == fleet.minted_receipts[-1]
        assert delivered.headers["x-fleet-routing-state"] == "delivered"
        correlation = delivered.headers["x-fleet-correlation-id"]

    receipts = fleet.lane.service._store.list_routing_receipts(
        clerk_id=fleet.lane.clerk_id
    )
    receipt = next(r for r in receipts if r.correlation_id == correlation)
    assert receipt.state == RoutingReceiptState.DELIVERED
    assert receipt.idempotency_key == "dk-42"
    assert receipt.upstream_receipt_ref == fleet.minted_receipts[-1]
    assert "dk-42" not in receipt.upstream_receipt_ref
    assert receipt.pinned_routing_epoch is not None
    assert receipt.pinned_binding_generation == 3


async def test_the_manual_order_path_converter_routes(fleet: _Fleet) -> None:
    """A slashed order reference rides the catalog's ``:path`` converter."""
    cancel = f"{fleet.base}/accounts/{ACCOUNT}/manual-orders/ord%2F1/cancel"
    async with fleet.client() as client:
        response = await client.post(
            cancel,
            json={
                "command_context": {
                    "capability": "manual_orders",
                    "idempotency_key": "mc-1",
                    "expected_effective_binding_generation": 3,
                }
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"cancelled": "ord/1"}


async def test_stream_events_carry_and_verify_provenance(fleet: _Fleet) -> None:
    """FR-076: every event proves its origin; the frames re-emit it publicly."""
    async with fleet.client() as client, client.stream(
        "GET", f"{fleet.base}/accounts/{ACCOUNT}/gallery/stream"
    ) as response:
        assert response.status_code == 200
        text = "".join(
            [chunk async for chunk in response.aiter_text()]
        )
    assert 'data: {"i": 2' in text
    assert "x-fleet-broker: alpaca" in text
    assert f"x-fleet-clerk-id: {fleet.lane.clerk_id}" in text
    assert f"x-fleet-routing-epoch: {fleet.identity['routing_epoch']}" in text
    assert "x-fleet-binding-generation: 3" in text


async def test_a_stale_identity_midstream_closes_the_stream(tmp_path: Path) -> None:
    """A lane that moves mid-stream cannot keep feeding the old consumer."""
    fleet = _Fleet(tmp_path, flip_identity_after_first_event=True)
    try:
        collected: list[str] = []
        async with fleet.client() as client:
            # The transport shapes of a server-closed stream vary by client
            # version; what matters is which events were delivered.
            try:
                async with client.stream(
                    "GET", f"{fleet.base}/accounts/{ACCOUNT}/gallery/stream"
                ) as response:
                    async for chunk in response.aiter_text():
                        collected.append(chunk)
            except httpx.HTTPError:
                pass  # the closure the test asks for
        text = "".join(collected)
        # The first event (old identity) arrived; the second (new epoch)
        # closed the stream instead of being delivered.
        assert 'data: {"i": 0' in text
        assert 'data: {"i": 1' not in text
    finally:
        fleet.stop()


async def test_commands_without_an_approved_endpoint_refuse(tmp_path: Path) -> None:
    """A session citing an unapproved endpoint is not routable (§10.4)."""
    lane = _Lane(tmp_path / "control2")
    try:
        # A local-presence session cites no endpoint reference; routing it
        # over HTTP is a delivery-time refusal (§10.4 clerk_unreachable).
        session = lane.service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            agent_instance_id="agnt_b00000000000000000000000b",
            endpoint_ref=None,
            fleet_protocol_version=FLEET_PROTOCOL_VERSION,
        )
        lane.service.reserve_assignment(
            broker="alpaca", clerk_id=lane.clerk_id, external_account_id=ACCOUNT
        )
        lane.service.confirm_assignment(
            broker="alpaca",
            clerk_id=lane.clerk_id,
            external_account_id=ACCOUNT,
            binding_generation=1,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
            effective_profile_id="prof_b",
            effective_revision=1,
        )
        coordinator = _RealServer(_coordinator_app(lane))
        coordinator.start()
        try:
            async with httpx.AsyncClient(
                base_url=coordinator.base_url, timeout=10.0
            ) as client:
                response = await client.get(
                    f"/api/brokers/alpaca/clerks/{lane.clerk_id}/account"
                )
                assert response.status_code == 503
                assert response.json()["reason"] == "clerk_unreachable"
                # The refusal names the *approval ceremony*, which a transport
                # failure could never produce. This is the assertion that
                # distinguishes the gate from a connection refused.
                assert "no approved endpoint row" in response.json()["message"]
        finally:
            coordinator.stop()
    finally:
        lane.close()


async def test_a_session_citing_a_different_endpoint_than_the_approved_row_refuses(
    tmp_path: Path,
) -> None:
    """The approval is per-reference: an approved row for ``agent:paper-1``
    does not authorize a session that cites ``agent:paper-2``."""
    from app.broker.fleet.records import ClerkSessionRecord
    from app.utils.timestamps import now_ms_utc

    lane = _Lane(tmp_path / "control3")
    try:
        lane.approve("http://127.0.0.1:1")
        # A registration citing a reference the deployment never approved is
        # already refused at registration time (``register_agent_session``
        # checks the same approved row) — so the only way to reach the
        # routing-layer branch is a session record that already cites a
        # different reference, written directly the way a stale or
        # hand-edited row would be.
        with lane.service._store.transaction() as conn:
            lane.service._store.upsert_session(
                conn,
                ClerkSessionRecord(
                    broker="alpaca",
                    clerk_id=lane.clerk_id,
                    agent_instance_id="agnt_b00000000000000000000000c",
                    routing_epoch=1,
                    started_at_ms=now_ms_utc(),
                    last_seen_at_ms=now_ms_utc(),
                    endpoint_ref="agent:paper-2",
                ),
            )
        lane.service.reserve_assignment(
            broker="alpaca", clerk_id=lane.clerk_id, external_account_id=ACCOUNT
        )
        lane.service.confirm_assignment(
            broker="alpaca",
            clerk_id=lane.clerk_id,
            external_account_id=ACCOUNT,
            binding_generation=1,
            agent_instance_id="agnt_b00000000000000000000000c",
            routing_epoch=1,
            effective_profile_id="prof_b",
            effective_revision=1,
        )
        coordinator = _RealServer(_coordinator_app(lane))
        coordinator.start()
        try:
            async with httpx.AsyncClient(
                base_url=coordinator.base_url, timeout=10.0
            ) as client:
                response = await client.get(
                    f"/api/brokers/alpaca/clerks/{lane.clerk_id}/account"
                )
                assert response.status_code == 503
                assert response.json()["reason"] == "clerk_unreachable"
                assert "no approved endpoint row" in response.json()["message"]
        finally:
            coordinator.stop()
    finally:
        lane.close()


async def test_the_composed_auth_policy_honors_both_caller_families(
    tmp_path: Path,
) -> None:
    """The browser secret terminates at the coordinator; the coordinator token rides."""
    from app.config import fleet_settings
    from app.security.data_plane_control import require_data_plane_control_secret

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3}
    )

    @agent.post(
        "/guarded", dependencies=[Depends(require_data_plane_control_secret)]
    )
    async def guarded() -> PlainTextResponse:
        return PlainTextResponse("ok")

    from app.config import settings

    server = _RealServer(agent)
    server.start()
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    original_allow = settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL
    original_token = fleet_settings.COORDINATOR_SERVICE_TOKEN
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL = False
    fleet_settings.COORDINATOR_SERVICE_TOKEN = COORDINATOR_TOKEN
    try:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5.0) as client:
            no_token = await client.post("/guarded")
            assert no_token.status_code == 403
            wrong = await client.post(
                "/guarded", headers={COORDINATOR_TOKEN_HEADER: "svct_" + "0" * 32}
            )
            assert wrong.status_code == 403
            forwarded = await client.post(
                "/guarded",
                headers={
                    COORDINATOR_TOKEN_HEADER: COORDINATOR_TOKEN,
                    # A fleet dispatch always pins the lane identity; the
                    # token alone is not a general-purpose bypass.
                    "X-Fleet-Broker": "alpaca",
                    "X-Fleet-Clerk-Id": CLERK_ID,
                },
            )
            assert forwarded.status_code == 200
            token_without_pin = await client.post(
                "/guarded", headers={COORDINATOR_TOKEN_HEADER: COORDINATOR_TOKEN}
            )
            assert token_without_pin.status_code == 403
            browser = await client.post(
                "/guarded",
                headers={"X-Data-Plane-Control-Secret": "test-plane-secret"},
            )
            assert browser.status_code == 200
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL = original_allow
        fleet_settings.COORDINATOR_SERVICE_TOKEN = original_token
        server.stop()


async def test_a_clerk_agent_refuses_an_unpinned_mutation_in_code() -> None:
    """Topology is the second layer, not the only one (#2075).

    This is the ``clerk_agent`` half. The ``combined`` half — that the real
    ``app.main.app`` wiring still serves an unpinned mutation at 200 — is
    proven by ``test_combined_role_still_serves_an_unpinned_mutation_at_200``
    below, which drives a request through the real app built under
    ``FLEET_ROLE=combined``. (``test_the_composed_auth_policy_honors_both_
    caller_families`` above only builds this file's isolated
    ``_build_agent_app()`` fixture and never touches ``app.main.app``, so it
    cannot see a regression in the ``main.py`` wiring line.)
    """
    from app.broker.fleet.errors import BrokerAndClerkRequired
    from app.config import fleet_settings, settings

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3},
        refuse_unpinned_mutations=True,
    )

    @agent.post("/guarded")
    async def guarded() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @agent.get("/guarded")
    async def guarded_read() -> PlainTextResponse:
        return PlainTextResponse("ok")

    server = _RealServer(agent)
    server.start()
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    original_token = fleet_settings.COORDINATOR_SERVICE_TOKEN
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    fleet_settings.COORDINATOR_SERVICE_TOKEN = COORDINATOR_TOKEN
    try:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5.0) as client:
            unpinned = await client.post(
                "/guarded", headers={"X-Data-Plane-Control-Secret": "test-plane-secret"}
            )
            assert unpinned.status_code == BrokerAndClerkRequired.status_code
            assert unpinned.json()["reason"] == BrokerAndClerkRequired.reason
            assert unpinned.json()["next_step"]

            unpinned_read = await client.get("/guarded")
            assert unpinned_read.status_code == 200

            forwarded = await client.post(
                "/guarded",
                headers={
                    COORDINATOR_TOKEN_HEADER: COORDINATOR_TOKEN,
                    "X-Fleet-Broker": "alpaca",
                    "X-Fleet-Clerk-Id": CLERK_ID,
                },
            )
            assert forwarded.status_code == 200
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        fleet_settings.COORDINATOR_SERVICE_TOKEN = original_token
        server.stop()


async def test_a_spoofed_clerk_id_pin_does_not_bypass_the_mutation_fence() -> None:
    """P1-B (Codex review, PR #2115): a bare clerk-id pin must not defeat it.

    A caller who merely echoes a known clerk id — not the coordinator's own
    proven forward — used to reach ``_pin_mismatch``, which treats an absent
    broker/epoch/generation pin as unconstrained rather than as missing
    proof; the route's own secret guard (``require_data_plane_control_secret``,
    unaware of any pin) then let it through on the browser secret alone. The
    fence must authenticate every mutation via ``lane_forward_is_authorized``,
    not merely require *some* clerk-id header to be present.
    """
    from app.broker.fleet.errors import BrokerAndClerkRequired
    from app.config import fleet_settings, settings
    from app.security.data_plane_control import require_data_plane_control_secret

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3},
        refuse_unpinned_mutations=True,
    )

    @agent.post(
        "/guarded-mutation",
        dependencies=[Depends(require_data_plane_control_secret)],
    )
    async def guarded_mutation() -> PlainTextResponse:
        return PlainTextResponse("ok")

    server = _RealServer(agent)
    server.start()
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    original_token = fleet_settings.COORDINATOR_SERVICE_TOKEN
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    fleet_settings.COORDINATOR_SERVICE_TOKEN = COORDINATOR_TOKEN
    try:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5.0) as client:
            # Knows the clerk id and the browser secret; has neither the
            # coordinator token nor the broker/epoch/generation pins a real
            # coordinator dispatch always attaches.
            spoofed = await client.post(
                "/guarded-mutation",
                headers={
                    "X-Data-Plane-Control-Secret": "test-plane-secret",
                    "X-Fleet-Clerk-Id": CLERK_ID,
                },
            )
            assert spoofed.status_code == BrokerAndClerkRequired.status_code
            assert spoofed.json()["reason"] == BrokerAndClerkRequired.reason
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        fleet_settings.COORDINATOR_SERVICE_TOKEN = original_token
        server.stop()


async def test_a_clerk_agent_still_answers_the_stranded_operator_mutations_unpinned() -> None:
    """P1-A (Codex review, PR #2115): the fence must not sever operator recovery.

    #2069/#2114 retain ``POST .../live-envelope/loss-hold/clear`` and
    ``POST .../runs/{run_id}/replay-receipt`` with no coordinator successor
    — an operator's only way to clear a live loss hold or regenerate a
    missing receipt is to call the clerk agent directly, unpinned. The
    mutation fence must exempt exactly these two, not sever them.
    """
    from app.config import settings
    from app.security.data_plane_control import require_data_plane_control_secret

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3},
        refuse_unpinned_mutations=True,
    )

    @agent.post(
        "/api/brokers/{broker}/live-envelope/loss-hold/clear",
        dependencies=[Depends(require_data_plane_control_secret)],
    )
    async def _loss_hold_clear(broker: str) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @agent.post(
        "/api/brokers/{broker}/bots/{strategy_instance_id}/runs/{run_id}/replay-receipt",
        dependencies=[Depends(require_data_plane_control_secret)],
    )
    async def _replay_receipt(
        broker: str, strategy_instance_id: str, run_id: str
    ) -> PlainTextResponse:
        return PlainTextResponse("ok")

    server = _RealServer(agent)
    server.start()
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    try:
        async with httpx.AsyncClient(base_url=server.base_url, timeout=5.0) as client:
            loss_hold = await client.post(
                "/api/brokers/alpaca/live-envelope/loss-hold/clear",
                headers={"X-Data-Plane-Control-Secret": "test-plane-secret"},
            )
            assert loss_hold.status_code == 200

            replay_receipt = await client.post(
                "/api/brokers/alpaca/bots/sid-1/runs/run-1/replay-receipt",
                headers={"X-Data-Plane-Control-Secret": "test-plane-secret"},
            )
            assert replay_receipt.status_code == 200
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        server.stop()


def test_combined_role_still_serves_an_unpinned_mutation_at_200() -> None:
    """`combined` IS the browser's data plane; #2075's fence must not reach it.

    Unlike the tests above, which build an isolated ``FastAPI()`` through
    this file's own ``_build_agent_app()``, this drives an actual unpinned
    POST through the real ``app.main.app`` — built fresh in a subprocess
    under ``FLEET_ROLE=combined``, the same shape as
    ``test_identity_middleware_role_scope.py`` (which imports the same
    module but only inspects installed middleware *class names* and so
    cannot see a regression in the ``refuse_unpinned_mutations=`` wiring at
    ``main.py``'s ``if _ROLE_RUNS_CLERK:`` block: a mutation-proof of that
    wiring going blanket-refuse left all of ``tests/broker/fleet`` green and
    only reddened unrelated router suites).
    """
    import json
    import os
    import subprocess
    import sys

    service_root = Path(__file__).resolve().parents[3]
    inherited = [
        entry
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != service_root / "tests"
    ]
    environment = {
        **os.environ,
        "FLEET_ROLE": "combined",
        "PYTHONPATH": os.pathsep.join([str(service_root), *inherited]),
    }
    probe = """
import asyncio
import json
import sys

sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]

import httpx
from app.main import app


@app.post('/__unpinned_mutation_probe__')
async def _probe() -> dict[str, bool]:
    return {'ok': True}


async def _run() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post('/__unpinned_mutation_probe__')
        sys.stdout.write(json.dumps({'status_code': response.status_code}) + '\\n')


asyncio.run(_run())
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        cwd=service_root,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["status_code"] == 200


async def test_combined_local_delivery_routes_in_process() -> None:
    """The combined posture dispatches through raw ASGI, streams unbuffered.

    The lane-side routes carry the always-on control-secret guard, so the
    dispatch's coordinator token must reach that guard through the raw-ASGI
    scope (lowercase header names, per the ASGI spec) with the secret
    enforced — an unauthenticated-control test environment would let the
    guard pass without the token and hide a broken dispatch.
    """
    from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
    from app.broker.fleet.routing import build_local_delivery
    from app.config import fleet_settings, settings

    agent = _build_agent_app(
        {"broker": "alpaca", "clerk_id": CLERK_ID,
         "routing_epoch": EPOCH, "binding_generation": 3}
    )
    original = fleet_settings.COORDINATOR_SERVICE_TOKEN
    fleet_settings.COORDINATOR_SERVICE_TOKEN = COORDINATOR_TOKEN
    original_secret = settings.DATA_PLANE_CONTROL_SECRET
    original_allow = settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL
    settings.DATA_PLANE_CONTROL_SECRET = "test-plane-secret"
    settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL = False
    try:
        delivery = build_local_delivery(agent)
        account_op = next(
            op for op in ALPACA_OPERATIONS if op.operation_id == "account_read"
        )
        result = await delivery.deliver(
            DeliveryRequest(
                broker="alpaca",
                clerk_id=CLERK_ID,
                operation=account_op,
                path_params={},
                routing_epoch=EPOCH,
                binding_generation=3,
            )
        )
        assert result.status_code == 200
        assert b"ACTIVE" in result.body
        assert result.headers["x-fleet-clerk-id"] == CLERK_ID

        stream_op = next(
            op for op in ALPACA_OPERATIONS if op.operation_id == "gallery_stream"
        )
        streamed = await delivery.stream(
            DeliveryRequest(
                broker="alpaca",
                clerk_id=CLERK_ID,
                operation=stream_op,
                path_params={"account_id": ACCOUNT},
                routing_epoch=EPOCH,
                binding_generation=3,
            )
        )
        events = [event async for event in streamed.events]
        assert len(events) == 3
        assert events[0].identity["x-fleet-routing-epoch"] == str(EPOCH)
    finally:
        settings.DATA_PLANE_CONTROL_SECRET = original_secret
        settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL = original_allow
        fleet_settings.COORDINATOR_SERVICE_TOKEN = original


def test_no_catalog_operation_targets_an_internal_route() -> None:
    """The routing surface can never escape into /internal — no exception."""
    for adapter in production_provider_adapters().values():
        for operation in adapter.operations():
            assert not operation.agent_path_template.startswith("/internal"), (
                operation.operation_id
            )


@pytest.mark.parametrize("version", [1, 3])
def test_incompatible_protocol_versions_refuse_registration(
    control_dir: Path, clock: FrozenClock, version: int
) -> None:
    """The supported mixed-version matrix: only the current protocol routes."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        volume_root = control_dir.parent / "v" / str(version)
        volume_root.mkdir(parents=True, exist_ok=True)
        provisioned = service.provision_clerk(
            broker="alpaca",
            display_label=f"v{version}",
            volume_root=volume_root,
            attestation_id=f"vol-v{version}",
        )
        with pytest.raises(FleetControlError) as raised:
            service.register_agent_session(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                fleet_protocol_version=version,
            )
        assert raised.value.reason == "fleet_protocol_incompatible"
    finally:
        service.close()


def test_the_catalog_stays_the_single_routing_contract() -> None:
    """Every operation id is unique and every public route is clerk-scoped."""
    adapter = AlpacaProviderAdapter()
    operations = adapter.operations()
    assert len({op.operation_id for op in operations}) == len(operations)
    assert all(op.path_template.startswith("/") for op in operations)
    assert any(op.capability.value == "custody_command" for op in operations)


def test_existing_wildcards_do_not_shadow_the_clerk_surface() -> None:
    """PRD §10.2: a clerk-scoped request resolves to the fleet route, never
    to an unscoped wildcard agent route mounted earlier."""
    import json
    import os
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as control:
        service_root = Path(__file__).resolve().parents[3]
        inherited = [
            entry
            for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
            if entry and Path(entry).resolve() != service_root / "tests"
        ]
        environment = {
            **os.environ,
            "FLEET_ROLE": "combined",
            "FLEET_CONTROL_DIR": control,
            "PYTHONPATH": os.pathsep.join([str(service_root), *inherited]),
        }
        probe = (
            "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
            "import json; from app.main import app; "
            "import starlette.routing as sr; "
            "scope = {'type': 'http', 'method': 'GET', 'path': "
            "'/api/brokers/alpaca/clerks/clrk_probe0000000000000000a/account', "
            "'headers': [], 'query_string': b''}; "
            "matches = [getattr(route, 'name', '') for route in app.routes "
            "if isinstance(route, sr.Route) and "
            "route.matches(scope)[0] == sr.Match.FULL]; "
            "sys.stdout.write(json.dumps(matches[:3]) + '\\n')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=environment,
            cwd=service_root,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        matched = json.loads(completed.stdout.strip().splitlines()[-1])
        assert matched, "the clerk-scoped request must resolve"
        assert matched[0].startswith("fleet_"), matched


def test_a_broker_without_the_operation_refuses_the_capability(
    tmp_path: Path,
) -> None:
    """A broker that does not declare the route's operation refuses it —
    no cross-provider silent servicing."""
    from app.routers.broker_clerks import _lookup_operation
    from tests.broker.fleet.conftest import fake_alpha

    alpaca_ops = production_provider_adapters()["alpaca"].operations()
    custody = next(op for op in alpaca_ops if op.operation_id == "custody_reconcile")
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=tmp_path / "capability-check"),
        provider_adapters={"fake_alpha": fake_alpha()},
    )
    try:
        with pytest.raises(FleetControlError) as raised:
            _lookup_operation("fake_alpha", custody, service)
        assert raised.value.reason == "broker_clerk_capability_unavailable"
    finally:
        service.close()


async def test_the_envelope_generation_fence_and_target_are_enforced(
    fleet: _Fleet,
) -> None:
    """No generation fence, wrong target or malformed fence → 422, never 500."""
    actions = f"{fleet.base}/accounts/{ACCOUNT}/bots/sid-9/actions"
    async with fleet.client() as client:
        no_generation = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "g-1",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "g-1",
                },
            },
        )
        assert no_generation.status_code == 422
        assert "expected_effective_binding_generation" in no_generation.json()[
            "message"
        ]

        malformed = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "g-2",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "g-2",
                    "expected_effective_binding_generation": "not-a-generation",
                },
            },
        )
        assert malformed.status_code == 422
        assert malformed.json()["reason"] == "command_envelope_invalid"

        wrong_target = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "g-3",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "g-3",
                    "expected_effective_binding_generation": 3,
                    "target": {
                        "account_id": "00000000-0000-0000-0000-000000000000",
                        "entity_id": "sid-9",
                    },
                },
            },
        )
        assert wrong_target.status_code == 422
        assert "target names account" in wrong_target.json()["message"]

        wrong_entity = await client.post(
            actions,
            json={
                "action_id": "arm",
                "idempotency_key": "g-4",
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": "g-4",
                    "expected_effective_binding_generation": 3,
                    "target": {"account_id": ACCOUNT, "entity_id": "sid-other"},
                },
            },
        )
        assert wrong_entity.status_code == 422


async def test_a_settled_attempt_never_redispatches(fleet: _Fleet) -> None:
    """D11: a retry of a settled key reconciles; it never resubmits."""
    actions = f"{fleet.base}/accounts/{ACCOUNT}/bots/sid-9/actions"
    payload = {
        "action_id": "arm",
        "idempotency_key": "dk-once",
        "command_context": {
            "capability": "bot_action",
            "idempotency_key": "dk-once",
            "expected_effective_binding_generation": 3,
        },
    }
    async with fleet.client() as client:
        first = await client.post(actions, json=payload)
        assert first.status_code == 200
        retry = await client.post(actions, json=payload)
        assert retry.status_code == 409
        body = retry.json()
        assert body["reason"] == "clerk_routing_attempt_conflict"
        assert fleet.minted_receipts[-1] in body["message"]
        assert "reconcile" in body["message"].lower()
        assert "never resubmit" in body["next_step"].lower()
    receipts = [
        receipt
        for receipt in fleet.lane.service._store.list_routing_receipts(
            clerk_id=fleet.lane.clerk_id
        )
        if receipt.idempotency_key == "dk-once"
    ]
    assert len(receipts) == 1, "the retry must not mint a second attempt"


async def test_one_shot_commands_get_distinct_attempt_identities(
    fleet: _Fleet,
) -> None:
    """Repeated one-shot operations never share a routing receipt."""
    profiles = f"{fleet.base}/configuration/profiles"
    async with fleet.client() as client:
        for _ in range(2):
            created = await client.post(
                profiles,
                json={
                    "name": "p",
                    "command_context": {"capability": "configuration_manage"},
                },
            )
            assert created.status_code == 200, created.text
    receipts = fleet.lane.service._store.list_routing_receipts(
        clerk_id=fleet.lane.clerk_id
    )
    one_shot = [r for r in receipts if r.idempotency_key.startswith("oneshot-")]
    assert len(one_shot) == 2
    assert len({r.idempotency_key for r in one_shot}) == 2


async def test_a_stale_pin_is_refused_before_the_agent_handler(fleet: _Fleet) -> None:
    """A mutation pinned to a stale epoch never reaches the handler."""
    async with httpx.AsyncClient(base_url=fleet.agent.base_url, timeout=5.0) as client:
        stale = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCOUNT}/bots/sid-9/actions",
            headers={
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": fleet.lane.clerk_id,
                "X-Fleet-Routing-Epoch": str(int(fleet.identity["routing_epoch"]) + 99),
                "X-Fleet-Binding-Generation": "3",
                COORDINATOR_TOKEN_HEADER: "irrelevant-here",
            },
            json={"action_id": "arm", "idempotency_key": "never"},
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "clerk_identity_mismatch"
        # A read pinned the same stale way stays servable — the echo and the
        # coordinator's checks own read provenance.
        read = await client.get(
            "/api/brokers/alpaca/account",
            headers={
                "X-Fleet-Broker": "alpaca",
                "X-Fleet-Clerk-Id": fleet.lane.clerk_id,
                "X-Fleet-Routing-Epoch": "999",
            },
        )
        assert read.status_code == 200
        assert read.headers["x-fleet-routing-epoch"] == str(
            fleet.identity["routing_epoch"]
        )


async def test_a_lane_serving_a_different_epoch_fails_the_coordinators_echo_check(
    tmp_path: Path,
) -> None:
    """FR-076: the echo is a check, not a formality.

    Reads pass the agent-side pin gate (``_pin_mismatch`` compares epoch only
    for mutations), so a lane serving a stale epoch answers 200 — and the
    coordinator's ``verify_identity_echo`` is the only thing standing
    between that answer and the caller.
    """
    fleet = _Fleet(tmp_path)
    try:
        # The lane re-registered and moved on; the coordinator still pins the
        # epoch its registry recorded.
        fleet.identity["routing_epoch"] = int(fleet.identity["routing_epoch"]) + 1
        async with fleet.client() as client:
            response = await client.get(f"{fleet.base}/account")
        assert response.status_code == 409
        assert response.json()["reason"] == "clerk_identity_mismatch"
    finally:
        fleet.stop()


async def test_a_refused_stream_keeps_its_refusal_status(fleet: _Fleet) -> None:
    """A provider 404 on a stream is that 404, not an empty 200 stream."""
    from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
    from app.broker.fleet.delivery import HttpLaneDelivery

    live = next(op for op in ALPACA_OPERATIONS if op.operation_id == "bot_live_stream")
    delivery = HttpLaneDelivery(
        base_url=fleet.agent.base_url, coordinator_service_token=COORDINATOR_TOKEN
    )
    result = await delivery.stream(
        DeliveryRequest(
            broker="alpaca",
            clerk_id=fleet.lane.clerk_id,
            operation=live,
            path_params={"account_id": ACCOUNT, "sid": "sid-9"},
            routing_epoch=int(fleet.identity["routing_epoch"]),
            binding_generation=3,
        )
    )
    assert result.status_code == 404
    assert result.error_body is not None


def test_an_older_adapter_build_refuses_registration(
    control_dir: Path, clock: FrozenClock
) -> None:
    """A mixed build (older agent, newer coordinator catalog) refuses."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        volume_root = control_dir.parent / "volumes" / "oldbuild"
        volume_root.mkdir(parents=True, exist_ok=True)
        provisioned = service.provision_clerk(
            broker="alpaca",
            display_label="old build",
            volume_root=volume_root,
            attestation_id="vol-oldbuild",
        )
        with pytest.raises(FleetControlError) as raised:
            service.register_agent_session(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                fleet_protocol_version=FLEET_PROTOCOL_VERSION,
                adapter_version="alpaca-fleet.2",
            )
        assert raised.value.reason == "fleet_protocol_incompatible"
        assert "Upgrade the agent" in raised.value.message
    finally:
        service.close()
