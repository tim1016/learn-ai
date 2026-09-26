"""#2320 and #2321: only an unreachable coordinator is unavailability.

- #2320: a coordinator that *answers* with a typed refusal (a retired or
  unknown clerk, a mixed-build version fence) was read as "unavailable", so
  the lane booted its last binding offline under FR-066 against the very
  coordinator that had just refused it.
- #2321: a lane that did boot offline (the coordinator really was down) never
  tried to register again, so it never rejoined and never heard a drain or a
  retirement while its custody kept running.

Real code throughout: ``open_fleet_lane``, ``start_heartbeat``,
``RemotePresence``, the ``internal_fleet`` router and a ``FleetControlService``
on a temp registry. The one fake is the HTTP transport: a refusing transport
stands in for a coordinator that is down, ``httpx.ASGITransport`` for one
that is up.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk import fleet_boot
from app.broker.alpaca.clerk.fleet_boot import (
    FleetBootRefused,
    FleetLaneBoot,
    close_fleet_lane,
    confirm_and_report,
    offline_boot_matches,
    open_fleet_lane,
    start_heartbeat,
)
from app.broker.fleet import presence as presence_module
from app.broker.fleet import volume as volume_module
from app.broker.fleet.confirmation import read_confirmation_evidence
from app.broker.fleet.errors import (
    FleetControlError,
    FleetRegistryRecoveryPending,
    FleetRegistryUnavailable,
)
from app.broker.fleet.presence import (
    FleetLaneDraining,
    FleetLaneRetired,
    FleetPresenceError,
    FleetPresenceRefused,
    LocalPresence,
    RemotePresence,
)
from app.broker.fleet.provider import FLEET_PROTOCOL_VERSION
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.service import FleetControlService, ProvisionedClerk
from app.config import FleetSettings
from app.services.bot_runner import (
    BotTaskRegistry,
    MarketDataFeedUnavailableError,
    RunAdmissionRefusedError,
    fleet_lane_start_gate,
)
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.fleet.conftest import FrozenClock

from .test_a2_alpaca_lane import _enrolled_lane
from .test_drained_lane_resurrection import (
    ACCOUNT,
    CLERK_TOKEN,
    _agent_app,
    _open_confirmed_lane,
    _service,
)

#: A private destination nothing listens on; the transport below never dials it.
COORDINATOR_URL = "http://127.0.0.1:1"


class _Coordinator:
    """The coordinator as the lane's transport sees it.

    Down (a refused connection), up (the real ``internal_fleet`` router over
    the service), up but answering every call with one canned ``refusal``
    (status, body), or up with its heartbeat route failing (``fail_observe``,
    a 503) so the lane's beat takes the repair path. ``requests`` counts the
    calls that reached an up coordinator.
    """

    def __init__(self, service: FleetControlService, clerk_id: str) -> None:
        self._app: FastAPI = _agent_app(clerk_id)
        self._app.state.fleet_service = service
        self._asgi = httpx.ASGITransport(app=self._app)
        self.up = False
        self.refusal: tuple[int, dict[str, str] | None] | None = None
        self.fail_observe = False
        self.requests = 0

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests += 1
        if self.refusal is not None:
            status, body = self.refusal
            return httpx.Response(status) if body is None else httpx.Response(status, json=body)
        if self.fail_observe and request.url.path.endswith("/sessions/observe"):
            return httpx.Response(503)
        return await self._asgi.handle_async_request(request)

    def client(self, **_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._answer), base_url="http://coordinator"
        )


def _remote_settings(provisioned: ProvisionedClerk) -> FleetSettings:
    return FleetSettings(
        ROLE="clerk_agent",
        COORDINATOR_URL=COORDINATOR_URL,
        AGENT_SERVICE_TOKEN=CLERK_TOKEN,
        CLERK_ID=provisioned.clerk.clerk_id,
        WORKER_KEY=provisioned.clerk.worker_key,
    )


async def _confirmed_then_closed(
    service: FleetControlService,
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ProvisionedClerk, Path]:
    """A lane that served, confirmed its grant, and went down cleanly."""
    provisioned, root, boot = await _open_confirmed_lane(
        service, control_dir, clock, tmp_path, monkeypatch
    )
    await close_fleet_lane(boot)
    evidence = read_confirmation_evidence(root)
    assert evidence is not None and evidence.lifecycle_state == "provisioned"
    return provisioned, root


async def _until(predicate: Callable[[], bool], *, what: str, deadline_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + deadline_s
    while not predicate():
        if loop.time() >= give_up_at:
            raise AssertionError(f"not within {deadline_s}s: {what}")
        await asyncio.sleep(0.01)


async def _offline_lane(
    coordinator: _Coordinator, provisioned: ProvisionedClerk, root: Path
) -> FleetLaneBoot:
    """Restart the lane while the coordinator is down: FR-066's offline boot."""
    assert not coordinator.up
    boot = await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
    assert boot is not None and not boot.online
    assert offline_boot_matches(
        boot,
        canonical_account_id=ACCOUNT,
        effective_profile_id="prof_1",
        effective_revision=2,
        binding_generation=1,
    )
    return boot


# ---------------------------------------------------------------------------
# #2320: a typed refusal from a reachable coordinator is an answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_version", "protocol_version"),
    [
        (f"{fleet_boot._ADAPTER.adapter_version}.incompatible", FLEET_PROTOCOL_VERSION),
        (fleet_boot._ADAPTER.adapter_version, FLEET_PROTOCOL_VERSION + 1),
    ],
    ids=["adapter_label_fence", "protocol_version_fence"],
)
async def test_a_version_fence_refusal_is_not_unavailability(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_version: str,
    protocol_version: int,
) -> None:
    """#2340's probe: both mixed-build fences answer 409 from a reachable
    coordinator, and neither may land in FR-066's offline family."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        presence = RemotePresence(base_url=COORDINATOR_URL, agent_service_token=CLERK_TOKEN)
        with pytest.raises(FleetControlError) as refused:
            await presence.register(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                agent_instance_id="agnt_0000000000000000000000aa",
                endpoint_ref=None,
                adapter_version=adapter_version,
                fleet_protocol_version=protocol_version,
            )
        assert not isinstance(refused.value, FleetPresenceError)
        assert isinstance(refused.value, FleetPresenceRefused)
        assert refused.value.coordinator_reason == "fleet_protocol_incompatible"
    finally:
        service.close()


async def test_a_mixed_build_restarting_against_a_reachable_coordinator_refuses_to_boot(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fence that exists to stop a mismatched build must not start it
    unfenced: boot refuses, and the evidence is left as it was (the refusal
    is not a lifecycle lesson, so nothing is tombstoned)."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", 99)

        with pytest.raises(FleetBootRefused, match="fleet_protocol_incompatible"):
            await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "provisioned"
    finally:
        service.close()


async def test_a_retired_clerk_restarting_against_a_reachable_coordinator_refuses_and_tombstones(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2320's probe: drained while down, force-retired past its deadline,
    then restarted with the coordinator up — boot refuses and the evidence
    the lane never got to mark is tombstoned now."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        clerk_id = provisioned.clerk.clerk_id
        drained = service.drain_clerk(clerk_id=clerk_id)
        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        service.force_retire_clerk(clerk_id=clerk_id, operator="ops", change_ref="chg-2320")
        coordinator = _Coordinator(service, clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)

        with pytest.raises(FleetBootRefused, match="retired"):
            await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "draining"
    finally:
        service.close()


async def test_a_reachable_coordinator_answering_an_unknown_clerk_refuses_to_boot(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that holds no such clerk answered; it was not absent."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        other_dir = control_dir.parent / "other-registry"
        other_dir.mkdir()
        other = _service(other_dir, clock)
        try:
            coordinator = _Coordinator(other, provisioned.clerk.clerk_id)
            coordinator.up = True
            monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
            with pytest.raises(FleetBootRefused, match="clerk_not_found"):
                await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        finally:
            other.close()
    finally:
        service.close()


# ---------------------------------------------------------------------------
# #2321: an offline-booted lane keeps trying, rejoins, and learns
# ---------------------------------------------------------------------------


async def test_an_offline_booted_lane_rejoins_and_learns_its_drain(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2321's regression: boot offline against a dead coordinator, bring the
    coordinator up, then drain the clerk — the lane re-registers, re-presents
    the grant it recovered offline, and learns the drain."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        clerk_id = provisioned.clerk.clerk_id
        coordinator = _Coordinator(service, clerk_id)
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await _offline_lane(coordinator, provisioned, root)
        # What main.py does for an offline lane once its binding installs:
        # report it, and hold the evidence-vouched grant for the rejoin.
        await confirm_and_report(
            boot,
            account_pin=ACCOUNT,
            effective_binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
            authority_kind="sqlite",
            endpoint_mode="paper",
        )
        start_heartbeat(boot, interval_s=0.02)
        await asyncio.sleep(0.1)
        assert not boot.online  # still down: FR-066 keeps riding it out

        coordinator.up = True
        await _until(lambda: boot.online, what="the offline lane rejoined")
        await _until(
            lambda: boot.confirmed_grant_session == boot.session,
            what="the recovered grant was re-presented under the new session",
        )
        session = service._store.read_session(clerk_id)
        assert session is not None
        assert boot.session is not None
        assert session.agent_instance_id == boot.session.agent_instance_id
        assert boot.offline_reason is None

        service.drain_clerk(clerk_id=clerk_id)
        await _until(lambda: boot.draining, what="the rejoined lane learned its drain")
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "draining"
        assert boot.heartbeat is not None and not boot.heartbeat.done()
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_an_offline_lane_drained_while_it_was_down_learns_it_on_rejoin(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain lands during the outage: the rejoin's registration refusal
    is the lesson, and the lane keeps trying so it can hear a retirement."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        clerk_id = provisioned.clerk.clerk_id
        coordinator = _Coordinator(service, clerk_id)
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await _offline_lane(coordinator, provisioned, root)
        stops: list[int] = []

        async def stop_bots() -> bool:
            stops.append(1)
            return True

        boot.stop_bots = stop_bots
        start_heartbeat(boot, interval_s=0.02)

        drained = service.drain_clerk(clerk_id=clerk_id)
        coordinator.up = True
        await _until(lambda: boot.draining, what="the offline lane learned its drain")
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "draining"
        assert not boot.online and not boot.retired

        # Force-retired past the deadline: the lane is still asking, so it hears.
        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        service.force_retire_clerk(clerk_id=clerk_id, operator="ops", change_ref="chg-2321")
        assert boot.heartbeat is not None
        await asyncio.wait_for(asyncio.shield(boot.heartbeat), timeout=5.0)
        assert boot.retired
        assert stops == [1]
        await close_fleet_lane(boot)
    finally:
        service.close()


# ---------------------------------------------------------------------------
# A refusal gates new starts; only the lane's own retirement stops bots
# ---------------------------------------------------------------------------


class _StopCounter:
    """The lane's bot-stop hook, counting the times it was asked to stop."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return True


#: Refusals a coordinator can answer a presence call with that are about its
#: own state as much as this lane's: a coordinator restarted with an empty
#: token map, a registry mid-restore, a process serving no fleet router, a
#: registry that holds no such clerk. ``typed`` is whether the reason code is
#: the coordinator's word about this lane (``LANE_ADMISSION_REFUSALS``).
_COORDINATOR_STATE_REFUSALS = [
    pytest.param(
        403,
        {"reason": "fleet_agent_token_refused", "message": "agent token refused"},
        True,
        id="token_refused",
    ),
    pytest.param(
        409,
        {"reason": "fleet_registry_recovery_pending", "message": "restore in progress"},
        False,
        id="recovery_pending",
    ),
    pytest.param(404, None, False, id="bodyless_404"),
    pytest.param(
        404,
        {"reason": "clerk_not_found", "message": "No clerk carries that identity."},
        True,
        id="clerk_not_found",
    ),
]


@pytest.mark.parametrize(("status", "body", "typed"), _COORDINATOR_STATE_REFUSALS)
async def test_a_coordinator_state_refusal_never_stops_an_offline_lanes_bots(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, str] | None,
    typed: bool,
) -> None:
    """A Stop is local and durable: it leaves the position unmanaged and the
    bot stopped after readmission. So a refusal on rejoin never stops a bot;
    a typed one gates new starts, an untyped one is still unavailability."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await _offline_lane(coordinator, provisioned, root)
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        coordinator.refusal = (status, body)
        coordinator.up = True
        await _until(lambda: coordinator.requests >= 8, what="several refused rejoins")

        assert stops.calls == 0
        assert not boot.online and not boot.draining and not boot.retired
        assert boot.start_refusal == ("fleet_presence_refused" if typed else None)
        assert boot.heartbeat is not None and not boot.heartbeat.done()
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize(("status", "body", "typed"), _COORDINATOR_STATE_REFUSALS)
async def test_a_coordinator_state_refusal_never_stops_an_online_lanes_bots(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, str] | None,
    typed: bool,
) -> None:
    """The same answers reach a live lane through its beat and its repair,
    and mean the same thing there: no bot stops, its session is kept."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        assert boot is not None and boot.online
        session = boot.session
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        coordinator.refusal = (status, body)
        await _until(lambda: coordinator.requests >= 8, what="several refused beats")

        assert stops.calls == 0
        assert boot.session == session
        assert not boot.draining and not boot.retired
        assert boot.start_refusal == ("fleet_presence_refused" if typed else None)
        assert boot.heartbeat is not None and not boot.heartbeat.done()
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        pytest.param(
            409,
            {"reason": "fleet_registry_recovery_pending", "message": "restore in progress"},
            id="recovery_pending",
        ),
        pytest.param(404, None, id="bodyless_404"),
    ],
)
async def test_a_restore_or_a_bodyless_refusal_still_boots_offline(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: dict[str, str] | None,
) -> None:
    """Classified by reason code, not status class: neither answer is the
    coordinator's word about this lane, so a restore ceremony (or a process
    with no fleet router) never stops a live lane booting its custody."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.refusal = (status, body)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)

        boot = await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)

        assert boot is not None and not boot.online
        assert boot.start_refusal is None
        assert offline_boot_matches(
            boot,
            canonical_account_id=ACCOUNT,
            effective_profile_id="prof_1",
            effective_revision=2,
            binding_generation=1,
        )
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (404, "clerk_not_found", FleetPresenceRefused),
        (409, "fleet_protocol_incompatible", FleetPresenceRefused),
        (409, "clerk_identity_mismatch", FleetPresenceRefused),
        (409, "clerk_endpoint_not_approved", FleetPresenceRefused),
        (403, "fleet_agent_token_refused", FleetPresenceRefused),
        (409, "fleet_registry_recovery_pending", FleetPresenceError),
        (400, "some_future_code", FleetPresenceError),
        (404, None, FleetPresenceError),
        (409, "clerk_lane_draining", FleetLaneDraining),
        (404, "clerk_lane_retired", FleetLaneRetired),
    ],
)
async def test_remote_presence_classifies_a_refusal_by_its_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reason: str | None,
    expected: type[FleetControlError],
) -> None:
    """Only the typed lane-identity and admission codes are refusals; a
    restore, an unknown code or no code at all is unavailability."""

    def answer(request: httpx.Request) -> httpx.Response:
        if reason is None:
            return httpx.Response(status)
        return httpx.Response(status, json={"reason": reason, "message": "refused"})

    monkeypatch.setattr(
        presence_module,
        "build_internal_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )
    presence = RemotePresence(base_url=COORDINATOR_URL, agent_service_token=CLERK_TOKEN)
    for call in (
        presence.expectation(clerk_id="clrk_aaaaaaaaaaaaaaaaaaaaaaaa"),
        presence.register(
            clerk_id="clrk_aaaaaaaaaaaaaaaaaaaaaaaa",
            worker_key="wk",
            agent_instance_id="agnt_0000000000000000000000aa",
            endpoint_ref=None,
            adapter_version="alpaca-fleet.8",
            fleet_protocol_version=2,
        ),
    ):
        with pytest.raises(FleetControlError) as refused:
            await call
        assert type(refused.value) is expected
        if expected is FleetPresenceRefused:
            assert refused.value.coordinator_reason == reason


async def test_a_live_lane_whose_repair_meets_a_version_fence_gates_new_starts(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair is the same registration unit as boot and rejoin: a refused
    beat's re-registration that meets the version fence gates new starts
    (it used to be logged and forgotten), keeps the bots running, and a
    later landed beat reopens starts."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        assert boot is not None and boot.online
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        coordinator.fail_observe = True
        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", 99)
        await _until(
            lambda: boot.start_refusal == "fleet_presence_refused",
            what="the repair's version-fence refusal gated new starts",
        )
        assert boot.admission_refusal == "fleet_protocol_incompatible"
        assert boot.online
        assert stops.calls == 0

        coordinator.fail_observe = False
        await _until(lambda: boot.start_refusal is None, what="a landed beat readmitted the lane")
        assert stops.calls == 0
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_a_rejoin_whose_volume_fails_the_identity_gate_is_refused(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0062 Decision 2 holds on rejoin too: the mounted root is proven
    against the registry's expectation before any registration, and a root
    that fails it is a refusal — starts gate, nothing registers."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        clerk_id = provisioned.clerk.clerk_id
        coordinator = _Coordinator(service, clerk_id)
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await _offline_lane(coordinator, provisioned, root)
        stale_session = service._store.read_session(clerk_id)
        marker_file = volume_module.marker_path(root)
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        marker["attestation_id"] = "attn_someone_else"
        marker_file.write_text(json.dumps(marker), encoding="utf-8")
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        coordinator.up = True
        await _until(
            lambda: boot.start_refusal == "fleet_presence_refused",
            what="the identity gate refused the rejoin",
        )
        assert boot.admission_refusal == "clerk_volume_identity_mismatch"
        assert not boot.online
        assert service._store.read_session(clerk_id) == stale_session
        assert stops.calls == 0
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_a_refused_offline_lane_gates_a_real_runner_until_it_is_admitted(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through a real ``BotTaskRegistry``: the fleet start gate
    reads ``start_refusal``, so a refused lane's operator sees the refusal
    before any admission work, running bots are never stopped, and the
    same registry starts again once the coordinator admits the lane."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await _offline_lane(coordinator, provisioned, root)
        runner_root = tmp_path / "runner"
        runner_root.mkdir()
        registry = BotTaskRegistry(
            runner_root,
            feed_resolver=lambda: None,
            boot_recovery_required=False,
            lane_start_gates=(fleet_lane_start_gate(lambda: boot.start_refusal),),
        )
        stops = _StopCounter()
        boot.stop_bots = stops
        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", 99)
        start_heartbeat(boot, interval_s=0.01)

        coordinator.up = True
        await _until(lambda: boot.start_refusal is not None, what="the coordinator refused")
        with pytest.raises(RunAdmissionRefusedError, match="refused this lane") as refused:
            await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        assert "fleet_protocol_incompatible" not in str(refused.value.detail)
        assert stops.calls == 0
        evidence = read_confirmation_evidence(root)
        assert evidence is not None
        assert evidence.lifecycle_state == StoredLifecycleState.PROVISIONED.value

        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", FLEET_PROTOCOL_VERSION)
        await _until(lambda: boot.online, what="the lane rejoined once admitted")
        assert boot.start_refusal is None
        # Past the fleet gate: what refuses now is the runner's own admission
        # (this test installs no clerk or feed), never the fleet refusal.
        with pytest.raises((RunAdmissionRefusedError, MarketDataFeedUnavailableError)) as later:
            await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        assert "refused this lane" not in str(later.value)
        assert stops.calls == 0
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize(
    "unavailable",
    [
        pytest.param(
            FleetRegistryRecoveryPending("restore in progress"), id="recovery_pending"
        ),
        pytest.param(FleetRegistryUnavailable("registry unreadable"), id="registry_unavailable"),
    ],
)
async def test_a_combined_lanes_registry_unavailability_is_not_a_refusal(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: FleetControlError,
) -> None:
    """``LocalPresence`` raises the registry's own availability types, not the
    wire's ``FleetPresenceError``. A remote lane reads a restore as
    unavailability and keeps its starts open; a combined lane mid-restore
    must too, rather than record an admission refusal and gate every start
    (#2398 review)."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        assert isinstance(boot.presence, LocalPresence)
        lane_service = boot.owned_service
        assert lane_service is not None
        calls = 0

        def registry_unavailable(*_: object, **__: object) -> None:
            nonlocal calls
            calls += 1
            raise unavailable

        monkeypatch.setattr(lane_service, "observe_session", registry_unavailable)
        monkeypatch.setattr(lane_service, "clerk_volume_expectation", registry_unavailable)
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        await _until(lambda: calls >= 8, what="several beats met the unavailable registry")

        assert boot.admission_refusal is None
        assert boot.start_refusal is None
        assert boot.online
        assert stops.calls == 0
        assert boot.heartbeat is not None and not boot.heartbeat.done()
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_a_volume_gate_refusal_outlives_beats_under_the_old_session(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live lane's repair that fails the volume identity gate keeps its old
    session, and that session's beats still land. A landed beat is not the
    gate passing: starts stay gated, every beat re-runs the gate, and only a
    registration that passes it reopens starts (#2398 review)."""
    service = _service(control_dir, clock)
    try:
        provisioned, root = await _confirmed_then_closed(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        coordinator = _Coordinator(service, provisioned.clerk.clerk_id)
        coordinator.up = True
        monkeypatch.setattr(presence_module, "build_internal_client", coordinator.client)
        boot = await open_fleet_lane(settings=_remote_settings(provisioned), volume_root=root)
        assert boot is not None and boot.online
        old_session = boot.session
        stops = _StopCounter()
        boot.stop_bots = stops
        start_heartbeat(boot, interval_s=0.01)

        marker_file = volume_module.marker_path(root)
        original_marker = marker_file.read_text(encoding="utf-8")
        marker = json.loads(original_marker)
        marker["attestation_id"] = "attn_someone_else"
        marker_file.write_text(json.dumps(marker), encoding="utf-8")
        coordinator.fail_observe = True
        await _until(
            lambda: boot.admission_refusal == "clerk_volume_identity_mismatch",
            what="the repair's volume gate refused",
        )
        assert boot.session == old_session

        coordinator.fail_observe = False
        landed_from = coordinator.requests
        await _until(
            lambda: coordinator.requests >= landed_from + 12,
            what="several beats landed under the old session",
        )
        assert boot.start_refusal == "fleet_presence_refused"
        assert boot.admission_refusal == "clerk_volume_identity_mismatch"
        assert stops.calls == 0

        marker_file.write_text(original_marker, encoding="utf-8")
        await _until(lambda: boot.start_refusal is None, what="a registration passed the gate")
        assert boot.session is not None and boot.session != old_session
        assert stops.calls == 0
        await close_fleet_lane(boot)
    finally:
        service.close()
