"""A draining lane can be made quiet, and a retired one stops (issue #2351).

ADR 0063 §2's amendment has the operator make a draining lane quiet with the
panel's stop, cancel-verified-working-orders and flatten-and-stop, then the
lane proves it. Before this fix ``resolve_route`` refused every routed
operation for a draining clerk — those three included — so a lane drained
with a bot running could never answer quiet from the UI; ``force-retire``
then released the account at the deadline, and the lane read its beat's
``ClerkNotFound`` as a transient refusal, re-registered forever, and kept
trading.

The fixed contract, pinned here:

- A draining lane routes its reads and the operations the catalog declares
  ``quiesce`` — one allow-list, ``ProviderOperation.routable_while_draining``
  — and refuses everything that can open exposure.
- ``force-retire`` stays deadline-only: ADR 0063 §5's amendment keeps it the
  exit for a lane "holding an obligation its operator declines to discharge",
  preconditions untouched. What closes the hole is the lane: a typed
  ``FleetLaneRetired`` from its own beat stops its bots and ends the beat.
- An unknown clerk or a transient refusal is never read as retirement.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import get_args

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
from app.broker.alpaca.clerk.fleet_boot import (
    FleetBootRefused,
    FleetLaneBoot,
    RetirementProgress,
    _stop_bots_after_retirement,
    close_fleet_lane,
    confirm_binding,
    open_fleet_lane,
    start_heartbeat,
    stop_heartbeat,
)
from app.broker.fleet.confirmation import (
    confirmation_evidence_path,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.delivery import DeliveryResult
from app.broker.fleet.errors import (
    ClerkLaneRetired,
    ClerkNotFound,
    ClerkUnreachable,
)
from app.broker.fleet.presence import (
    FleetLaneRetired,
    FleetPresenceError,
    FleetPresenceRefused,
    LocalPresence,
    RemotePresence,
    SessionInfo,
)
from app.broker.fleet.provider import (
    Capability,
    OperationDrainAdmission,
    OperationIdempotency,
    OperationReadiness,
    ProviderOperation,
    validate_operation_catalog,
)
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.routing import CommandEnvelope, LaneRouter
from app.broker.v2panel.vocabulary import ACTION_IDS, QUIESCE_ACTION_IDS
from app.schemas.broker_v2_panel import PanelQuiesceActionRequest
from tests.broker.fleet.conftest import FrozenClock

from .test_a2_alpaca_lane import _await_beat_at, _RealServer
from .test_drained_lane_resurrection import (
    ACCOUNT,
    CLERK_TOKEN,
    _agent_app,
    _enrolled_lane,
    _lane_settings,
    _open_confirmed_lane,
    _service,
)
from .test_lane_quiet_transport import _answer, _await_confirmation_at

OPERATOR = "host-operator"
CHANGE_REF = "incident-2026-09-23-drained-lane-quiesce"

#: Every non-read operation a draining lane routes. Pinned exactly: a new
#: ``quiesce`` declaration widens what a closed door admits, so it arrives by
#: editing this set in review, never silently.
QUIESCE_OPERATION_IDS = frozenset(
    {
        "bot_cohort_flatten",
        "bot_panel_quiesce_action",
        "custody_runs_stop",
        "lane_stop_all_bots",
    }
)


def _operation(operation_id: str) -> ProviderOperation:
    return next(op for op in ALPACA_OPERATIONS if op.operation_id == operation_id)


def _path_params(operation: ProviderOperation) -> dict[str, str]:
    values = {
        "account_id": ACCOUNT,
        "sid": "sid-1",
        "profile_id": "prof_1",
        "revision": "2",
        "ticket_id": "tkt-1",
        "program_key": "prog-1",
        "order_ref": "ord-1",
        "command_id": "cmd-1",
        "transaction_id": "txn-1",
        "external_order_id": "ext-1",
    }
    return {
        name: value
        for name, value in values.items()
        if "{" + name in operation.path_template
    }


class _RecordingDelivery:
    """Stands in for the lane: records what reached it and answers 200."""

    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.bodies: list[object] = []

    async def deliver(self, request) -> DeliveryResult:
        self.delivered.append(request.operation.operation_id)
        self.bodies.append(request.json_body)
        return DeliveryResult(status_code=200, headers={}, body=b"{}")


async def _route(
    router: LaneRouter,
    clerk_id: str,
    operation: ProviderOperation,
    key: str,
    body: dict[str, object] | None = None,
) -> None:
    """Route one operation exactly as the public coordinator route does."""
    path_params = _path_params(operation)
    if operation.idempotency == OperationIdempotency.READ:
        await router.deliver_read(
            broker="alpaca",
            clerk_id=clerk_id,
            operation=operation,
            path_params=path_params,
            query={},
        )
        return
    await router.deliver_command(
        broker="alpaca",
        clerk_id=clerk_id,
        operation=operation,
        path_params=path_params,
        query={},
        body={**(body or {}), "idempotency_key": key},
        envelope=CommandEnvelope(
            capability=operation.capability.value,
            idempotency_key=key,
            expected_effective_binding_generation=1,
            target={},
        ),
    )


# ---------------------------------------------------------------------------
# Outcome 1: the drain allow-list at the routing seam
# ---------------------------------------------------------------------------


def test_the_drain_allow_list_is_the_reads_plus_the_declared_quiesce_operations() -> None:
    """One allow-list, keyed on the catalog's own classification."""
    routable_mutations = {
        op.operation_id
        for op in ALPACA_OPERATIONS
        if op.routable_while_draining and op.idempotency != OperationIdempotency.READ
    }
    assert routable_mutations == QUIESCE_OPERATION_IDS
    assert all(
        op.routable_while_draining
        for op in ALPACA_OPERATIONS
        if op.idempotency == OperationIdempotency.READ
    )


def test_a_read_declaring_quiesce_is_refused_by_the_catalog_validator() -> None:
    """``quiesce`` names mutations only; a read already routes while draining."""
    read = ProviderOperation(
        operation_id="some_read",
        method="GET",
        path_template="/some-read",
        agent_path_template="/api/some-read",
        capability=Capability.ACCOUNT_READ,
        readiness=OperationReadiness.EXECUTION,
        requires_effective_account=False,
        idempotency=OperationIdempotency.READ,
        drain_admission=OperationDrainAdmission.QUIESCE,
    )
    with pytest.raises(ValueError, match="cannot declare quiesce"):
        validate_operation_catalog(frozenset({read}))


@pytest.mark.parametrize(
    "operation_id",
    [
        # The ADR 0063 §2 acts (the panel's quiesce operation), cohort
        # flatten, stop-all, and the reads — the quiet read among them.
        "bot_panel_quiesce_action",
        "bot_cohort_flatten",
        "lane_stop_all_bots",
        "lane_account_quiet_read",
        "custody_account_snapshot",
        "bot_panel_read",
        "orders_read",
        "positions_read",
    ],
)
async def test_a_draining_lane_routes_its_quiesce_operations_and_reads(
    operation_id: str,
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before #2351 each of these refused "draining and accepts no routed
    operations" — the stop controls ADR 0063 tells the operator to use."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        service.drain_clerk(clerk_id=boot.clerk_id)
        delivery = _RecordingDelivery()
        router = LaneRouter(service=service, delivery_for=lambda broker, session: delivery)

        await _route(router, boot.clerk_id, _operation(operation_id), key=f"key-{operation_id}")

        assert delivery.delivered == [operation_id]
        await close_fleet_lane(boot)
    finally:
        service.close()


@pytest.mark.parametrize(
    "operation_id",
    [
        # Resume and continue travel here; only the quiesce split routes.
        "bot_panel_action",
        # Both accept every recovery id, resolve_execution_coverage included.
        "custody_bot_recovery_execute",
        "custody_recovery_execute",
        "bot_create",
        "custody_runs_start",
        "paper_access_confirm",
        "live_graduation_apply",
        "manual_order_ticket_put",
        "lane_go_live_release",
        "configuration_profile_create",
        "configuration_selection_apply",
    ],
)
async def test_a_draining_lane_still_refuses_every_operation_that_can_open_exposure(
    operation_id: str,
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        operation = _operation(operation_id)
        delivery = _RecordingDelivery()
        router = LaneRouter(service=service, delivery_for=lambda broker, session: delivery)
        # Served before the drain: the refusal below is the drain's alone.
        await _route(router, boot.clerk_id, operation, key=f"before-{operation_id}")
        assert delivery.delivered == [operation_id]

        service.drain_clerk(clerk_id=boot.clerk_id)
        with pytest.raises(ClerkUnreachable, match="draining"):
            await _route(router, boot.clerk_id, operation, key=f"after-{operation_id}")

        assert delivery.delivered == [operation_id]
        await close_fleet_lane(boot)
    finally:
        service.close()


def test_the_quiesce_action_set_is_one_closed_set_every_surface_derives_from() -> None:
    """The panel's quiesce ids, the recovery executor's reducing ids and the
    cohort flatten legs are one set, and it names only presented actions."""
    from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionId
    from app.schemas.broker_v2_panel import CohortFlattenActionId

    quiesce = set(QUIESCE_ACTION_IDS)
    assert quiesce <= set(ACTION_IDS)
    assert quiesce <= set(get_args(PanelQuiesceActionRequest.model_fields["action_id"].annotation))
    # Every recovery id whose executor only stops decisions, cancels owned
    # verified working orders, submits a reduction, reconciles, or discharges
    # a residue the broker proves it does not hold (#2381: a draining lane
    # stranded by EXIT_NOT_FLAT/EXIT_STUCK needs it to answer quiet).
    assert quiesce & set(get_args(RecoveryActionId)) == {
        "stop_bot_decisions",
        "cancel_verified_working_orders",
        "execute_safe_flatten",
        "reconcile_now",
        "discharge_attributed_residue",
    }
    # Rewrites fill evidence and can lift a hold a running bot is under.
    assert "resolve_execution_coverage" not in quiesce
    assert set(get_args(CohortFlattenActionId)) <= quiesce


def test_the_quiesce_panel_route_admits_exactly_the_quiesce_actions() -> None:
    """The split operation's lane handler cannot be used to resume a bot."""
    body = {
        "revision": 1,
        "concurrency_token": "tok",
        "idempotency_key": "key",
    }
    for action_id in QUIESCE_ACTION_IDS:
        assert PanelQuiesceActionRequest.model_validate({**body, "action_id": action_id})
    for action_id in set(ACTION_IDS) - set(QUIESCE_ACTION_IDS):
        with pytest.raises(ValidationError):
            PanelQuiesceActionRequest.model_validate({**body, "action_id": action_id})


@pytest.mark.parametrize("action_id", QUIESCE_ACTION_IDS)
async def test_every_quiesce_action_executes_through_the_quiesce_route(
    action_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each quiesce id reaches the one executor every panel action shares."""
    from app.routers import broker_v2_panel
    from app.schemas.broker_v2_panel import PanelActionResult

    ran: list[str] = []

    async def _record(broker, account_id, sid, request, *, operator_identity):
        ran.append(request.action_id)
        return PanelActionResult(
            action_id=request.action_id,
            receipt_id="r-1",
            recorded_at_ms=1,
            applied=True,
            revision=2,
            concurrency_token="tok-2",
            message="done",
        )

    monkeypatch.setattr(broker_v2_panel.ds, "run_action", _record)
    monkeypatch.setattr(
        broker_v2_panel, "schedule_live_projection_refresh", lambda *_args: None
    )
    app = FastAPI()
    app.include_router(broker_v2_panel.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        answered = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCOUNT}/bots/sid-1/actions/quiesce",
            json={
                "action_id": action_id,
                "revision": 1,
                "concurrency_token": "tok",
                "idempotency_key": "key",
            },
        )
    assert answered.status_code == 200, answered.text
    assert ran == [action_id]


async def test_a_draining_lane_routes_the_panels_cancel_verified_working_orders(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0063 §2 names this panel act; it travels on the quiesce operation."""
    service = _service(control_dir, clock)
    try:
        _provisioned, _root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        service.drain_clerk(clerk_id=boot.clerk_id)
        delivery = _RecordingDelivery()
        router = LaneRouter(service=service, delivery_for=lambda broker, session: delivery)

        await _route(
            router,
            boot.clerk_id,
            _operation("bot_panel_quiesce_action"),
            key="cancel-1",
            body={"action_id": "cancel_verified_working_orders", "revision": 1},
        )

        assert delivery.delivered == ["bot_panel_quiesce_action"]
        delivered_body = delivery.bodies[0]
        assert isinstance(delivered_body, dict)
        assert delivered_body["action_id"] == "cancel_verified_working_orders"
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_the_quiesce_panel_route_refuses_a_resume_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import broker_v2_panel

    ran: list[str] = []

    async def _record(broker, account_id, sid, request, *, operator_identity):
        ran.append(request.action_id)
        raise AssertionError("a refused action must not execute")

    monkeypatch.setattr(broker_v2_panel.ds, "run_action", _record)
    app = FastAPI()
    app.include_router(broker_v2_panel.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        refused = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCOUNT}/bots/sid-1/actions/quiesce",
            json={
                "action_id": "resume",
                "revision": 1,
                "concurrency_token": "tok",
                "idempotency_key": "key",
            },
        )
    assert refused.status_code == 422
    assert ran == []


# ---------------------------------------------------------------------------
# Outcome 3: a lane that learns its own retirement stops its bots
# ---------------------------------------------------------------------------


async def test_a_force_retired_lane_stops_its_bots_and_ends_its_beat(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's probe, inverted.

    A bot is still running when the drain deadline passes; the operator
    force-retires (ADR 0063 §5 keeps that deadline-only). The lane's next
    beat hears the typed retirement, stops its bots through the lane-wide
    stop, tombstones its evidence, never re-registers, and ends its beat.
    """
    service = _service(control_dir, clock)
    try:
        _provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        bot_running = [True]
        stop_calls: list[int] = []

        async def probe():
            return _answer(clock, runner_idle=not bot_running[0])

        async def stop_bots() -> bool:
            stop_calls.append(clock())
            bot_running[0] = False
            return True

        registrations: list[str] = []
        real_register = boot.presence.register

        async def counting_register(**kwargs):
            registrations.append(kwargs["agent_instance_id"])
            return await real_register(**kwargs)

        monkeypatch.setattr(boot.presence, "register", counting_register)
        boot.lane_quiet_probe = probe
        boot.stop_bots = stop_bots
        start_heartbeat(boot, interval_s=0.05)

        drained = service.drain_clerk(clerk_id=boot.clerk_id)
        clock.advance(1_000)
        await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")
        assert boot.draining and not boot.retired
        # The stop control reaches the draining lane (BUG 1 in the issue).
        stop_all = _operation("lane_stop_all_bots")
        service.resolve_route(
            broker="alpaca",
            clerk_id=boot.clerk_id,
            readiness=stop_all.readiness,
            routable_while_draining=stop_all.routable_while_draining,
        )

        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        answer = await _await_confirmation_at(service, boot.clerk_id, clock)
        assert not answer.is_quiet  # the lane says a bot is still running
        retired = service.force_retire_clerk(
            clerk_id=boot.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
        assert retired.lifecycle_state == StoredLifecycleState.RETIRED

        assert boot.heartbeat is not None
        beat = boot.heartbeat
        await asyncio.wait_for(asyncio.shield(beat), timeout=5.0)

        assert beat.exception() is None
        assert boot.retired
        assert len(stop_calls) == 1
        assert bot_running == [False]
        assert registrations == []  # retirement is not a refusal to repair
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "draining"
        with pytest.raises(ClerkNotFound):
            service.resolve_route(
                broker="alpaca",
                clerk_id=boot.clerk_id,
                readiness=stop_all.readiness,
                routable_while_draining=stop_all.routable_while_draining,
            )
        await close_fleet_lane(boot)
    finally:
        service.close()


async def test_an_incomplete_stop_is_retried_on_a_later_beat(tmp_path: Path) -> None:
    """A bot that refused to stop keeps the beat alive until it does."""
    attempts: list[int] = []

    async def stop_bots() -> bool:
        attempts.append(len(attempts))
        return len(attempts) >= 3

    boot = _stub_boot(tmp_path, _RetiredPresence())
    boot.stop_bots = stop_bots
    start_heartbeat(boot, interval_s=0.02)
    assert boot.heartbeat is not None
    await asyncio.wait_for(asyncio.shield(boot.heartbeat), timeout=5.0)

    assert boot.retired
    assert len(attempts) == 3
    await stop_heartbeat(boot)


async def test_a_stop_that_keeps_failing_backs_off_instead_of_writing_a_receipt_every_beat(
    tmp_path: Path,
) -> None:
    """Each attempt writes a lane-stop receipt, so retries double their gap."""
    attempts: list[int] = []

    async def never_stops() -> bool:
        attempts.append(1)
        return False

    boot = _stub_boot(tmp_path, _RetiredPresence())
    boot.stop_bots = never_stops
    progress = RetirementProgress()
    for _ in range(15):
        await _stop_bots_after_retirement(boot, progress)

    # Attempts on beats 1, 3, 6 and 11: gaps of 1, 2 and 4 beats.
    assert len(attempts) == 4
    assert not progress.bots_stopped


async def test_a_missing_stop_hook_keeps_the_retired_beat_alive_until_it_is_installed(
    tmp_path: Path,
) -> None:
    """A hook the composition root has not installed yet is not "nothing to stop"."""
    boot = _stub_boot(tmp_path, _RetiredPresence())
    start_heartbeat(boot, interval_s=0.02)
    for _ in range(100):
        if boot.retired:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    assert boot.retired
    assert boot.heartbeat is not None and not boot.heartbeat.done()

    stops: list[int] = []

    async def stop_bots() -> bool:
        stops.append(1)
        return True

    boot.stop_bots = stop_bots
    await asyncio.wait_for(asyncio.shield(boot.heartbeat), timeout=5.0)
    assert stops == [1]
    await stop_heartbeat(boot)


async def test_an_unwritable_volume_still_stops_the_bots_and_retries_the_tombstone(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review probe: a read-only evidence directory used to kill the beat
    (PermissionError) before a single bot stopped, with the start gate open."""
    service = _service(control_dir, clock)
    evidence_dir: Path | None = None
    try:
        _provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        evidence_dir = confirmation_evidence_path(root).parent
        stops: list[int] = []

        async def stop_bots() -> bool:
            stops.append(1)
            return True

        boot.stop_bots = stop_bots
        start_heartbeat(boot, interval_s=0.05)
        drained = service.drain_clerk(clerk_id=boot.clerk_id)
        clock.advance(1_000)
        await _await_beat_at(service, boot.clerk_id, clock, reported_state="binding_confirmed")
        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        # Force-retire a lane whose drain mark never landed: the tombstone is
        # still owed, and the disk now refuses it.
        unmarked = replace(read_confirmation_evidence(root), lifecycle_state="provisioned")
        write_confirmation_evidence(root, unmarked)
        evidence_dir.chmod(0o555)
        service.force_retire_clerk(
            clerk_id=boot.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
        for _ in range(100):
            if stops:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)

        beat = boot.heartbeat
        assert beat is not None and not beat.done()  # alive: the tombstone is owed
        assert boot.retired and boot.draining
        assert stops == [1]
        assert boot.retirement is not None and not boot.retirement.tombstone_written

        evidence_dir.chmod(0o755)
        await asyncio.wait_for(asyncio.shield(beat), timeout=5.0)
        assert beat.exception() is None
        marked = read_confirmation_evidence(root)
        assert marked is not None and marked.lifecycle_state == "draining"
        await close_fleet_lane(boot)
    finally:
        if evidence_dir is not None:
            evidence_dir.chmod(0o755)
        service.close()


async def test_a_retirement_first_heard_at_confirmation_is_learned(tmp_path: Path) -> None:
    """Symmetric with the drain lesson at confirmation (#2155)."""

    class _RefusingConfirmation(_RetiredPresence):
        async def confirm(self, **_kwargs):
            raise FleetLaneRetired("Clerk x is retired; a retired lane confirms nothing.")

    boot = _stub_boot(tmp_path, _RefusingConfirmation())
    with pytest.raises(FleetBootRefused, match="retired"):
        await confirm_binding(
            boot,
            external_account_id=ACCOUNT,
            binding_generation=1,
            effective_profile_id="prof_1",
            effective_revision=2,
        )
    assert boot.retired and boot.draining


class _RetiredPresence:
    """A coordinator that answers every beat with the typed retirement."""

    def __init__(self) -> None:
        self.registrations = 0

    async def observe(self, **_kwargs) -> str:
        raise FleetLaneRetired("Clerk x is retired.")

    async def register(self, **_kwargs) -> SessionInfo:
        self.registrations += 1
        raise FleetLaneRetired("Clerk x is retired.")


class _UnknownClerkPresence:
    """A coordinator whose refusal is NOT retirement: unavailability on the
    beat and on every re-registration, which starts at the volume identity
    gate's expectation (#2320's one registration unit)."""

    def __init__(self) -> None:
        self.registrations = 0

    async def observe(self, **_kwargs) -> str:
        raise FleetPresenceError("The fleet coordinator refused: clerk_not_found")

    async def expectation(self, **_kwargs) -> dict[str, object]:
        self.registrations += 1
        raise FleetPresenceError("The fleet coordinator refused: clerk_not_found")

    async def register(self, **_kwargs) -> SessionInfo:
        raise AssertionError("registration never runs past a failed identity gate")


def _stub_boot(tmp_path: Path, presence: object) -> FleetLaneBoot:
    root = tmp_path / "volume"
    root.mkdir(exist_ok=True)
    return FleetLaneBoot(
        presence=presence,  # type: ignore[arg-type]
        clerk_id="clrk_aaaaaaaaaaaaaaaaaaaaaaaa",
        worker_key="wkrk_00000000000000000000000000000000",
        volume_root=root,
        registry_id="",
        volume_id="",
        session=SessionInfo(agent_instance_id="agnt_0000000000000000000000aa", routing_epoch=1),
    )


async def test_an_unknown_clerk_or_transient_refusal_is_not_read_as_retirement(
    tmp_path: Path,
) -> None:
    """Only the typed retirement stops bots; anything else is repaired."""
    stop_calls: list[int] = []

    async def stop_bots() -> bool:
        stop_calls.append(1)
        return True

    presence = _UnknownClerkPresence()
    boot = _stub_boot(tmp_path, presence)
    boot.stop_bots = stop_bots
    start_heartbeat(boot, interval_s=0.02)
    for _ in range(100):
        if presence.registrations >= 3:
            break
        await asyncio.sleep(0.02)

    assert presence.registrations >= 3  # still repairing, as before
    assert not boot.retired
    assert stop_calls == []
    assert boot.heartbeat is not None and not boot.heartbeat.done()
    await stop_heartbeat(boot)


async def test_the_coordinator_refuses_a_retired_lane_with_the_typed_lesson(
    control_dir: Path, clock: FrozenClock, tmp_path: Path
) -> None:
    """Registration and the beat both name retirement; an unknown id does not."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        service.retire_clerk(clerk_id=provisioned.clerk.clerk_id)  # never served
        with pytest.raises(ClerkLaneRetired):
            service.observe_session(
                clerk_id=provisioned.clerk.clerk_id,
                agent_instance_id="agnt_0000000000000000000000aa",
            )
        with pytest.raises(ClerkLaneRetired):
            service.register_agent_session(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                fleet_protocol_version=2,
            )
        with pytest.raises(ClerkNotFound) as unknown:
            service.observe_session(
                clerk_id="clrk_bbbbbbbbbbbbbbbbbbbbbbbb",
                agent_instance_id="agnt_0000000000000000000000aa",
            )
        assert not isinstance(unknown.value, ClerkLaneRetired)

        presence = LocalPresence(service, volume_root=Path(provisioned.clerk.volume_root))
        with pytest.raises(FleetLaneRetired) as translated:
            await presence.observe(
                clerk_id=provisioned.clerk.clerk_id,
                agent_instance_id="agnt_0000000000000000000000aa",
            )
        assert not isinstance(translated.value, FleetPresenceError)
    finally:
        service.close()


async def test_remote_presence_carries_the_retirement_lesson_over_the_wire(
    control_dir: Path, clock: FrozenClock, tmp_path: Path
) -> None:
    service = _service(control_dir, clock)
    server = None
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        service.retire_clerk(clerk_id=provisioned.clerk.clerk_id)
        app = _agent_app(provisioned.clerk.clerk_id)
        app.state.fleet_service = service
        server = _RealServer(app)
        server.start()
        presence = RemotePresence(base_url=server.base_url, agent_service_token=CLERK_TOKEN)
        with pytest.raises(FleetLaneRetired) as refused:
            await presence.observe(
                clerk_id=provisioned.clerk.clerk_id,
                agent_instance_id="agnt_0000000000000000000000aa",
            )
        # Not the unavailability family FR-066's offline fallback catches.
        assert not isinstance(refused.value, FleetPresenceError)
    finally:
        if server is not None:
            server.stop()
        service.close()


def _not_found_coordinator_app() -> FastAPI:
    coordinator = FastAPI()

    @coordinator.post("/internal/fleet/sessions/observe")
    async def observe() -> JSONResponse:
        return JSONResponse(
            {"reason": "clerk_not_found", "message": "No clerk carries identity 'x'."},
            status_code=404,
        )

    return coordinator


async def test_remote_presence_reads_a_plain_not_found_as_a_refusal_not_retirement() -> None:
    """Retirement is learned only from its typed code (#2351); a plain
    not-found is the coordinator's refusal — never retirement, and since
    #2320 never unavailability either (FR-066 rides out only an outage)."""
    server = _RealServer(_not_found_coordinator_app())
    server.start()
    try:
        presence = RemotePresence(base_url=server.base_url, agent_service_token=CLERK_TOKEN)
        with pytest.raises(FleetPresenceRefused) as refused:
            await presence.observe(
                clerk_id="clrk_aaaaaaaaaaaaaaaaaaaaaaaa",
                agent_instance_id="agnt_0000000000000000000000aa",
            )
        assert not isinstance(refused.value, FleetLaneRetired | FleetPresenceError)
        assert refused.value.coordinator_reason == "clerk_not_found"
    finally:
        server.stop()


async def test_a_retired_lane_restarting_on_an_unwritable_volume_still_refuses_its_boot(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-time site: a failed tombstone is logged, and the boot still
    closes the lane and refuses rather than escaping as a raw OSError."""
    service = _service(control_dir, clock)
    evidence_dir: Path | None = None
    try:
        provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        await close_fleet_lane(boot)
        drained = service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        service.force_retire_clerk(
            clerk_id=provisioned.clerk.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
        evidence_dir = confirmation_evidence_path(root).parent
        evidence_dir.chmod(0o555)

        with pytest.raises(FleetBootRefused, match="retired"):
            await open_fleet_lane(
                settings=_lane_settings(control_dir, provisioned), volume_root=root
            )
    finally:
        if evidence_dir is not None:
            evidence_dir.chmod(0o755)
        service.close()


async def test_a_retired_lane_restarting_refuses_its_boot_and_marks_its_evidence(
    control_dir: Path, clock: FrozenClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot-time half: a retired lane never boots its binding back."""
    service = _service(control_dir, clock)
    try:
        provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        await close_fleet_lane(boot)
        drained = service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        assert drained.drain_deadline_at_ms is not None
        clock.advance(drained.drain_deadline_at_ms - clock() + 1)
        service.force_retire_clerk(
            clerk_id=provisioned.clerk.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
        evidence = read_confirmation_evidence(root)
        assert evidence is not None and evidence.lifecycle_state == "provisioned"

        with pytest.raises(FleetBootRefused, match="retired"):
            await open_fleet_lane(
                settings=_lane_settings(control_dir, provisioned), volume_root=root
            )

        marked = read_confirmation_evidence(root)
        assert marked is not None and marked.lifecycle_state == "draining"
    finally:
        service.close()
