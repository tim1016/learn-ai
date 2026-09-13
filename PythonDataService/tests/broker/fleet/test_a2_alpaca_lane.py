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
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as FastAPIRequest
from fastapi.responses import StreamingResponse

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


def test_the_alpaca_adapter_declares_a_valid_catalog_and_canonical_uuids() -> None:
    """The first production provider: validated catalog, UUID canonicity."""
    adapter = AlpacaProviderAdapter()
    validate_operation_catalog(adapter.operations())
    assert adapter.operations() == ALPACA_OPERATIONS
    assert adapter.canonical_account_id("  ABCDEF01-1234-ABCD-5678-EF0123456789 ") == (
        "abcdef01-1234-abcd-5678-ef0123456789"
    )
    with pytest.raises(LookupError):
        adapter.validate_served_context(
            _served_context(account_id="not-a-uuid", capability="bot_action")
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


def _agent_identity_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The identity echo a well-behaved agent derives from its own state."""
    return {
        "X-Fleet-Broker": "alpaca",
        "X-Fleet-Clerk-Id": headers.get("x-fleet-clerk-id", ""),
        "X-Fleet-Routing-Epoch": headers.get("x-fleet-routing-epoch", ""),
        "X-Fleet-Binding-Generation": headers.get("x-fleet-binding-generation", ""),
    }


def _build_agent_app() -> FastAPI:
    """A minimal agent serving the A2 catalog shapes with identity echoes."""

    agent = FastAPI()

    @agent.get("/api/brokers/alpaca/account")
    async def account(request: FastAPIRequest) -> dict[str, object]:
        return {
            "account_id": "abcdef01-1234-abcd-5678-ef0123456789",
            "_echo": _agent_identity_headers(request.headers),
        }

    @agent.get("/api/brokers/alpaca/accounts/{account_id}/gallery/stream")
    async def gallery_stream(account_id: str, request: FastAPIRequest) -> StreamingResponse:
        async def events():
            for index in range(3):
                yield f"event: update\ndata: {{\"i\": {index}, \"account\": \"{account_id}\"}}\n\n".encode()
                await asyncio.sleep(0.01)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers=_agent_identity_headers(request.headers),
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
        for _ in range(100):
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    return
            asyncio.sleep(0) if False else None
            import time

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
    control_dir: Path, clock: FrozenClock
) -> None:
    """Volume gate → register → reserve → confirm → evidence, in order."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
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

        boot = await open_fleet_lane(settings=settings, volume_root=provisioned.clerk.volume_root and Path(provisioned.clerk.volume_root))
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
    control_dir: Path, clock: FrozenClock, tmp_path: Path
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


async def test_a_wrong_identity_echo_is_an_uncertain_outcome(agent_server: _RealServer) -> None:
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
    # Unpinned dimensions are not checked; absent echoes pass.
    verify_identity_echo({"X-Fleet-Broker": "alpaca"}, request)
    verify_identity_echo({}, request)


# ---------------------------------------------------------------------------
# Role-gated surface, proven in real subprocesses
# ---------------------------------------------------------------------------


def _route_paths_for_role(role: str) -> set[str]:
    """Import the real app under a role and collect its mounted paths."""
    import os
    import subprocess
    import sys

    service_root = str(Path(__file__).resolve().parents[3])
    environment = {**os.environ, "FLEET_ROLE": role, "PYTHONPATH": service_root}
    probe = (
        "import json; from app.main import app; "
        "print(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(
    ("role", "expect_brokers", "expect_internal"),
    [
        ("combined", True, False),
        ("fleet_coordinator", False, False),
        ("clerk_agent", True, False),
    ],
)
def test_the_router_surface_follows_the_role(role: str, expect_brokers: bool, expect_internal: bool) -> None:
    """Combined keeps today's surface; the coordinator mounts no clerk routes."""
    paths = _route_paths_for_role(role)
    has_brokers = any(path.startswith("/api/brokers") for path in paths)
    assert has_brokers is expect_brokers, (role, sorted(paths)[:5])
    has_internal = any(path.startswith("/internal/fleet") for path in paths)
    assert has_internal is expect_internal


def test_the_coordinator_surface_appears_with_a_control_directory() -> None:
    """The internal fleet surface mounts only where the registry lives."""
    import os
    import subprocess
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as control:
        service_root = str(Path(__file__).resolve().parents[3])
        environment = {
            **os.environ,
            "FLEET_ROLE": "fleet_coordinator",
            "FLEET_CONTROL_DIR": control,
            "PYTHONPATH": service_root,
        }
        probe = (
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
        assert not any(path.startswith("/api/brokers") for path in paths)


def _volume_with_effective_tuple(tmp_path: Path) -> Path:
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
                "'ABCDEF01-1234-ABCD-5678-EF0123456789', effective_acknowledged_at_ms = 11 "
                "WHERE id = 1",
                (created.profile.profile_id,),
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
