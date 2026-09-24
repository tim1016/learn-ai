"""A draining lane answers lane quiet on its heartbeat (#2154).

The coordinator half (``confirm_lane_quiet``, ``_require_lane_quiet``) is
pinned in ``test_lane_quiet_confirmation.py``. This module pins the half that
makes a real lane able to answer: the beat sends a fresh answer while the lane
is draining and never otherwise, a failed observation cannot end the beat,
both transports reach the same entry point, and a real lane that has gone
quiet now retires on the normal path rather than through ``force-retire``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.broker.alpaca.clerk.fleet_boot import (
    FleetLaneBoot,
    LaneQuietAnswer,
    close_fleet_lane,
    lane_quiet_probe,
    start_heartbeat,
)
from app.broker.alpaca.clerk.sqlite.lane_quiet import AccountQuietObservation
from app.broker.fleet.errors import ClerkLaneQuietUnproven
from app.broker.fleet.presence import RemotePresence
from app.broker.fleet.records import (
    LaneConfirmationState,
    LaneQuietConfirmationRecord,
    StoredLifecycleState,
)
from app.broker.fleet.service import FleetControlService
from tests.broker.fleet.conftest import FrozenClock

from .test_a2_alpaca_lane import _await_beat_at, _RealServer
from .test_drained_lane_resurrection import (
    ACCOUNT,
    CLERK_TOKEN,
    _agent_app,
    _enrolled_lane,
    _open_confirmed_lane,
    _service,
)

OPERATOR = "host-operator"
CHANGE_REF = "incident-2026-09-22-lane-quiet"


def _answer(clock: FrozenClock, **unsatisfied: bool) -> LaneQuietAnswer:
    conditions = {
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": True,
        "intents_resolved": True,
    }
    conditions.update(unsatisfied)
    return LaneQuietAnswer(observed_at_ms=clock(), **conditions)


def _probe_answering(clock: FrozenClock, calls: list[int], **unsatisfied: bool):
    async def probe() -> LaneQuietAnswer:
        calls.append(clock())
        return _answer(clock, **unsatisfied)

    return probe


async def _await_confirmation_at(
    service: FleetControlService, clerk_id: str, clock: FrozenClock, *, deadline_s: float = 5.0
) -> LaneQuietConfirmationRecord:
    """Wait for a confirmation observed at the current frozen instant."""
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + deadline_s
    while True:
        confirmation = service.read_lane_quiet_confirmation(clerk_id=clerk_id)
        if confirmation is not None and confirmation.observed_at_ms == clock():
            return confirmation
        if loop.time() > give_up_at:
            raise AssertionError(f"no confirmation at {clock()}; newest was {confirmation}")
        await asyncio.sleep(0.02)


async def _drained_and_released(
    service: FleetControlService, boot: FleetLaneBoot, clock: FrozenClock
) -> None:
    """Drain, let the beat learn it, wait out the deadline, release the account.

    Every gate ahead of lane quiet is then green, so a retirement refusal can
    only be the lane-quiet gate.
    """
    drained = service.drain_clerk(clerk_id=boot.clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(1_000)
    await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")
    assert boot.draining
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    service.release_assignment(
        broker="alpaca",
        external_account_id=ACCOUNT,
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )


async def test_a_quiet_draining_lane_confirms_on_its_beat_and_retires_unforced(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline: a real lane's heartbeat opens the normal retirement path."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        calls: list[int] = []
        boot.lane_quiet_probe = _probe_answering(clock, calls)
        start_heartbeat(boot, interval_s=0.05)
        await _drained_and_released(service, boot, clock)

        confirmation = await _await_confirmation_at(service, boot.clerk_id, clock)
        assert confirmation.is_quiet
        retired = service.retire_clerk(clerk_id=boot.clerk_id)

        assert retired.lifecycle_state == StoredLifecycleState.RETIRED
        assert retired.lane_confirmation == LaneConfirmationState.PRESENT
        assert retired.retire_operator is None
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_an_outstanding_condition_reaches_the_gate_and_is_named(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "not yet" is sent, recorded, and names what the operator must clear."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        boot.lane_quiet_probe = _probe_answering(clock, [], account_flat=False)
        start_heartbeat(boot, interval_s=0.05)
        await _drained_and_released(service, boot, clock)

        confirmation = await _await_confirmation_at(service, boot.clerk_id, clock)
        assert not confirmation.is_quiet
        with pytest.raises(ClerkLaneQuietUnproven, match="the account or the lane's custody is not flat"):
            service.retire_clerk(clerk_id=boot.clerk_id)
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_a_serving_lane_never_observes_or_confirms(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker reads cost nothing until the lane has learned its drain."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        calls: list[int] = []
        boot.lane_quiet_probe = _probe_answering(clock, calls)
        start_heartbeat(boot, interval_s=0.05)
        for _ in range(3):
            clock.advance(1_000)
            await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")

        assert calls == []
        assert service.read_lane_quiet_confirmation(clerk_id=boot.clerk_id) is None
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["raises", "no_answer", "hangs"])
async def test_a_failed_observation_sends_nothing_and_the_beat_goes_on(
    failure: str,
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither an exception, an unreadable broker nor a hung one may end the
    lane's presence.

    A lane whose beat died or stalled would be projected unreachable while it
    still holds the account; and a missing answer is silence, which the gate
    refuses.
    """
    from app.broker.alpaca.clerk import fleet_boot

    monkeypatch.setattr(fleet_boot, "LANE_QUIET_OBSERVATION_TIMEOUT_S", 0.05)
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        attempts: list[int] = []

        async def probe() -> LaneQuietAnswer | None:
            attempts.append(clock())
            if failure == "raises":
                raise RuntimeError("the clerk's ledger is unreadable")
            if failure == "hangs":
                await asyncio.Event().wait()
            return None

        boot.lane_quiet_probe = probe
        start_heartbeat(boot, interval_s=0.05)
        await _drained_and_released(service, boot, clock)
        for _ in range(2):
            clock.advance(1_000)
            await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")

        assert attempts
        assert boot.heartbeat is not None and not boot.heartbeat.done()
        assert service.read_lane_quiet_confirmation(clerk_id=boot.clerk_id) is None
        with pytest.raises(ClerkLaneQuietUnproven, match="has not confirmed"):
            service.retire_clerk(clerk_id=boot.clerk_id)
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize("running", [(True, False), (False, True)])
async def test_a_bot_running_on_either_side_of_the_account_read_is_not_idle(
    running: tuple[bool, bool],
) -> None:
    """The runner is read before and after the broker, and both must be idle."""
    reads = list(running)

    async def observe_account() -> AccountQuietObservation:
        return AccountQuietObservation(
            observed_at_ms=5, broker_work_ended=True, account_flat=True, intents_resolved=True
        )

    probe = lane_quiet_probe(bots_running=lambda: reads.pop(0), observe_account=observe_account)
    answer = await probe()

    assert answer is not None
    assert not answer.runner_idle
    assert answer.outstanding == ("a bot is still running",)


async def test_an_unreadable_account_composes_to_no_answer() -> None:
    async def observe_account() -> None:
        return None

    probe = lane_quiet_probe(bots_running=lambda: False, observe_account=observe_account)

    assert await probe() is None


def _lane_quiet_body(clerk_id: str, session, observed_at_ms: int) -> dict[str, object]:
    return {
        "clerk_id": clerk_id,
        "agent_instance_id": session.agent_instance_id,
        "routing_epoch": session.routing_epoch,
        "observed_at_ms": observed_at_ms,
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": False,
        "intents_resolved": True,
    }


async def test_the_internal_route_records_the_answer_and_refuses_a_partial_one(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Strict on the wire: a missing or loosely-typed condition is malformed.

    A default would let an older or broken agent assert a condition it never
    observed.
    """
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        clerk_id = provisioned.clerk.clerk_id
        app = _agent_app(clerk_id)
        app.state.fleet_service = service
        session = service.register_agent_session(
            clerk_id=clerk_id, worker_key=provisioned.clerk.worker_key, fleet_protocol_version=2
        )
        service.drain_clerk(clerk_id=clerk_id)
        clock.advance(1_000)
        headers = {"X-Fleet-Clerk-Id": clerk_id, "X-Fleet-Agent-Token": CLERK_TOKEN}
        body = _lane_quiet_body(clerk_id, session, clock())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            recorded = await client.post(
                "/internal/fleet/lanes/confirm-quiet", json=body, headers=headers
            )
            assert recorded.status_code == 200
            assert recorded.json() == {
                "quiet": False,
                "outstanding": ["the account or the lane's custody is not flat"],
            }

            partial = {key: value for key, value in body.items() if key != "account_flat"}
            missing = await client.post(
                "/internal/fleet/lanes/confirm-quiet", json=partial, headers=headers
            )
            assert missing.status_code == 422

            loose = await client.post(
                "/internal/fleet/lanes/confirm-quiet",
                json={**body, "account_flat": "true"},
                headers=headers,
            )
            assert loose.status_code == 422

            other_clerk = await client.post(
                "/internal/fleet/lanes/confirm-quiet",
                json=body,
                headers={**headers, "X-Fleet-Clerk-Id": "clrk_bbbbbbbbbbbbbbbbbbbbbbbb"},
            )
            assert other_clerk.status_code in {401, 403}
        confirmation = service.read_lane_quiet_confirmation(clerk_id=clerk_id)
        assert confirmation is not None and not confirmation.account_flat
    finally:
        service.close()


async def test_remote_presence_reaches_the_same_entry_point(
    control_dir: Path, clock: FrozenClock
) -> None:
    """The HTTP transport's path and payload match the route, end to end."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        clerk_id = provisioned.clerk.clerk_id
        app = _agent_app(clerk_id)
        app.state.fleet_service = service
        session = service.register_agent_session(
            clerk_id=clerk_id, worker_key=provisioned.clerk.worker_key, fleet_protocol_version=2
        )
        service.drain_clerk(clerk_id=clerk_id)
        clock.advance(1_000)
        server = _RealServer(app)
        server.start()
        try:
            presence = RemotePresence(base_url=server.base_url, agent_service_token=CLERK_TOKEN)
            await presence.confirm_lane_quiet(
                clerk_id=clerk_id,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
                observed_at_ms=clock(),
                runner_idle=True,
                broker_work_ended=False,
                account_flat=True,
                intents_resolved=True,
            )
        finally:
            server.stop()
        confirmation = service.read_lane_quiet_confirmation(clerk_id=clerk_id)
        assert confirmation is not None
        assert confirmation.outstanding == ("a working order on the account has not ended",)
    finally:
        service.close()


async def test_a_lane_that_cannot_answer_says_so_when_it_learns_its_drain(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lane with no clerk never confirms; the log says so once, not never."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        start_heartbeat(boot, interval_s=0.05)
        with caplog.at_level("WARNING", logger="app.broker.alpaca.clerk.fleet_boot"):
            await _drained_and_released(service, boot, clock)
            clock.advance(1_000)
            await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")

        unanswerable = [
            record for record in caplog.records
            if getattr(record, "action", None) == "lane_quiet_unanswerable"
        ]
        assert len(unanswerable) == 1
        await close_fleet_lane(boot)
    finally:
        service.close()
