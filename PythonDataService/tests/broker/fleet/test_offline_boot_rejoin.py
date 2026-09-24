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
from app.broker.fleet.confirmation import read_confirmation_evidence
from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.presence import FleetPresenceError, FleetPresenceRefused, RemotePresence
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.service import FleetControlService, ProvisionedClerk
from app.config import FleetSettings
from app.services.bot_runner import RunAdmissionRefusedError, refused_lane_start_gate
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
    """The coordinator as the lane's transport sees it: down, or up."""

    def __init__(self, service: FleetControlService, clerk_id: str) -> None:
        self._app: FastAPI = _agent_app(clerk_id)
        self._app.state.fleet_service = service
        self.up = False

    def client(self, **_: object) -> httpx.AsyncClient:
        if not self.up:

            def refuse(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused", request=request)

            return httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://coordinator"
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
    [("alpaca-fleet.8", 2), (fleet_boot._ADAPTER.adapter_version, 3)],
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


async def test_an_offline_lane_refused_on_rejoin_stops_its_bots_until_admitted(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator came back as a build this lane may not join: it runs
    no bot on a refusal, keeps asking, and reopens once it is admitted."""
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
        gate = refused_lane_start_gate(lambda: boot.admission_refusal)
        gate("bot-1")  # an offline lane nobody has refused may start bots
        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", 99)
        start_heartbeat(boot, interval_s=0.02)

        coordinator.up = True
        await _until(lambda: stops == [1], what="the refused lane stopped its bots")
        assert boot.admission_refusal == "fleet_protocol_incompatible"
        assert not boot.online
        with pytest.raises(RunAdmissionRefusedError):
            gate("bot-1")
        evidence = read_confirmation_evidence(root)
        assert evidence is not None
        assert evidence.lifecycle_state == StoredLifecycleState.PROVISIONED.value

        monkeypatch.setattr(fleet_boot, "FLEET_PROTOCOL_VERSION", 2)
        await _until(lambda: boot.online, what="the lane rejoined once admitted")
        assert boot.admission_refusal is None
        gate("bot-1")
        assert stops == [1]  # stopped once, not once per refused beat
        await close_fleet_lane(boot)
    finally:
        service.close()
