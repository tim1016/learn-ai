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
import contextlib
import dataclasses
import functools
import json
import logging
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
    DeliveryContractViolation,
    DeliveryIdentityMismatch,
    DeliveryRequest,
    DeliveryResult,
    HttpLaneDelivery,
    verify_identity_echo,
)
from app.broker.fleet.errors import ClerkUnreachable
from app.broker.fleet.provider import (
    FLEET_PROTOCOL_VERSION,
    OperationReadiness,
    validate_operation_catalog,
)
from app.broker.fleet.records import (
    AssignmentState,
    ClerkSessionRecord,
    ProviderSummaryObservation,
    SummaryEndpointMode,
)
from app.broker.fleet.service import (
    DEFAULT_SESSION_STALE_AFTER_MS,
    FleetControlService,
)
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


def test_provider_summary_carries_the_two_facts_that_disambiguate_starting_and_degraded() -> (
    None
):
    """`starting` has two causes (never confirmed / superseded session), and
    `degraded` has two (corrupt registry / agent-reported) — service.py's
    ``_project_lifecycle`` and ``ClerkDescriptor.public_fields`` collapse both
    pairs to one wire label each. ``confirmed_by_current_session`` and
    ``multiple_effective_assignments`` are the only observation facts that
    still separate them, so the provider-authored summary must carry both
    through rather than dropping them at the adapter boundary."""
    adapter = AlpacaProviderAdapter()

    confirmed_by_stale_session = adapter.provider_summary(
        {
            "confirmed_account_id": "acct-1",
            "confirmed_binding_generation": 3,
            "confirmed_by_current_session": False,
            "multiple_effective_assignments": False,
        }
    )
    assert confirmed_by_stale_session["confirmed_by_current_session"] is False
    assert confirmed_by_stale_session["multiple_effective_assignments"] is False

    corrupt_registry = adapter.provider_summary(
        {
            "confirmed_account_id": "acct-1",
            "confirmed_binding_generation": None,
            "confirmed_by_current_session": False,
            "multiple_effective_assignments": True,
        }
    )
    assert corrupt_registry["multiple_effective_assignments"] is True
    assert corrupt_registry["confirmed_by_current_session"] is False


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


def test_live_verdict_declares_configuration_access_not_execution_readiness() -> None:
    """#2140: EXECUTION readiness would refuse routing to an unactivated lane
    before its refusal reason was ever reached (see ``resolve_route``'s
    docstring, ``app/broker/fleet/service.py``). Asserted directly against
    the declared operation, not indirectly through a request that happens to
    pass for an already-bound lane -- a passing request for a bound lane
    would still pass under ``EXECUTION`` too, and could not catch this
    readiness regressing."""
    assert _alpaca_operation("live_verdict").readiness is OperationReadiness.CONFIGURATION_ACCESS


async def test_live_verdict_stays_routable_for_an_activation_required_lane(
    control_dir: Path, clock: FrozenClock
) -> None:
    """The paper-lane-up-but-unactivated case the bug report names: routing
    must not refuse before the clerk's own ``ACTIVATION_REQUIRED`` refusal
    reason is reached (#2140)."""
    from app.broker.alpaca.clerk.active_authority import (
        ActiveClerkRuntime,
        ClerkStartupFailure,
    )
    from app.broker.alpaca.config import AlpacaSettings
    from app.services.alpaca_live_verdict import alpaca_live_verdict
    from tests.broker.fleet.conftest import provision_lane

    live_verdict = _alpaca_operation("live_verdict")
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    try:
        lane = provision_lane(
            service, broker="alpaca", label="activation-required", tmp_path=control_dir.parent
        )
        service.register_agent_session(
            fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key
        )
        # No reservation, no confirmation: the routing-registry analogue of
        # an unactivated lane -- no confirmed binding exists, exactly what
        # ACTIVATION_REQUIRED means at the clerk itself.
        with pytest.raises(ClerkUnreachable):
            service.resolve_route(broker="alpaca", clerk_id=lane.clerk_id)
        _clerk, session, assignment = service.resolve_route(
            broker="alpaca", clerk_id=lane.clerk_id, readiness=live_verdict.readiness,
        )
        assert assignment is None
        assert session.clerk_id == lane.clerk_id
    finally:
        service.close()

    # The verdict the now-reachable route would carry: the clerk's own
    # refusal reason code, not a generic fleet-routing refusal that
    # swallows it before the agent is ever asked.
    runtime = ActiveClerkRuntime(
        authority_kind="unavailable",
        account_id=None,
        startup_failure=ClerkStartupFailure(
            reason_code="ACTIVATION_REQUIRED",
            account_id=None,
            scope="ACCOUNT_CLERK",
            impact="No SQLite Clerk activation record exists.",
            recovery=(
                "Complete the supervised SQLite Clerk cutover and activation "
                "before starting Alpaca custody."
            ),
            observed_at_ms=clock(),
        ),
    )
    verdict = alpaca_live_verdict(
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
        runtime=runtime,
        now_ms=clock(),
    )
    assert verdict.clerk_refusal_reason_code == "ACTIVATION_REQUIRED"


# ---------------------------------------------------------------------------
# Internal fleet surface
# ---------------------------------------------------------------------------


class _CoordinatorApp:
    """A coordinator FastAPI app with the fleet service installed."""

    def __init__(self, control_dir: Path, tokens: dict[str, str]) -> None:
        from app.routers.internal_fleet import router as internal_router
        from app.utils.error_handlers import install_fleet_control_error_handler

        self.service = FleetControlService(
            store=FleetRegistryStore.open(control_dir=control_dir),
            provider_adapters=production_provider_adapters(),
        )
        self.app = FastAPI()
        self.app.state.fleet_service = self.service
        self.app.state.fleet_agent_tokens_text = json.dumps(tokens)
        self.app.include_router(internal_router)
        # Production-shaped: the coordinator's real app registers this
        # handler too (app.main), because the agent-token guard raises
        # outside any route-local try/except (#2067).
        install_fleet_control_error_handler(self.app)


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


def _boot_service_on_the_test_clock(monkeypatch: pytest.MonkeyPatch, clock: FrozenClock) -> None:
    """Give the lane's own ``LocalPresence`` service the test's frozen clock.

    ``open_fleet_lane`` builds its *own* ``FleetControlService`` over the same
    control directory, and that one defaults to wall time — so a lane booted
    here stamps ``last_seen_at_ms`` from a different clock than the one the
    assertions read staleness against, and no amount of ``clock.advance``
    could ever make its session look stale (or its heartbeat look landed).
    Only a test that shares one clock across both handles can tell "the beat
    arrived" from "the session was never stale to begin with".
    """
    from app.broker.alpaca.clerk import fleet_boot

    monkeypatch.setattr(
        fleet_boot, "FleetControlService", functools.partial(FleetControlService, clock=clock)
    )


def _directory_entry(service: FleetControlService, clerk_id: str) -> dict[str, object]:
    """The one public directory row this clerk projects."""
    return next(
        entry for entry in service.directory()["clerks"] if entry["clerk_id"] == clerk_id
    )


async def _await_beat_at(
    service: FleetControlService,
    clerk_id: str,
    clock: FrozenClock,
    *,
    reported_state: str,
    deadline_s: float = 5.0,
) -> ClerkSessionRecord:
    """Wait for an observation stamped at the current frozen instant.

    The stamp alone does not identify a beat: ``confirm_assignment`` touches
    the session row from inside its own transaction too, with
    ``last_seen_at_ms=now`` and ``reported_state="binding_confirmed"``
    (``app/broker/fleet/service.py``). A caller that confirms and then polls
    would read the *confirmation's* touch and return before a single beat had
    run, leaving the heartbeat assertions to pass with no heartbeat at all.

    So the expected ``reported_state`` is required, not optional — and a
    caller that confirms first must still advance the clock past the
    confirmation's stamp, because a confirmed lane beats the very state the
    confirmation just wrote.

    The deadline is 50x the 0.05 s interval these tests run the heartbeat at:
    a test that asserts on real asyncio scheduling flakes under a loaded box,
    so the poll is generous and the failure says what was actually seen
    rather than timing out somewhere further along.
    """
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + deadline_s
    while True:
        session = service._store.read_session(clerk_id)
        if (
            session is not None
            and session.last_seen_at_ms == clock()
            and session.reported_state == reported_state
        ):
            return session
        if loop.time() >= give_up_at:
            raise AssertionError(
                f"no {reported_state!r} heartbeat landed for {clerk_id} within "
                f"{deadline_s}s: "
                f"last_seen_at_ms={None if session is None else session.last_seen_at_ms}, "
                f"reported_state={None if session is None else session.reported_state!r}, "
                f"clock={clock()}"
            )
        await asyncio.sleep(0.01)


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


# ---------------------------------------------------------------------------
# Heartbeat facts, and the boot sequencing that starts them
# ---------------------------------------------------------------------------


def test_heartbeat_facts_for_a_confirmed_paper_lane_are_byte_identical_to_the_pre_fix_closure() -> (
    None
):
    """A confirmed lane's observation did not change shape when it moved.

    The desk reads these four keys and the summary's two, so lifting the
    facts out of main.py's closure is only safe if the confirmed case comes
    out identical — this is the byte-for-byte pin on that.
    """
    from app.broker.alpaca.clerk.fleet_boot import heartbeat_facts

    assert heartbeat_facts(
        account_pin="ABC",
        effective_binding_generation=3,
        authority_kind="sqlite",
        endpoint_mode="paper",
    ) == {
        "reported_binding_generation": 3,
        "reported_account_id": "ABC",
        "reported_state": "binding_confirmed",
        "reported_summary": {"endpoint_mode": "paper", "authority_state": "real_paper"},
    }


def test_heartbeat_facts_for_a_confirmed_live_shadow_lane_report_live_and_shadow() -> None:
    """The two axes are independent: the mode is the endpoint, not the authority."""
    from app.broker.alpaca.clerk.fleet_boot import heartbeat_facts

    assert heartbeat_facts(
        account_pin="U1234567",
        effective_binding_generation=2,
        authority_kind="shadow",
        endpoint_mode="live",
    ) == {
        "reported_binding_generation": 2,
        "reported_account_id": "U1234567",
        "reported_state": "binding_confirmed",
        "reported_summary": {"endpoint_mode": "live", "authority_state": "shadow"},
    }


def test_heartbeat_facts_for_an_unconfirmed_lane_report_pending_with_a_summary_that_parses() -> (
    None
):
    """The epoch-churn pin: an unbound lane's summary must survive the parser.

    ``observe_session`` raises ``ClerkIdentityMismatch`` on anything that is
    not the bounded typed observation, and ``start_heartbeat``'s loop answers
    every refusal by registering a *fresh* session — so a free-form summary
    here would not merely fail, it would climb the routing epoch on every
    beat, forever. Parsing the reported summary back is the assertion; "it
    did not raise" would pass on a summary the coordinator never sees.
    """
    from app.broker.alpaca.clerk.fleet_boot import heartbeat_facts

    facts = heartbeat_facts(
        account_pin="ABC",
        effective_binding_generation=0,
        authority_kind="unavailable",
        endpoint_mode="paper",
    )

    assert facts == {
        "reported_binding_generation": None,
        "reported_account_id": "ABC",
        "reported_state": "binding_pending",
        "reported_summary": {"endpoint_mode": "paper", "authority_state": "unavailable"},
    }
    observation = ProviderSummaryObservation.parse(facts["reported_summary"])
    assert observation is not None
    assert observation.endpoint_mode == SummaryEndpointMode.PAPER
    assert observation.authority_state == "unavailable"


def test_heartbeat_facts_without_an_account_pin_still_carry_a_parsable_summary() -> None:
    """A lane that never reached a pin reports none — not a placeholder."""
    from app.broker.alpaca.clerk.fleet_boot import heartbeat_facts

    facts = heartbeat_facts(
        account_pin=None,
        effective_binding_generation=0,
        authority_kind="synthetic",
        endpoint_mode="paper",
    )

    assert facts == {
        "reported_binding_generation": None,
        "reported_account_id": None,
        "reported_state": "binding_pending",
        "reported_summary": {"endpoint_mode": "paper", "authority_state": "synthetic"},
    }
    observation = ProviderSummaryObservation.parse(facts["reported_summary"])
    assert observation is not None
    assert observation.authority_state == "synthetic"


def test_heartbeat_facts_map_an_unknown_authority_kind_to_unavailable() -> None:
    """An authority vocabulary this lane does not know reads as unavailable."""
    from app.broker.alpaca.clerk.fleet_boot import heartbeat_facts

    facts = heartbeat_facts(
        account_pin="ABC",
        effective_binding_generation=1,
        authority_kind="weird",
        endpoint_mode="live",
    )

    assert facts["reported_summary"] == {
        "endpoint_mode": "live",
        "authority_state": "unavailable",
    }


@pytest.mark.parametrize(
    ("account_pin", "effective_binding_generation", "granted"),
    [
        (None, 1, False),
        ("", 1, False),
        ("pin", 0, False),
        ("pin", 1, True),
        ("pin", 7, True),
    ],
)
def test_binding_is_granted_needs_both_a_pin_and_a_grant(
    account_pin: str | None, effective_binding_generation: int, granted: bool
) -> None:
    """The one predicate: generation 0 is "no grant yet", not "generation zero"."""
    from app.broker.alpaca.clerk.fleet_boot import binding_is_granted

    assert (
        binding_is_granted(
            account_pin=account_pin,
            effective_binding_generation=effective_binding_generation,
        )
        is granted
    )


def test_the_default_lane_facts_parse(control_dir: Path, tmp_path: Path) -> None:
    """What a lane reports before anything installs must survive the parser.

    ``FleetLaneBoot``'s default is not a placeholder: the beat starts with the
    lane, so these are the facts every heartbeat carries until (and unless)
    ``confirm_and_report`` replaces them. A summary the coordinator refuses
    here would not fail once — the heartbeat loop answers a refusal by
    registering a fresh session, so the routing epoch would climb on every
    beat, forever.
    """
    from app.broker.alpaca.clerk.fleet_boot import FleetLaneBoot
    from app.broker.fleet.presence import LocalPresence

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
    )
    try:
        boot = FleetLaneBoot(
            presence=LocalPresence(service, volume_root=tmp_path),
            clerk_id="clrk_unbound00000000000000aa",
            worker_key="wkrk_" + "0" * 32,
            volume_root=tmp_path,
            registry_id="fltr_unbound0000000000000000",
            volume_id="vol_unbound00000000000000",
        )

        assert boot.heartbeat is None
        assert boot.reported_facts == {
            "reported_binding_generation": None,
            "reported_account_id": None,
            "reported_state": "binding_pending",
            "reported_summary": {
                "endpoint_mode": "unidentified",
                "authority_state": "unavailable",
            },
        }
        observation = ProviderSummaryObservation.parse(
            boot.reported_facts["reported_summary"]
        )
        assert observation is not None
        assert observation.endpoint_mode == SummaryEndpointMode.UNIDENTIFIED
        assert observation.authority_state == "unavailable"
    finally:
        service.close()


async def test_a_reserved_only_lane_heartbeats_and_stays_configuration_routable(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paper-lane deadlock, closed at its clerk-side cause.

    An unbound lane used to register, reserve, and then go quiet: the
    coordinator projected it ``unreachable`` and refused even
    ``configuration_access`` — the one surface that could have bound it. The
    lane now beats from boot with ``binding_pending`` facts, so the operator
    can reach the configuration read, while execution routing stays closed
    because a heartbeat still confirms nothing (admission probe 1).
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        confirm_and_report,
        open_fleet_lane,
        reserve_account,
        start_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    heartbeat = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        epoch_at_registration = boot.session.routing_epoch
        await reserve_account(
            boot, external_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )

        # The pre-fix shape, reproduced: no beat since registration, so the
        # session is stale and the configuration surface the lane needs is
        # exactly the one it cannot reach. (Production starts the beat at
        # `open_fleet_lane`; here it is deliberately held back so the
        # deadlock this fix removes is on the record.)
        clock.advance(DEFAULT_SESSION_STALE_AFTER_MS + 1)
        assert _directory_entry(service, boot.clerk_id)["lifecycle_state"] == "unreachable"
        with pytest.raises(ClerkUnreachable, match="older than"):
            service.resolve_route(
                broker="alpaca",
                clerk_id=boot.clerk_id,
                readiness=OperationReadiness.CONFIGURATION_ACCESS,
            )

        heartbeat = start_heartbeat(boot, interval_s=0.05)
        await confirm_and_report(
            boot,
            account_pin="abcdef01-1234-abcd-5678-ef0123456789",
            effective_binding_generation=0,
            effective_profile_id=None,
            effective_revision=None,
            authority_kind="unavailable",
            endpoint_mode="paper",
        )
        session = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_pending"
        )

        assert session.reported_state == "binding_pending"
        assert session.reported_binding_generation is None
        entry = _directory_entry(service, boot.clerk_id)
        assert entry["lifecycle_state"] == "starting"
        assert entry["effective_binding_generation"] is None
        _clerk, routed, assignment = service.resolve_route(
            broker="alpaca",
            clerk_id=boot.clerk_id,
            readiness=OperationReadiness.CONFIGURATION_ACCESS,
        )
        assert assignment is None
        assert routed.routing_epoch == epoch_at_registration
        with pytest.raises(ClerkUnreachable, match="no effective account assignment"):
            service.resolve_route(broker="alpaca", clerk_id=boot.clerk_id)
        assert read_confirmation_evidence(root) is None

        # Three further beats land on the SAME session: a summary the
        # coordinator refused would have re-registered the lane once per
        # beat and climbed the routing epoch with it.
        settled = session
        for _ in range(3):
            clock.advance(1)
            settled = await _await_beat_at(
                service, boot.clerk_id, clock, reported_state="binding_pending"
            )
        assert settled.routing_epoch == epoch_at_registration
        assert boot.session.routing_epoch == epoch_at_registration
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await close_fleet_lane(boot)
        service.close()


async def test_a_confirmed_lane_still_confirms_at_boot_and_heartbeats_binding_confirmed(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound lane's boot is unchanged: confirm first, then observe.

    Same entry point as the unbound lane — the binding state, not the caller,
    decides whether a confirmation happens before the first beat.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        confirm_and_report,
        open_fleet_lane,
        reserve_account,
        start_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    heartbeat = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        epoch_at_registration = boot.session.routing_epoch
        await reserve_account(
            boot, external_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        # Move off the registration instant so a landed beat is distinguishable
        # from the stamp `register` already wrote.
        clock.advance(1)

        heartbeat = start_heartbeat(boot, interval_s=0.05)
        await confirm_and_report(
            boot,
            account_pin="abcdef01-1234-abcd-5678-ef0123456789",
            effective_binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
            authority_kind="sqlite",
            endpoint_mode="paper",
        )
        # The confirmation stamped `last_seen_at_ms` itself (and with the same
        # `binding_confirmed` a confirmed lane beats), so move off that instant
        # — otherwise the wait below returns on the confirmation's own touch
        # and the heartbeat assertions never see a heartbeat.
        clock.advance(1)

        stored = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert stored is not None
        assert stored.state == AssignmentState.EFFECTIVE
        assert stored.confirmed_binding_generation == 1
        evidence = read_confirmation_evidence(root)
        assert evidence is not None
        assert evidence.binding_generation == 1

        session = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_confirmed"
        )
        assert session.reported_state == "binding_confirmed"
        assert session.reported_binding_generation == 1
        assert session.routing_epoch == epoch_at_registration
        entry = _directory_entry(service, boot.clerk_id)
        assert entry["lifecycle_state"] == "ready"
        assert entry["effective_binding_generation"] == 1
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        await close_fleet_lane(boot)
        service.close()


async def test_an_online_lane_with_no_binding_installed_heartbeats_and_stays_configuration_routable(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadlock's widest case: no binding installs at all, ever.

    ``_install_alpaca_binding`` returns ``None`` on four reachable paths — the
    installation lock refused this process, the profiles database is
    unavailable, a profile was staged but never applied, or the installation
    is unconfigured with no usable environment settings. On every one of them
    ``confirm_and_report`` is never reached, so a beat gated on the binding is
    a beat that never runs: the lane goes stale, projects ``unreachable``, and
    the coordinator refuses the ``configuration_access`` reads that were the
    only way to bind it.

    Here the lane does nothing but open — no reservation, no confirmation —
    and must still be present and configuration-routable.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        open_fleet_lane,
        start_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        epoch_at_registration = boot.session.routing_epoch
        heartbeat = start_heartbeat(boot, interval_s=0.05)
        assert boot.heartbeat is heartbeat

        # Past the staleness horizon with nothing installed: this is exactly
        # where the lane used to fall silent.
        clock.advance(DEFAULT_SESSION_STALE_AFTER_MS + 1)
        session = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_pending"
        )

        assert session.reported_binding_generation is None
        assert session.reported_account_id is None
        observation = ProviderSummaryObservation.parse(session.reported_summary_json)
        assert observation is not None
        assert observation.endpoint_mode == SummaryEndpointMode.UNIDENTIFIED
        assert observation.authority_state == "unavailable"
        entry = _directory_entry(service, boot.clerk_id)
        assert entry["lifecycle_state"] == "starting"
        assert entry["effective_binding_generation"] is None
        _clerk, routed, assignment = service.resolve_route(
            broker="alpaca",
            clerk_id=boot.clerk_id,
            readiness=OperationReadiness.CONFIGURATION_ACCESS,
        )
        assert assignment is None
        assert routed.routing_epoch == epoch_at_registration
        with pytest.raises(ClerkUnreachable, match="no effective account assignment"):
            service.resolve_route(broker="alpaca", clerk_id=boot.clerk_id)

        # Three further beats on the SAME session: a summary the coordinator
        # refused would re-register the lane once per beat and climb the epoch.
        settled = session
        for _ in range(3):
            clock.advance(1)
            settled = await _await_beat_at(
                service, boot.clerk_id, clock, reported_state="binding_pending"
            )
        assert settled.routing_epoch == epoch_at_registration
        assert boot.session.routing_epoch == epoch_at_registration
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_confirm_and_report_switches_the_reported_facts_without_restarting_the_beat(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install changes what the lane says, not who says it.

    The beat belongs to the lane, so the binding arrives *under* a task that
    is already running. A second task would double the observe cadence and
    race the first one's facts, and the desk would see the two disagree.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        confirm_and_report,
        open_fleet_lane,
        reserve_account,
        start_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        epoch_at_registration = boot.session.routing_epoch
        heartbeat = start_heartbeat(boot, interval_s=0.05)
        clock.advance(1)
        pending = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_pending"
        )
        unbound = ProviderSummaryObservation.parse(pending.reported_summary_json)
        assert unbound is not None
        assert unbound.endpoint_mode == SummaryEndpointMode.UNIDENTIFIED

        await reserve_account(
            boot, external_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        await confirm_and_report(
            boot,
            account_pin="abcdef01-1234-abcd-5678-ef0123456789",
            effective_binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
            authority_kind="sqlite",
            endpoint_mode="paper",
        )
        assert boot.heartbeat is heartbeat
        assert not heartbeat.done()
        # The confirmation stamped `last_seen_at_ms` with `binding_confirmed`
        # itself; move off that instant so the wait below reads a beat.
        clock.advance(1)

        confirmed = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_confirmed"
        )
        assert confirmed.reported_binding_generation == 1
        bound = ProviderSummaryObservation.parse(confirmed.reported_summary_json)
        assert bound is not None
        assert bound.endpoint_mode == SummaryEndpointMode.PAPER
        assert bound.authority_state == "real_paper"
        assert confirmed.routing_epoch == epoch_at_registration
        assert boot.heartbeat is heartbeat
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_close_fleet_lane_stops_the_heartbeat(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The beat's lifetime is the lane's, at both ends.

    It observes through the very presence ``close_fleet_lane`` releases, and a
    lane that kept beating after shutdown would keep telling the coordinator
    it was reachable.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        open_fleet_lane,
        start_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        clerk_id = boot.clerk_id
        heartbeat = start_heartbeat(boot, interval_s=0.05)
        clock.advance(1)
        beat = await _await_beat_at(
            service, clerk_id, clock, reported_state="binding_pending"
        )

        await close_fleet_lane(boot)
        boot = None
        assert heartbeat.done()

        # Several intervals of real time at a moved clock: a live beat would
        # stamp the new instant, so an unchanged stamp is the beat's silence.
        clock.advance(1)
        for _ in range(4):
            await asyncio.sleep(0.05)
        after = service._store.read_session(clerk_id)
        assert after is not None
        assert after.last_seen_at_ms == beat.last_seen_at_ms
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_serve_lane_presence_installs_the_identity_echo_and_starts_the_beat_without_a_binding(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The echo belongs to the lane, exactly as the beat does (FR-076).

    The identity echo used to be installed inside the binding branch, so the
    four paths that install no binding left an open, heartbeating lane
    answering forwarded ``configuration_access`` calls with no echo at all —
    and ``verify_identity_echo`` rejects every one of those responses *after*
    the clerk has already executed the call. The wiring is asserted on the
    real ``app.main`` application object, because a copy of the wiring in a
    throwaway ``FastAPI()`` would pin nothing about what production installs.
    """
    from app.broker.alpaca.clerk.fleet_boot import close_fleet_lane, open_fleet_lane
    from app.broker.fleet.agent_identity import SERVED_IDENTITY_STATE_KEY
    from app.main import app, serve_lane_presence

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        # The one state key the middleware reads through; monkeypatch restores
        # whatever the real app carried (normally nothing) afterwards.
        monkeypatch.setattr(app.state, SERVED_IDENTITY_STATE_KEY, None, raising=False)

        # No reservation, no confirmation, no binding — the unbound lane.
        serve_lane_presence(app, boot, interval_s=0.05)

        provider = getattr(app.state, SERVED_IDENTITY_STATE_KEY)
        served = provider()
        assert served is not None
        assert served["broker"] == "alpaca"
        assert served["clerk_id"] == boot.clerk_id
        assert served["routing_epoch"] == boot.session.routing_epoch
        # The generation is whatever the selection reads *now* — none at all
        # on an unbound lane, an int once one is applied. The key's presence
        # is the contract; its value is live state.
        assert "binding_generation" in served
        assert served["binding_generation"] is None or isinstance(
            served["binding_generation"], int
        )

        assert boot.heartbeat is not None
        assert not boot.heartbeat.done()
        beat = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_pending"
        )
        assert beat.reported_binding_generation is None
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_stop_heartbeat_is_idempotent_and_ends_the_beat(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown starts by ending the beat, and may say so twice.

    The service teardown (bots, consumers, custody, repositories) runs before
    the lane closes, so a beat that only stops at ``close_fleet_lane`` keeps
    telling the coordinator a dismantling clerk is reachable. The inner
    teardown calls this first, and the lane close calls it again — so calling
    it twice must be as quiet as calling it once.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        open_fleet_lane,
        start_heartbeat,
        stop_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        clerk_id = boot.clerk_id
        heartbeat = start_heartbeat(boot, interval_s=0.05)
        clock.advance(1)
        beat = await _await_beat_at(
            service, clerk_id, clock, reported_state="binding_pending"
        )

        await stop_heartbeat(boot)
        assert heartbeat.done()
        assert boot.heartbeat is None
        await stop_heartbeat(boot)
        assert boot.heartbeat is None

        # Several intervals of real time at a moved clock: a live beat would
        # stamp the new instant, so an unchanged stamp is the beat's silence.
        clock.advance(1)
        for _ in range(4):
            await asyncio.sleep(0.05)
        after = service._store.read_session(clerk_id)
        assert after is not None
        assert after.last_seen_at_ms == beat.last_seen_at_ms

        # The lane still closes cleanly behind an already-stopped beat.
        await close_fleet_lane(boot)
        boot = None
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_stop_heartbeat_logs_a_dead_beat_instead_of_raising(
    control_dir: Path,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A beat that died on an unexpected exception must not abort teardown.

    ``_beat`` only catches ``FleetControlError`` around its observation call.
    Anything else — a malformed coordinator response, a raw ``sqlite3`` error
    surfacing from ``LocalPresence`` — ends the task with that exception
    stored on it, done, uncancelled. ``stop_heartbeat`` is the first
    statement of the service teardown's ``finally`` block, so if it awaited
    that already-done task the stored exception would re-raise there and
    skip every step after it. It must log and swallow instead.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        open_fleet_lane,
        start_heartbeat,
        stop_heartbeat,
    )

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online

        async def _observe_and_explode(**_kwargs: object) -> None:
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(boot.presence, "observe", _observe_and_explode)

        start_heartbeat(boot, interval_s=0.05)
        heartbeat = boot.heartbeat
        assert heartbeat is not None

        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + 5.0
        while not heartbeat.done():
            if loop.time() >= give_up_at:
                raise AssertionError("the beat never died on the injected RuntimeError")
            await asyncio.sleep(0.01)

        with caplog.at_level(logging.WARNING, logger="app.broker.alpaca.clerk.fleet_boot"):
            await stop_heartbeat(boot)

        assert boot.heartbeat is None
        dead_beat_records = [
            record for record in caplog.records if "already ended with an error" in record.getMessage()
        ]
        assert len(dead_beat_records) == 1
        record = dead_beat_records[0]
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError

        await close_fleet_lane(boot)
        boot = None
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_a_refused_beat_re_registers_under_the_endpoint_reference_it_registered_with(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement session cites the same approved endpoint, or routes nowhere.

    ``register_agent_session`` keeps the current reference only while the
    session row survives; a session the coordinator lost is re-created with
    whatever the registration cites. Re-registering with ``endpoint_ref=None``
    therefore leaves a lane that heartbeats as reachable while
    ``coordinator_delivery_for`` refuses every delivery to it, because the
    session names no approved endpoint.

    The refusal itself is injected at the presence seam (one observation,
    once) — the transport is exactly where a coordinator's refusal arrives
    from. Everything after it is the real thing: the real approval check, the
    real epoch bump, and the assertion reads the registry's own row.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        open_fleet_lane,
        start_heartbeat,
    )
    from app.broker.fleet.errors import ClerkIdentityMismatch

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _boot_service_on_the_test_clock(monkeypatch, clock)
        service.approve_endpoint(
            clerk_id=provisioned.clerk.clerk_id,
            endpoint_ref="agent:paper-1",
            base_url="http://alpaca-paper-clerk:8000",
        )
        settings = FleetSettings(
            ROLE="clerk_agent",
            CONTROL_DIR=str(control_dir),
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
            AGENT_ENDPOINT_REF="agent:paper-1",
            DEPLOYMENT_NAMESPACE="compose:test",
        )

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        assert boot.endpoint_ref == "agent:paper-1"
        registered = service._store.read_session(boot.clerk_id)
        assert registered is not None
        assert registered.endpoint_ref == "agent:paper-1"
        epoch_at_registration = boot.session.routing_epoch

        refused: list[object] = []
        real_observe = boot.presence.observe

        async def _refuse_the_first_observation(**kwargs: object) -> None:
            """Refuse once, the way a coordinator that lost the session does."""
            if not refused:
                refused.append(kwargs)
                raise ClerkIdentityMismatch(
                    "The coordinator refuses this lane's observation.",
                )
            await real_observe(**kwargs)

        monkeypatch.setattr(boot.presence, "observe", _refuse_the_first_observation)

        start_heartbeat(boot, interval_s=0.05)
        # The first beat is refused and re-registers; the next one lands, so a
        # `binding_pending` beat is the proof the replacement session exists.
        beat = await _await_beat_at(
            service, boot.clerk_id, clock, reported_state="binding_pending"
        )

        assert refused, "the beat was never refused, so nothing re-registered"
        assert beat.routing_epoch > epoch_at_registration
        assert beat.agent_instance_id == boot.session.agent_instance_id
        assert beat.endpoint_ref == "agent:paper-1"
    finally:
        await close_fleet_lane(boot)
        service.close()


async def test_confirm_binding_records_the_session_it_confirmed_even_if_the_lane_re_registers_mid_confirm(
    control_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence names the session the coordinator actually confirmed.

    ``confirm_binding`` awaits the coordinator, and a refused beat during that
    await re-registers the lane — replacing ``boot.session`` under it. Reading
    the session a second time for the evidence would write an instance id and
    epoch the coordinator never saw, and the next boot's FR-066 recovery reads
    that evidence as fact.
    """
    from app.broker.alpaca.clerk.fleet_boot import (
        close_fleet_lane,
        confirm_binding,
        open_fleet_lane,
        reserve_account,
    )
    from app.broker.fleet.identity import new_agent_instance_id

    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    boot = None
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

        boot = await open_fleet_lane(settings=settings, volume_root=root)
        assert boot is not None and boot.online
        await reserve_account(
            boot, external_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        confirmed_session = boot.session
        replacement = dataclasses.replace(
            confirmed_session,
            agent_instance_id=new_agent_instance_id(),
            routing_epoch=confirmed_session.routing_epoch + 1,
        )
        real_confirm = boot.presence.confirm

        async def _re_register_then_confirm(**kwargs: object):
            """A refused beat's re-registration, landing mid-confirmation."""
            boot.session = replacement
            return await real_confirm(**kwargs)

        monkeypatch.setattr(boot.presence, "confirm", _re_register_then_confirm)

        await confirm_binding(
            boot,
            external_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
        )

        evidence = read_confirmation_evidence(root)
        assert evidence is not None
        assert evidence.agent_instance_id == confirmed_session.agent_instance_id
        assert evidence.routing_epoch == confirmed_session.routing_epoch
        stored = service._store.read_assignment(
            broker="alpaca", canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789"
        )
        assert stored is not None
        assert stored.confirmed_agent_instance_id == confirmed_session.agent_instance_id
        assert stored.confirmed_routing_epoch == confirmed_session.routing_epoch
    finally:
        await close_fleet_lane(boot)
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


async def test_a_reachable_coordinator_that_refuses_expectation_boots_offline_too(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-066 also covers refusal, not only unreachability.

    Distinct from ``test_an_offline_coordinator_boots_only_the_evidence_confirmed_tuple``
    above: that test dials a dead port and never gets a response at all. Here
    the coordinator is a real, reachable process that answers every
    boot-time ``expectation()`` call with a plain 403 — a wrong token or an
    unknown clerk, never a connection failure.

    The only reason this still reaches the FR-066 offline fallback is that
    ``RemotePresence.expectation``'s non-200 branch in
    ``app/broker/fleet/presence.py`` raises ``FleetPresenceError``. Before
    that reclassification it raised the base ``FleetControlError``, which
    ``fleet_boot.py``'s ``except FleetPresenceError as exc:`` does not catch
    — boot would crash instead of falling back to the already-confirmed
    evidence, for an already-confirmed live lane.
    """
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )
    coordinator = FastAPI()

    @coordinator.get("/internal/fleet/clerks/{clerk_id}/volume-expectation")
    async def volume_expectation(clerk_id: str) -> JSONResponse:
        # Reachable, but refuses: a wrong token or an unknown clerk both
        # surface as a plain 4xx, never a socket error.
        del clerk_id
        return JSONResponse({"detail": "agent token refused"}, status_code=403)

    server = _RealServer(coordinator)
    server.start()
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        write_confirmation_evidence(
            root,
            ConfirmationEvidence(
                clerk_id=provisioned.clerk.clerk_id,
                volume_id=provisioned.clerk.volume_id,
                registry_id="fltr_refused0000000000000000",
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
        settings = FleetSettings(
            ROLE="clerk_agent",
            COORDINATOR_URL=server.base_url,
            AGENT_SERVICE_TOKEN="svct_" + "1" * 32,
            CLERK_ID=provisioned.clerk.clerk_id,
            WORKER_KEY=provisioned.clerk.worker_key,
        )
        from app.broker.alpaca.clerk.fleet_boot import offline_boot_matches, open_fleet_lane

        boot = await open_fleet_lane(settings=settings, volume_root=root)

        assert boot is not None
        assert not boot.online
        assert boot.offline_reason is not None
        assert "refused" in boot.offline_reason
        assert offline_boot_matches(
            boot,
            canonical_account_id="abcdef01-1234-abcd-5678-ef0123456789",
            effective_profile_id="prof_1",
            effective_revision=2,
        )
    finally:
        server.stop()
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


async def test_local_delivery_refuses_a_handler_that_returns_the_wrong_result_type() -> None:
    """A bare ``assert isinstance`` vanishes under ``python -O`` (precedent:
    fleet_boot.py's writable-root fence); the combined posture's in-process
    handler is untrusted the same as a real agent's HTTP response, so a
    shape mismatch must raise unconditionally.

    Raises ``DeliveryContractViolation``, not ``DeliveryIdentityMismatch``
    (#2119): the handler returned the wrong Python type before any identity
    echo was even inspected, which is a dispatch defect in the handler
    wiring, not the serving runtime naming the wrong lane."""
    from app.broker.fleet.delivery import LocalLaneDelivery

    request = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=_alpaca_operation("account_read"),
        path_params={},
    )

    wrong_deliver = LocalLaneDelivery(lambda _request: "not-a-delivery-result")
    with pytest.raises(DeliveryContractViolation, match="DeliveryResult"):
        await wrong_deliver.deliver(request)

    stream_operation = _alpaca_operation("gallery_stream")
    stream_request = DeliveryRequest(
        broker="alpaca",
        clerk_id="clrk_testagent00000000000000aa",
        operation=stream_operation,
        path_params={"account_id": "abcdef01-1234-abcd-5678-ef0123456789"},
    )
    wrong_stream = LocalLaneDelivery(lambda _request: "not-a-stream-result")
    with pytest.raises(DeliveryContractViolation, match="StreamDeliveryResult"):
        await wrong_stream.stream(stream_request)


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
        "sys.stdout.write(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})) + '\\n')"
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
            "sys.stdout.write(json.dumps(sorted({getattr(r, 'path', '') for r in app.routes})) + '\\n')"
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
        worker_restart=None,
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
