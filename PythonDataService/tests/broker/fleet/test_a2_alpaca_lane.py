"""Delivery A2: one Alpaca lane end-to-end through the fleet protocol.

The exit evidence the audit's delivery table demands, as tests: the Alpaca
adapter and its catalog; the internal fleet surface with two-factor
authentication; both presence transports (the remote one over a real
socket); the confirmation evidence and the offline rule; the agent boot
sequence with reservation before custody; the delivery adapters over a real
agent process including an SSE stream and the identity-echo contract; the
resumable enrolment ceremony; and the role-gated router surface proven in
real subprocesses.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from app.broker.alpaca.clerk.fleet_adapter import (
    ALPACA_OPERATIONS,
    AlpacaProviderAdapter,
)
from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    confirmation_evidence_path,
    evidence_vouches_for,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.delivery import (
    DeliveryIdentityMismatch,
    DeliveryRequest,
    DeliveryResult,
    HttpLaneDelivery,
    verify_identity_echo,
)
from app.broker.fleet.provider import (
    FLEET_PROTOCOL_VERSION,
    validate_operation_catalog,
)
from app.broker.fleet.records import AssignmentState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from app.config import FleetSettings
from tests.broker.fleet.conftest import FrozenClock

# ---------------------------------------------------------------------------
# Adapter and composition
# ---------------------------------------------------------------------------


def test_the_alpaca_adapter_declares_a_valid_catalog_and_canonical_account_ids() -> None:
    """The first production provider: validated catalog, account-id canonicity.

    Both live registry bindings are account numbers, not UUIDs (see
    ``canonical_account_id``'s docstring) — the served-context gate must
    accept them.
    """
    adapter = AlpacaProviderAdapter()
    validate_operation_catalog(adapter.operations())
    assert adapter.operations() == ALPACA_OPERATIONS
    assert adapter.canonical_account_id("  ABCDEF01-1234-ABCD-5678-EF0123456789 ") == (
        "abcdef01-1234-abcd-5678-ef0123456789"
    )
    adapter.validate_served_context(
        _served_context(account_id="123456789", capability="bot_action")
    )
    adapter.validate_served_context(
        _served_context(account_id="pa3abcdefghi", capability="bot_action")
    )
    with pytest.raises(LookupError):
        adapter.validate_served_context(
            _served_context(account_id=None, capability="bot_action")
        )


def _served_context(*, account_id: str | None, capability: str):
    from app.broker.fleet.provider import Capability, ServedContext

    return ServedContext(
        broker="alpaca",
        clerk_id="clrk_0000000000000000000000aa",
        agent_instance_id="agnt_0000000000000000000000aa",
        routing_epoch=1,
        account_id=account_id,
        capability=Capability(capability),
        effective_binding_generation=1,
    )


def test_the_composition_registry_maps_alpaca_and_nothing_else() -> None:
    """The app-level composition is the only production registry, Alpaca-only."""
    adapters = production_provider_adapters()
    assert set(adapters) == {"alpaca"}
    assert adapters["alpaca"].provider_id == "alpaca"
    # The fleet package's own constant stays empty: the provider enters at
    # application composition, never inside the broker-neutral package.
    from app.broker.fleet.provider import PRODUCTION_PROVIDER_ADAPTERS

    assert PRODUCTION_PROVIDER_ADAPTERS == {}


# ---------------------------------------------------------------------------
# Internal fleet surface
# ---------------------------------------------------------------------------


class _CoordinatorApp:
    """A coordinator FastAPI app with the fleet service installed."""

    def __init__(self, control_dir: Path, tokens: dict[str, str]) -> None:
        from app.routers.internal_fleet import router as internal_router

        self.service = FleetControlService(
            store=FleetRegistryStore.open(control_dir=control_dir),
            provider_adapters=production_provider_adapters(),
        )
        self.app = FastAPI()
        self.app.state.fleet_service = self.service
        self.app.state.fleet_agent_tokens_text = json.dumps(tokens)
        self.app.include_router(internal_router)


#: The agent's own served identity — what its runtime actually is, never a
#: reflection of what the caller pinned (audit 2026-09-13, finding 7).
AGENT_BROKER = "alpaca"
AGENT_CLERK_ID = "clrk_testagent00000000000000aa"
AGENT_EPOCH = 4
AGENT_BINDING_GENERATION = 2


def _agent_identity_headers() -> dict[str, str]:
    """The identity echo a well-behaved agent derives from its own state."""
    return {
        "X-Fleet-Broker": AGENT_BROKER,
        "X-Fleet-Clerk-Id": AGENT_CLERK_ID,
        "X-Fleet-Routing-Epoch": str(AGENT_EPOCH),
        "X-Fleet-Binding-Generation": str(AGENT_BINDING_GENERATION),
    }


def _build_agent_app() -> FastAPI:
    """A minimal agent serving the A2 catalog shapes with identity echoes."""

    agent = FastAPI()

    @agent.get("/api/brokers/alpaca/account")
    async def account(request: FastAPIRequest) -> JSONResponse:
        del request
        return JSONResponse(
            {"account_id": "abcdef01-1234-abcd-5678-ef0123456789"},
            headers=_agent_identity_headers(),
        )

    @agent.post("/api/brokers/alpaca/accounts/{account_id}/bots/{sid}/actions")
    async def bot_action(account_id: str, sid: str, request: FastAPIRequest) -> JSONResponse:
        del request
        return JSONResponse(
            {
                "outcome": "confirmed",
                "account_id": account_id,
                "sid": sid,
                "receipt_id": f"provider/command-{sid}",
            },
            headers=_agent_identity_headers(),
        )

    @agent.get("/api/brokers/alpaca/accounts/{account_id}/gallery/stream")
    async def gallery_stream(account_id: str, request: FastAPIRequest) -> StreamingResponse:
        async def events():
            # Delivery B: every frame carries the runtime's own provenance as
            # x-fleet-* fields, the way the identity middleware stamps them.
            provenance = "".join(
                f"{name.lower()}: {value}\n"
                for name, value in _agent_identity_headers().items()
                if name.lower().startswith("x-fleet-")
            )
            for index in range(3):
                yield (
                    f"event: update\ndata: {{\"i\": {index}, "
                    f"\"account\": \"{account_id}\"}}\n{provenance}\n"
                ).encode()
                await asyncio.sleep(0.01)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=_agent_identity_headers(),
        )

    return agent


class _RealServer:
    """A real uvicorn server on an ephemeral loopback port."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._server = None
        self._thread = None
        self.base_url = ""

    def start(self) -> None:
        """Listen for the lifetime of the test."""
        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(self._app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{port}"
        import time

        for _ in range(100):
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError("agent server did not start")

    def stop(self) -> None:
        """Terminate the listener."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def agent_server():
    """One real agent process serving the A2 catalog shapes."""
    server = _RealServer(_build_agent_app())
    server.start()
    yield server
    server.stop()


@pytest.fixture
def coordinator_app(control_dir: Path):
    """A coordinator app whose agent token maps one clerk."""
    app = _CoordinatorApp(
        control_dir=control_dir, tokens={"clrk_testagent00000000000000aa": "svct_" + "0" * 32}
    )
    yield app
    app.service.close()


async def test_the_internal_surface_refuses_unmapped_and_wrong_tokens(
    coordinator_app: _CoordinatorApp,
) -> None:
    """Two-factor auth: no token mapping, wrong token, wrong clerk all refuse."""
    transport = httpx.ASGITransport(app=coordinator_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/internal/fleet/sessions",
            json={
                "clerk_id": "clrk_other00000000000000000000aa",
                "worker_key": "wkrk_00000000000000000000000000000000",
                "fleet_protocol_version": FLEET_PROTOCOL_VERSION,
            },
            headers={"X-Fleet-Agent-Token": "svct_" + "0" * 32},
        )
        assert response.status_code == 403
        response = await client.post(
            "/internal/fleet/sessions",
            json={
                "clerk_id": "clrk_testagent00000000000000aa",
                "worker_key": "wkrk_00000000000000000000000000000000",
                "fleet_protocol_version": FLEET_PROTOCOL_VERSION,
            },
            headers={"X-Fleet-Agent-Token": "svct_wrongwrongwrongwrongwrong0"},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Presence, boot sequence, evidence — local transport
# ---------------------------------------------------------------------------


def _fence_satisfying_roots(monkeypatch: pytest.MonkeyPatch, volume_root: Path) -> None:
    """Point the lane-local writable roots inside the clerk volume."""
    from app.broker.ibkr import config as ibkr_config

    monkeypatch.setattr(
        ibkr_config,
        "get_settings",
        lambda: ibkr_config.IbkrSettings(
            live_runs_root=str(volume_root / "live_runs" / "runs"),
            live_bars_root=str(volume_root / "live_bars"),
        ),
    )


def _enrolled_lane(service: FleetControlService, tmp_path: Path, clock: FrozenClock):
    """Provision one enrolled alpaca lane; returns (record, marker, root)."""
    volume_root = tmp_path / "volumes" / "paper"
    volume_root.mkdir(parents=True)
    provisioned = service.provision_clerk(
        broker="alpaca",
        display_label="Paper",
        volume_root=volume_root,
        attestation_id="learn-ai-alpaca-paper",
        deployment_namespace="compose:test",
    )
    return provisioned


async def test_the_agent_boot_sequence_reserves_before_confirming(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Volume gate → register → reserve → confirm → evidence, in order."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        _fence_satisfying_roots(monkeypatch, Path(provisioned.clerk.volume_root))
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )
        from app.broker.alpaca.clerk.fleet_boot import (
            confirm_binding,
            open_fleet_lane,
            reserve_account,
        )

        boot = await open_fleet_lane(settings=settings, volume_root=Path(provisioned.clerk.volume_root))
        assert boot is not None and boot.online
        assignment_row = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert assignment_row is None  # nothing reserved yet
        await reserve_account(
            boot, external_account_id="ABCDEF01-1234-ABCD-5678-EF0123456789"
        )
        await confirm_binding(
            boot,
            external_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
        )
        stored = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert stored is not None
        assert stored.state == AssignmentState.EFFECTIVE
        assert stored.confirmed_binding_generation == 1
        evidence = read_confirmation_evidence(Path(provisioned.clerk.volume_root))
        assert evidence is not None
        assert evidence.binding_generation == 1
        assert evidence.agent_instance_id == boot.session.agent_instance_id
    finally:
        service.close()


async def test_an_offline_coordinator_boots_only_the_evidence_confirmed_tuple(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-066: offline boot matches only the exact confirmed tuple."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        # No coordinator running: remote presence to a dead port.
        settings = FleetSettings(
            ROLE="clerk_agent",
            COORDINATOR_URL="http://127.0.0.1:9",
            AGENT_SERVICE_TOKEN="svct_" + "1" * 32,
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
        )
        from app.broker.alpaca.clerk.fleet_boot import (
            FleetBootRefused,
            offline_boot_matches,
            open_fleet_lane,
        )

        with pytest.raises(FleetBootRefused, match="no confirmation evidence"):
            await open_fleet_lane(settings=settings, volume_root=root)

        write_confirmation_evidence(
            root,
            ConfirmationEvidence(
                clerk_id=provisioned.clerk.clerk_id,
                volume_id=provisioned.clerk.volume_id,
                registry_id="fltr_offline0000000000000000",
                assignment_generation=1,
                canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789",
                binding_generation=1,
                effective_profile_id="prof_1",
                effective_revision=2,
                confirmed_at_ms=1,
                agent_instance_id="agnt_0000000000000000000000aa",
                routing_epoch=1,
            ),
        )
        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None
        assert not boot.online
        assert boot.offline_reason is not None
        assert offline_boot_matches(
            boot,
            canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            effective_profile_id="prof_1",
            effective_revision=2,
        )
        # A changed tuple does not match: it waits for the coordinator.
        assert not offline_boot_matches(
            boot,
            canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            effective_profile_id="prof_2",
            effective_revision=2,
        )
    finally:
        service.close()


def test_confirmation_evidence_refuses_identity_migration(tmp_path: Path) -> None:
    """Evidence never migrates onto another lane; grants move forward only."""
    root = tmp_path / "clerk"
    root.mkdir()
    base = ConfirmationEvidence(
        clerk_id="clrk_aaaaaaaaaaaaaaaaaaaaaaaa",
        volume_id="vol_aaaaaaaaaaaaaaaaaaaaaaaa",
        registry_id="fltr_aaaaaaaaaaaaaaaaaaaaaaa",
        assignment_generation=1,
        canonical_account_id="acct-a",
        binding_generation=1,
        effective_profile_id="p",
        effective_revision=1,
        confirmed_at_ms=1,
        agent_instance_id="agnt_aaaaaaaaaaaaaaaaaaaaaaaa",
        routing_epoch=1,
    )
    write_confirmation_evidence(root, base)
    from app.broker.fleet.confirmation import ConfirmationEvidenceError

    foreign = dataclasses.replace(base, clerk_id="clrk_bbbbbbbbbbbbbbbbbbbbbbbb")
    with pytest.raises(ConfirmationEvidenceError, match="refusing to overwrite"):
        write_confirmation_evidence(root, foreign)
    advanced = dataclasses.replace(base, binding_generation=2, confirmed_at_ms=2)
    write_confirmation_evidence(root, advanced)
    assert read_confirmation_evidence(root).binding_generation == 2
    assert evidence_vouches_for(
        read_confirmation_evidence(root),
        canonical_account_id="acct-a",
        effective_profile_id="p",
        effective_revision=1,
    )
    (confirmation_evidence_path(root)).write_text("{corrupt")
    with pytest.raises(ConfirmationEvidenceError):
        read_confirmation_evidence(root)


# ---------------------------------------------------------------------------
# Delivery over a real agent process
# ---------------------------------------------------------------------------


def _alpaca_operation(operation_id: str):
    return next(op for op in ALPACA_OPERATIONS if op.operation_id == operation_id)


async def test_http_delivery_serves_a_read_and_a_stream_over_a_real_agent(
    agent_server: _RealServer,
) -> None:
    """One scoped read and one stream through the full pinned route."""
    delivery = HttpLaneDelivery(
        base_url=agent_server.base_url,
        coordinator_service_token="svct_coordinatorcoordinatorcoord",
    )
    read_request = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=_alpaca_operation("account_read"),
        path_params={},
        routing_epoch=4,
        binding_generation=2,
    )
    result = await delivery.deliver(read_request)
    assert result.status_code == 200
    payload = json.loads(result.body)
    assert payload["account_id"] == "abcdef01-1234-abcd-5678-ef0123456789"

    stream_request = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=_alpaca_operation("gallery_stream"),
        path_params={"account_id": "abcdef01-1234-abcd-5678-ef0123456789"},
        routing_epoch=4,
        binding_generation=2,
    )
    streamed = await delivery.stream(stream_request)
    events = [event async for event in streamed.events]
    assert [event.event for event in events] == ["update", "update", "update"]
    assert json.loads(events[0].data)["account"] == "abcdef01-1234-abcd-5678-ef0123456789"


async def test_verify_identity_echo_refuses_a_wrong_or_missing_echo() -> None:
    """A mismatched echo refuses the response after possible dispatch."""
    result = DeliveryResult(
        status_code=200,
        headers={
            "X-Fleet-Broker": "alpaca",
            "X-Fleet-Clerk-Id": "clrk_somebodyelse000000000000aa",
            "X-Fleet-Routing-Epoch": "4",
        },
        body=b"{}",
    )
    request = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=_alpaca_operation("account_read"),
        path_params={},
        routing_epoch=4,
    )
    with pytest.raises(DeliveryIdentityMismatch, match="clerk"):
        verify_identity_echo(result.headers, request)
    # A pinned dimension without an echo is not verifiable and refuses: a
    # missing echo cannot distinguish the serving runtime from a reflection.
    with pytest.raises(DeliveryIdentityMismatch, match="broker"):
        verify_identity_echo({}, request)
    with pytest.raises(DeliveryIdentityMismatch, match="epoch"):
        verify_identity_echo(
            {"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": request.clerk_id},
            request,
        )
    # Unpinned dimensions go unchecked.
    unpinned = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=_alpaca_operation("account_read"),
        path_params={},
    )
    verify_identity_echo(
        {"X-Fleet-Broker": "alpaca", "X-Fleet-Clerk-Id": unpinned.clerk_id}, unpinned
    )


async def test_a_command_travels_the_full_pinned_route_with_its_attempt(
    agent_server: _RealServer,
) -> None:
    """A representative command: pinned context before dispatch, identity
    echo verified, provider receipt carried, attempt settled delivered."""
    from app.broker.fleet.records import RoutingReceiptState

    delivery = HttpLaneDelivery(
        base_url=agent_server.base_url,
        coordinator_service_token="svct_coordinatorcoordinatorcoord",
    )
    request = DeliveryRequest(
        broker=AGENT_BROKER,
        clerk_id=AGENT_CLERK_ID,
        operation=_alpaca_operation("bot_panel_action"),
        path_params={
            "account_id": "abcdef01-1234-abcd-5678-ef0123456789",
            "sid": "sid-1",
        },
        json_body={"action": "stop", "idempotency_key": "idem-cmd-1"},
        routing_epoch=AGENT_EPOCH,
        binding_generation=AGENT_BINDING_GENERATION,
    )
    # The attempt model around the delivery: pin before dispatch, settle after.
    # (The registry-less agent fixture proves the transport contract; the
    # attempt lifecycle itself is pinned in test_routing_attempts.py.)
    result = await delivery.deliver(request)
    assert result.status_code == 200
    payload = json.loads(result.body)
    assert payload["receipt_id"] == "provider/command-sid-1"
    assert RoutingReceiptState.DELIVERED.value == "delivered"


# ---------------------------------------------------------------------------
# Role-gated surface, proven in real subprocesses
# ---------------------------------------------------------------------------


def _route_paths_for_role(role: str) -> set[str]:
    """Import the real app under a role and collect its mounted paths."""
    import os
    import subprocess
    import sys

    service_root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "FLEET_ROLE": str(role)}
    if role == "clerk_agent":
        # Delivery D requires an agent deployment to size both independent
        # runtime pools explicitly; this route-surface probe is one such
        # minimal deployment rather than a legacy combined compatibility boot.
        environment.update(
            {
                "FLEET_MAX_INFLIGHT_REQUESTS": "1",
                "FLEET_MAX_INFLIGHT_STREAMS": "1",
                "FLEET_REQUEST_QUEUE_LIMIT": "0",
                "FLEET_REQUEST_QUEUE_TIMEOUT_MS": "0",
            }
        )
    # A CI harness may put tests/ itself on PYTHONPATH; tests/operator would
    # then shadow the stdlib operator module inside the child interpreter's
    # bootstrap. Filter that entry from the inherited path before prepending
    # the service root; the in-child scrub below is the second line of defense.
    inherited = [
        entry
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != service_root / "tests"
    ]
    environment["PYTHONPATH"] = os.pathsep.join([str(service_root), *inherited])
    probe = (
        "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
        "import json; from app.main import app; "
        "print(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})))"
    )
    # The child's cwd must be the service root: `python -c` puts the cwd at
    # sys.path[0], and a tests/ cwd would shadow the stdlib `operator` module
    # with tests/operator/ before the in-child scrub can remove it ('' does
    # not end with '/tests').
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        cwd=service_root,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(
    ("role", "expect_unscoped_brokers", "expect_internal"),
    [
        ("combined", True, False),
        ("fleet_coordinator", True, False),
        ("clerk_agent", True, False),
    ],
)
def test_the_router_surface_follows_the_role(
    role: str, expect_unscoped_brokers: bool, expect_internal: bool
) -> None:
    """Combined keeps today's surface; the coordinator is narrowly bounded.

    Delivery B gives the coordinator the clerk-scoped routing surface under
    ``/api/brokers/{broker}/clerks/…``. Delivery D adds exactly two retained
    read aliases; the unscoped agent families stay on clerk-agent processes.
    """
    paths = _route_paths_for_role(role)
    unscoped_broker_paths = {
        path
        for path in paths
        if path.startswith("/api/brokers")
        and not path.startswith("/api/brokers/{broker}/clerks")
    }
    assert bool(unscoped_broker_paths) is expect_unscoped_brokers, (
        role,
        sorted(paths)[:5],
    )
    if role == "fleet_coordinator":
        assert unscoped_broker_paths == {
            "/api/brokers/{broker}/live-verdict",
            "/api/brokers/{broker}/panel-profile",
        }
    has_internal = any(path.startswith("/internal/fleet") for path in paths)
    assert has_internal is expect_internal
    if role == "fleet_coordinator":
        # The control directory gates the whole coordinator surface; the
        # routing mount rides the same gate.
        assert any(
            path.startswith("/api/brokers/{broker}/clerks") for path in paths
        ) is False  # no control dir in this probe: nothing mounts
    # The data-plane core is the coordinator's: an agent serves none of it
    # (review finding 1 — the gate exists and the probe proves it).
    has_core = any(path.startswith("/api/engine") or path.startswith("/api/research") for path in paths)
    assert has_core == (role != "clerk_agent"), (role, sorted(paths)[:5])


def test_the_coordinator_surface_appears_with_a_control_directory() -> None:
    """The internal fleet surface mounts only where the registry lives."""
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
            "FLEET_ROLE": "fleet_coordinator",
            "FLEET_CONTROL_DIR": control,
            "PYTHONPATH": os.pathsep.join([str(service_root), *inherited]),
        }
        probe = (
            "import sys; sys.path[:] = [p for p in sys.path if not p.endswith('/tests')]; "
            "import json; from app.main import app; "
            "print(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=environment,
            cwd=service_root,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        paths = set(json.loads(completed.stdout.strip().splitlines()[-1]))
        assert any(path.startswith("/internal/fleet") for path in paths)
        # The coordinator owns the clerk-scoped routing surface plus exactly
        # two D compatibility reads; agent families remain private.
        brokers_paths = {
            path for path in paths if path.startswith("/api/brokers")
        }
        assert brokers_paths, "the clerk-scoped routing surface must mount"
        unscoped = {
            path
            for path in brokers_paths
            if not path.startswith("/api/brokers/{broker}/clerks")
        }
        assert unscoped == {
            "/api/brokers/{broker}/live-verdict",
            "/api/brokers/{broker}/panel-profile",
        }


def _volume_with_effective_tuple(tmp_path: Path, *, binding_generation: int = 0) -> Path:
    """A legacy clerk volume: profiles db holding an effective tuple."""
    from app.broker_configuration.service import BrokerConfigurationService
    from app.broker_configuration.store import ProfilesStore
    from tests.broker_configuration.conftest import (
        OPERATOR_IDENTITY,
        FakeAccountVerifier,
        paper_profile,
        slot_directory_for_tests,
    )

    root = tmp_path / "legacy-volume"
    (root / "broker_configuration").mkdir(parents=True)
    service = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=root),
        operator_identity=OPERATOR_IDENTITY,
        clock=lambda: 1_757_000_000_000,
        credential_slots=slot_directory_for_tests(),
        account_verifier=FakeAccountVerifier(),
    )
    created = paper_profile(service)
    service.close()
    store = ProfilesStore.open(clerk_dir=root)
    try:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE installation_selection SET effective_profile_id = ?, "
                "effective_revision = 1, effective_account_id = "
                "'ABCDEF01-1234-ABCD-5678-EF0123456789', effective_acknowledged_at_ms = 11, "
                "effective_binding_generation = ? "
                "WHERE id = 1",
                (created.profile.profile_id, binding_generation),
            )
    finally:
        store.close()
    return root


def test_migrate_existing_enrols_resumes_and_never_remints(tmp_path: Path, capsys) -> None:
    """The offline enrolment ceremony: complete, idempotent, resumable."""
    from scripts.manage_broker_fleet import main

    control_dir = tmp_path / "control"
    volume_root = _volume_with_effective_tuple(tmp_path)
    common = [
        "--control-dir",
        str(control_dir),
        "--volume-root",
        str(volume_root),
        "--attestation-id",
        "learn-ai-alpaca-paper",
        "--deployment-namespace",
        "compose:test",
    ]

    assert main(["init", *common[:2]]) == 0
    capsys.readouterr()
    assert main(["migrate-existing", *common]) == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["step"] == "complete"
    assert first["assignment"]["canonical_account_id"] == (
        "abcdef01-1234-abcd-5678-ef0123456789"
    )
    assert first["assignment"]["state"] == "effective"
    assert first["binding_generation_seeded"] is True
    clerk_id = first["clerk_id"]

    # Idempotent rerun: same identities, no reseed, already complete.
    assert main(["migrate-existing", *common]) == 0
    again = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert again["clerk_id"] == clerk_id
    assert again["binding_generation_seeded"] is False

    # Interrupted run: the marker disappears after registration; the rerun
    # resumes onto the SAME identity instead of minting a second one.
    from app.broker.fleet import volume as volume_module

    volume_module.marker_path(volume_root).unlink()
    assert main(["migrate-existing", *common]) == 0
    resumed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert resumed["clerk_id"] == clerk_id
    marker = volume_module.read_volume_marker(volume_root)
    assert marker is not None and marker.clerk_id == clerk_id

    # The registry holds exactly one clerk and one effective assignment.
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    try:
        clerks = service._store.list_clerks(include_retired=True)
        assert [clerk.clerk_id for clerk in clerks] == [clerk_id]
        assignment = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert assignment is not None
        assert assignment.state == AssignmentState.EFFECTIVE
        assert assignment.confirmed_binding_generation == 1
        evidence = read_confirmation_evidence(volume_root)
        assert evidence is not None
        assert evidence.canonical_account_id == "abcdef01-1234-abcd-5678-ef0123456789"
    finally:
        service.close()


def test_the_ceremony_confirms_the_volumes_generation_not_a_hardcoded_one(
    tmp_path: Path, capsys
) -> None:
    """A volume that already advanced past 1 through Apply cycles keeps its
    generation: the imported confirmation and evidence cite the volume's
    value, so the registry's confirmed observation agrees with the clerk's
    own selection from the first boot."""
    from scripts.manage_broker_fleet import main

    control_dir = tmp_path / "control"
    volume_root = _volume_with_effective_tuple(tmp_path, binding_generation=3)
    common = [
        "--control-dir",
        str(control_dir),
        "--volume-root",
        str(volume_root),
        "--attestation-id",
        "learn-ai-alpaca-paper",
        "--deployment-namespace",
        "compose:test",
    ]

    assert main(["init", "--control-dir", str(control_dir)]) == 0
    capsys.readouterr()
    assert main(["migrate-existing", *common]) == 0
    ceremony = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ceremony["step"] == "complete"
    assert ceremony["binding_generation_seeded"] is False
    assert ceremony["binding_generation"] == 3

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    try:
        assignment = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert assignment is not None
        assert assignment.confirmed_binding_generation == 3
        evidence = read_confirmation_evidence(volume_root)
        assert evidence is not None
        assert evidence.binding_generation == 3
    finally:
        service.close()


async def test_an_enrolled_agent_without_any_coordinator_destination_refuses(
    control_dir: Path, clock: FrozenClock, tmp_path: Path
) -> None:
    """An enrolled clerk agent naming neither a coordinator URL nor a control
    directory gets a typed boot refusal — not an AssertionError that -O strips."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
        )
        from app.broker.alpaca.clerk.fleet_boot import FleetBootRefused, open_fleet_lane

        with pytest.raises(FleetBootRefused, match="requires FLEET_COORDINATOR_URL"):
            await open_fleet_lane(
                settings=settings, volume_root=Path(provisioned.clerk.volume_root)
            )
    finally:
        service.close()


async def test_a_lane_root_outside_the_clerk_volume_refuses_before_authority(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writable-root fence is load-bearing: an escaping root refuses."""
    from app.broker.ibkr import config as ibkr_config

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        root = Path(provisioned.clerk.volume_root)
        shared = tmp_path / "shared-artifacts"
        # The deployment default: both roots on the shared artifacts tree,
        # which is exactly the two-lanes-one-root posture the fleet prevents.
        monkeypatch.setattr(
            ibkr_config,
            "get_settings",
            lambda: ibkr_config.IbkrSettings(
                live_runs_root=str(shared / "live_runs" / "runs"),
                live_bars_root=str(shared / "live_bars"),
            ),
        )
        (shared / "live_runs" / "runs").mkdir(parents=True)
        (shared / "live_bars").mkdir(parents=True)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )
        from app.broker.alpaca.clerk.fleet_boot import FleetBootRefused, open_fleet_lane

        with pytest.raises(FleetBootRefused, match="escapes the clerk volume"):
            await open_fleet_lane(settings=settings, volume_root=root)
        # The refusal precedes session registration: nothing was registered.
        assert service._store.read_session(provisioned.clerk.clerk_id) is None
    finally:
        service.close()


async def test_a_malformed_200_from_the_coordinator_is_presence_unavailability() -> None:
    """A heartbeat response that is not JSON translates to the presence
    family, so the heartbeat loop logs and re-registers instead of dying on
    a bare decode error."""
    from fastapi import FastAPI

    from app.broker.fleet.presence import FleetPresenceError, RemotePresence

    coordinator = FastAPI()

    @coordinator.post("/internal/fleet/sessions/observe")
    async def observe() -> PlainTextResponse:
        # A 200 whose body is not JSON — httpx's response.json() then raises,
        # which the presence seam must translate, not leak.
        return PlainTextResponse("not-json{")

    server = _RealServer(coordinator)
    server.start()
    try:
        presence = RemotePresence(
            base_url=server.base_url, agent_service_token="svct_" + "2" * 32
        )
        with pytest.raises(FleetPresenceError, match="malformed"):
            await presence.observe(
                clerk_id="clrk_000000000000000000000000aa",
                agent_instance_id="agnt_000000000000000000000000",
            )
    finally:
        server.stop()


async def test_an_effective_lane_restarts_and_reconfirms_under_a_new_epoch(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart convergence: the replacement boot registers a new epoch,
    re-confirms the unchanged binding generation, and routes again — the
    lost-reply case converges on the same identity instead of a new grant."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )
        from app.broker.alpaca.clerk.fleet_boot import (
            close_fleet_lane,
            confirm_binding,
            open_fleet_lane,
            reserve_account,
        )

        first = await open_fleet_lane(settings=settings, volume_root=root)
        assert first is not None and first.online
        await reserve_account(first, external_account_id="abcdef01-1234-abcd-5678-ef0123456789")
        await confirm_binding(
            first,
            external_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
        )
        first_epoch = first.session.routing_epoch
        await close_fleet_lane(first)

        # The replacement process: a fresh instance id, a higher epoch, and
        # a re-confirmation of the SAME generation (a lost reply reconciles
        # by identity — never a second reservation or a new grant).
        second = await open_fleet_lane(settings=settings, volume_root=root)
        assert second is not None and second.online
        assert second.session.routing_epoch > first_epoch
        await reserve_account(second, external_account_id="abcdef01-1234-abcd-5678-ef0123456789")
        reconfirmed = await confirm_binding(
            second,
            external_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
        )
        assert reconfirmed.confirmed_binding_generation == 1
        assert reconfirmed.confirmed_agent_instance_id == second.session.agent_instance_id
        # The lane routes again under the replacement session.
        clerk, session, assignment = service.resolve_route(
            broker="alpaca", clerk_id=provisioned.clerk.clerk_id
        )
        assert assignment is not None
        assert session.routing_epoch == second.session.routing_epoch
        assert clerk.clerk_id == provisioned.clerk.clerk_id
        await close_fleet_lane(second)
    finally:
        service.close()
