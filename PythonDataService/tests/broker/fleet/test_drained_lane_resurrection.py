"""#2155: a drained lane cannot resurrect its binding by restarting.

The exposure was three-fold, and this module walks each fold closing:

- the coordinator refused only ``retired`` — registration (and, weaker,
  confirmation) let a draining lane back in, so it never learned anything;
- ``ConfirmationEvidence`` carried no lifecycle and was never re-authored,
  so it vouched forever for the grant it recorded;
- ``offline_boot_matches`` trusted that evidence unconditionally, so a
  restart during a coordinator outage booted the drained binding straight
  back up.

The fix's shape: the heartbeat *answers* with the clerk's lifecycle, and
the typed registration refusal is distinguishable from unreachability —
either way the lane marks its own evidence, and marked evidence boots
nothing, online or off.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    confirmation_evidence_path,
    evidence_vouches_for,
    mark_confirmation_evidence_draining,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.errors import ClerkLaneDraining
from app.broker.fleet.presence import (
    FleetLaneDraining,
    FleetPresenceError,
    LocalPresence,
    RemotePresence,
)
from app.broker.fleet.records import StoredLifecycleState
from app.broker.fleet.service import FleetControlService, FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from app.config import FleetSettings
from tests.broker.fleet.conftest import FrozenClock

from .test_a2_alpaca_lane import (
    _await_beat_at,
    _boot_service_on_the_test_clock,
    _enrolled_lane,
    _fence_satisfying_roots,
    _RealServer,
)

ACCOUNT = "abcdef01-1234-abcd-5678-ef0123456789"
CLERK_TOKEN = "svct_" + "2" * 32
CLERK = "clrk_aaaaaaaaaaaaaaaaaaaaaaaa"


def _service(control_dir: Path, clock: FrozenClock) -> FleetControlService:
    return FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=production_provider_adapters(),
        clock=clock,
    )


def _write_v1_evidence(root: Path, *, clerk_id: str, volume_id: str) -> None:
    """A pre-#2155 evidence file, byte-shaped exactly as schema v1 wrote it."""
    payload = {
        "schema_version": 1,
        "clerk_id": clerk_id,
        "volume_id": volume_id,
        "registry_id": "fltr_v1evidence0000000000000",
        "assignment_generation": 1,
        "canonical_account_id": ACCOUNT,
        "binding_generation": 1,
        "effective_profile_id": "prof_1",
        "effective_revision": 2,
        "confirmed_at_ms": 1,
        "agent_instance_id": "agnt_0000000000000000000000aa",
        "routing_epoch": 1,
    }
    target = confirmation_evidence_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), "utf-8")


def _evidence(clerk_id: str, volume_id: str) -> ConfirmationEvidence:
    return ConfirmationEvidence(
        clerk_id=clerk_id,
        volume_id=volume_id,
        registry_id="fltr_resurrection00000000000",
        assignment_generation=1,
        canonical_account_id=ACCOUNT,
        binding_generation=1,
        effective_profile_id="prof_1",
        effective_revision=2,
        confirmed_at_ms=1,
        agent_instance_id="agnt_0000000000000000000000aa",
        routing_epoch=1,
    )


def _agent_app(clerk_id: str) -> FastAPI:
    """A coordinator app serving the internal fleet surface over ``service``."""
    from app.routers.internal_fleet import router as internal_router
    from app.utils.error_handlers import install_fleet_control_error_handler

    app = FastAPI()
    app.state.fleet_agent_tokens_text = json.dumps({clerk_id: CLERK_TOKEN})
    app.include_router(internal_router)
    install_fleet_control_error_handler(app)
    return app


# ---------------------------------------------------------------------------
# The evidence file: schema v2, v1 back-compat, the tombstone, the voucher
# ---------------------------------------------------------------------------


def test_a_v1_evidence_file_reads_back_as_provisioned_and_still_vouches(
    tmp_path: Path,
) -> None:
    """The legacy volume keeps its FR-066 story: v1 parses, v1 vouches."""
    root = tmp_path / "clerk"
    root.mkdir()
    _write_v1_evidence(root, clerk_id=CLERK, volume_id="vol_x")
    evidence = read_confirmation_evidence(root)
    assert evidence is not None
    assert evidence.lifecycle_state == "provisioned"
    assert evidence_vouches_for(
        evidence,
        canonical_account_id=ACCOUNT,
        effective_profile_id="prof_1",
        effective_revision=2,
        binding_generation=1,
    )


def test_written_evidence_is_schema_two_and_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "clerk"
    root.mkdir()
    write_confirmation_evidence(root, _evidence(CLERK, "vol_x"))
    on_disk = json.loads(confirmation_evidence_path(root).read_text("utf-8"))
    assert on_disk["schema_version"] == 2
    assert on_disk["lifecycle_state"] == "provisioned"
    evidence = read_confirmation_evidence(root)
    assert evidence is not None
    assert evidence.lifecycle_state == "provisioned"


def test_marking_drained_preserves_the_grant_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """The tombstone is also the audit record of what was drained."""
    root = tmp_path / "clerk"
    root.mkdir()
    written = _evidence(CLERK, "vol_x")
    write_confirmation_evidence(root, written)
    assert mark_confirmation_evidence_draining(root) is True
    drained = read_confirmation_evidence(root)
    assert drained is not None
    assert drained.lifecycle_state == "draining"
    assert drained.tuple_key() == written.tuple_key()
    assert drained.binding_generation == written.binding_generation
    assert drained.confirmed_at_ms == written.confirmed_at_ms
    # Re-learning the same drain changes nothing.
    assert mark_confirmation_evidence_draining(root) is True
    assert read_confirmation_evidence(root) == drained
    # A volume that never confirmed anything has nothing to mark — not an
    # error, and never a tombstone invented from nothing.
    bare = tmp_path / "bare"
    bare.mkdir()
    assert mark_confirmation_evidence_draining(bare) is False


def test_drained_evidence_vouches_for_nothing(tmp_path: Path) -> None:
    """The exact grant, the exact generation — the tombstone refuses all of it."""
    root = tmp_path / "clerk"
    root.mkdir()
    write_confirmation_evidence(root, _evidence(CLERK, "vol_x"))
    mark_confirmation_evidence_draining(root)
    drained = read_confirmation_evidence(root)
    assert drained is not None
    assert not evidence_vouches_for(
        drained,
        canonical_account_id=ACCOUNT,
        effective_profile_id="prof_1",
        effective_revision=2,
        binding_generation=1,
    )


def test_an_invalid_lifecycle_state_refuses_to_parse(tmp_path: Path) -> None:
    root = tmp_path / "clerk"
    root.mkdir()
    write_confirmation_evidence(root, _evidence(CLERK, "vol_x"))
    target = confirmation_evidence_path(root)
    payload = json.loads(target.read_text("utf-8"))
    payload["lifecycle_state"] = "zombie"
    target.write_text(json.dumps(payload), "utf-8")
    from app.broker.fleet.confirmation import ConfirmationEvidenceError

    with pytest.raises(ConfirmationEvidenceError, match="lifecycle_state"):
        read_confirmation_evidence(root)


def test_a_schema_one_claim_with_schema_two_fields_refuses(tmp_path: Path) -> None:
    root = tmp_path / "clerk"
    root.mkdir()
    _write_v1_evidence(root, clerk_id=CLERK, volume_id="vol_x")
    target = confirmation_evidence_path(root)
    payload = json.loads(target.read_text("utf-8"))
    payload["lifecycle_state"] = "provisioned"
    target.write_text(json.dumps(payload), "utf-8")
    from app.broker.fleet.confirmation import ConfirmationEvidenceError

    with pytest.raises(ConfirmationEvidenceError, match="claims schema version 1"):
        read_confirmation_evidence(root)


# ---------------------------------------------------------------------------
# The coordinator: the typed lesson, and the heartbeat that carries it
# ---------------------------------------------------------------------------


def test_registration_refuses_a_draining_clerk_with_the_typed_lesson(
    control_dir: Path, clock: FrozenClock
) -> None:
    """DRAINING refuses with its own code — not ClerkNotFound, not a 503."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        with pytest.raises(ClerkLaneDraining, match="never returns") as refused:
            service.register_agent_session(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                fleet_protocol_version=2,
            )
        assert refused.value.reason == "clerk_lane_draining"
        assert refused.value.status_code == 409
    finally:
        service.close()


def test_the_heartbeat_answers_with_the_lifecycle_a_live_lane_learns(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Observing a draining clerk succeeds — and its answer says 'draining'."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        session = service.register_agent_session(
            clerk_id=provisioned.clerk.clerk_id,
            worker_key=provisioned.clerk.worker_key,
            fleet_protocol_version=2,
        )
        before = service.observe_session(
            clerk_id=provisioned.clerk.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_state="binding_confirmed",
        )
        assert before.touched is True
        assert before.lifecycle_state is StoredLifecycleState.PROVISIONED
        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        clock.advance(1_000)
        after = service.observe_session(
            clerk_id=provisioned.clerk.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_state="binding_confirmed",
        )
        assert after.touched is True
        assert after.lifecycle_state is StoredLifecycleState.DRAINING
    finally:
        service.close()


def _drain_mid_flight(
    service: FleetControlService, monkeypatch: pytest.MonkeyPatch, clerk_id: str
) -> None:
    """Interpose so the *next* write transaction opens only after a drain
    has committed.

    The lifecycle gate's pre-read is lock-free; the review follow-up on PR
    #2247 is that a drain committing between that read and the caller's
    ``BEGIN IMMEDIATE`` must still refuse. This helper reproduces exactly
    that interleaving deterministically: the drain runs to its own commit
    inside the window, then the caller's transaction opens against the
    already-drained row.
    """
    store = service._store
    real_transaction = store.transaction
    armed = {"first": True}

    @contextmanager
    def _transaction() -> Iterator[sqlite3.Connection]:
        if armed.pop("first", None):
            service.drain_clerk(clerk_id=clerk_id)
        with real_transaction() as conn:
            yield conn

    monkeypatch.setattr(store, "transaction", _transaction)


def test_a_drain_committing_mid_registration_still_refuses(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration gate re-reads lifecycle inside the write transaction.

    Without the in-transaction re-read, this interleaving installs a fresh
    session for a lane whose door the drain ceremony already closed.
    """
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        _drain_mid_flight(service, monkeypatch, provisioned.clerk.clerk_id)
        with pytest.raises(ClerkLaneDraining, match="never returns"):
            service.register_agent_session(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                fleet_protocol_version=2,
            )
        assert service._store.read_session(provisioned.clerk.clerk_id) is None
    finally:
        service.close()


def test_a_drain_committing_mid_heartbeat_answers_draining_not_provisioned(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat's lifecycle answer reads the transactional truth.

    A stale answer here is the lesson failing to arrive: the lane would keep
    beating as a provisioned lane and never mark its evidence.
    """
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        session = service.register_agent_session(
            clerk_id=provisioned.clerk.clerk_id,
            worker_key=provisioned.clerk.worker_key,
            fleet_protocol_version=2,
        )
        _drain_mid_flight(service, monkeypatch, provisioned.clerk.clerk_id)
        observation = service.observe_session(
            clerk_id=provisioned.clerk.clerk_id,
            agent_instance_id=session.agent_instance_id,
        )
        assert observation.touched is True
        assert observation.lifecycle_state is StoredLifecycleState.DRAINING
    finally:
        service.close()


def test_a_drain_committing_mid_confirmation_still_refuses(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmation gate re-reads lifecycle inside the write transaction.

    A confirmation that slips past a mid-flight drain re-presents a grant
    the ceremony already closed — the resurrection in miniature.
    """
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        session = service.register_agent_session(
            clerk_id=provisioned.clerk.clerk_id,
            worker_key=provisioned.clerk.worker_key,
            fleet_protocol_version=2,
        )
        service.reserve_assignment(
            broker="alpaca",
            clerk_id=provisioned.clerk.clerk_id,
            external_account_id=ACCOUNT,
        )
        _drain_mid_flight(service, monkeypatch, provisioned.clerk.clerk_id)
        with pytest.raises(ClerkLaneDraining, match="confirms no binding"):
            service.confirm_assignment(
                broker="alpaca",
                clerk_id=provisioned.clerk.clerk_id,
                external_account_id=ACCOUNT,
                binding_generation=1,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
            )
        assignment = service._store.read_assignment(
            broker="alpaca", canonical_account_id=ACCOUNT
        )
        assert assignment is not None
        assert assignment.confirmed_binding_generation is None
    finally:
        service.close()


async def test_the_internal_surface_carries_the_lesson_in_both_bodies(
    control_dir: Path, clock: FrozenClock
) -> None:
    """The observe answer names the lifecycle; the register refusal names
    its typed reason — over the wire, exactly as in-process."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, control_dir.parent, clock)
        app = _agent_app(provisioned.clerk.clerk_id)
        app.state.fleet_service = service
        session = service.register_agent_session(
            clerk_id=provisioned.clerk.clerk_id,
            worker_key=provisioned.clerk.worker_key,
            fleet_protocol_version=2,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {
                "X-Fleet-Clerk-Id": provisioned.clerk.clerk_id,
                "X-Fleet-Agent-Token": CLERK_TOKEN,
            }
            observed = await client.post(
                "/internal/fleet/sessions/observe",
                json={
                    "clerk_id": provisioned.clerk.clerk_id,
                    "agent_instance_id": session.agent_instance_id,
                    "reported_state": "binding_confirmed",
                },
                headers=headers,
            )
            assert observed.status_code == 200
            assert observed.json()["lifecycle_state"] == "provisioned"

            service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
            clock.advance(1_000)
            drained_beat = await client.post(
                "/internal/fleet/sessions/observe",
                json={
                    "clerk_id": provisioned.clerk.clerk_id,
                    "agent_instance_id": session.agent_instance_id,
                    "reported_state": "binding_confirmed",
                },
                headers=headers,
            )
            assert drained_beat.status_code == 200
            assert drained_beat.json()["lifecycle_state"] == "draining"

            refused = await client.post(
                "/internal/fleet/sessions",
                json={
                    "clerk_id": provisioned.clerk.clerk_id,
                    "worker_key": provisioned.clerk.worker_key,
                    "fleet_protocol_version": 2,
                },
                headers=headers,
            )
            assert refused.status_code == 409
            assert refused.json()["reason"] == "clerk_lane_draining"
    finally:
        service.close()


# ---------------------------------------------------------------------------
# The transports: one typed refusal, neither of it the unavailability family
# ---------------------------------------------------------------------------


async def test_local_presence_translates_the_typed_refusal(
    control_dir: Path, clock: FrozenClock, tmp_path: Path
) -> None:
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        presence = LocalPresence(service, volume_root=Path(provisioned.clerk.volume_root))
        with pytest.raises(FleetLaneDraining, match="draining") as refused:
            await presence.register(
                clerk_id=provisioned.clerk.clerk_id,
                worker_key=provisioned.clerk.worker_key,
                agent_instance_id="agnt_0000000000000000000000aa",
                endpoint_ref=None,
                adapter_version="alpaca-1",
                fleet_protocol_version=2,
            )
        # The translation must not land in the family FR-066's offline
        # fallback catches — that family boots stale evidence.
        assert not isinstance(refused.value, FleetPresenceError)
    finally:
        service.close()


def _refusing_coordinator_app() -> FastAPI:
    """A coordinator whose every registration answers the typed drain refusal."""
    coordinator = FastAPI()

    @coordinator.post("/internal/fleet/sessions")
    async def register() -> JSONResponse:
        return JSONResponse(
            {
                "reason": "clerk_lane_draining",
                "message": "Clerk x is draining; a drained lane never returns to service.",
                "next_step": "Finish the drain ceremony.",
            },
            status_code=409,
        )

    return coordinator


async def test_remote_presence_translates_the_typed_refusal_not_unavailability() -> None:
    server = _RealServer(_refusing_coordinator_app())
    server.start()
    try:
        presence = RemotePresence(base_url=server.base_url, agent_service_token=CLERK_TOKEN)
        with pytest.raises(FleetLaneDraining, match="draining") as refused:
            await presence.register(
                clerk_id=CLERK,
                worker_key="wkrk_00000000000000000000000000000000",
                agent_instance_id="agnt_0000000000000000000000aa",
                endpoint_ref=None,
                adapter_version="alpaca-1",
                fleet_protocol_version=2,
            )
        assert not isinstance(refused.value, FleetPresenceError)
    finally:
        server.stop()


def _observing_coordinator_app(lifecycle: object) -> FastAPI:
    """A coordinator whose observe answer carries ``lifecycle`` as sent."""
    coordinator = FastAPI()

    @coordinator.post("/internal/fleet/sessions/observe")
    async def observe() -> JSONResponse:
        body: dict[str, object] = {"observed": True}
        if lifecycle is not None:
            body["lifecycle_state"] = lifecycle
        return JSONResponse(body)

    return coordinator


async def test_remote_presence_observe_reads_the_lifecycle_from_the_answer() -> None:
    server = _RealServer(_observing_coordinator_app("draining"))
    server.start()
    try:
        presence = RemotePresence(base_url=server.base_url, agent_service_token=CLERK_TOKEN)
        learned = await presence.observe(
            clerk_id=CLERK, agent_instance_id="agnt_0000000000000000000000aa"
        )
        assert learned == "draining"
    finally:
        server.stop()

    # An older coordinator sends no key: no news, not an error.
    legacy = _RealServer(_observing_coordinator_app(None))
    legacy.start()
    try:
        presence = RemotePresence(base_url=legacy.base_url, agent_service_token=CLERK_TOKEN)
        assert (
            await presence.observe(
                clerk_id=CLERK, agent_instance_id="agnt_0000000000000000000000aa"
            )
            is None
        )
    finally:
        legacy.stop()

    # A malformed answer on the one field this call exists to carry refuses
    # loudly rather than flattening into "no news".
    wrong = _RealServer(_observing_coordinator_app(7))
    wrong.start()
    try:
        presence = RemotePresence(base_url=wrong.base_url, agent_service_token=CLERK_TOKEN)
        with pytest.raises(FleetPresenceError, match="lifecycle_state"):
            await presence.observe(
                clerk_id=CLERK, agent_instance_id="agnt_0000000000000000000000aa"
            )
    finally:
        wrong.stop()


# ---------------------------------------------------------------------------
# The lane: learn, mark, and never boot the drained binding again
# ---------------------------------------------------------------------------


def _lane_settings(control_dir: Path, provisioned) -> FleetSettings:
    return FleetSettings(
        ROLE="clerk_agent",
        CONTROL_DIR=str(control_dir),
        CLERK_ID=provisioned.clerk.clerk_id,
        WORKER_KEY=provisioned.clerk.worker_key,
        DEPLOYMENT_NAMESPACE="compose:test",
    )


def _offline_settings(provisioned) -> FleetSettings:
    return FleetSettings(
        ROLE="clerk_agent",
        COORDINATOR_URL="http://127.0.0.1:9",
        AGENT_SERVICE_TOKEN=CLERK_TOKEN,
        CLERK_ID=provisioned.clerk.clerk_id,
        WORKER_KEY=provisioned.clerk.worker_key,
    )


async def _open_confirmed_lane(
    service: FleetControlService,
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """One enrolled lane, opened, reserved, confirmed, and reporting it."""
    from app.broker.alpaca.clerk.fleet_boot import (
        confirm_and_report,
        open_fleet_lane,
        reserve_account,
    )

    provisioned = _enrolled_lane(service, tmp_path, clock)
    root = Path(provisioned.clerk.volume_root)
    _fence_satisfying_roots(monkeypatch, root)
    _boot_service_on_the_test_clock(monkeypatch, clock)
    boot = await open_fleet_lane(
        settings=_lane_settings(control_dir, provisioned), volume_root=root
    )
    assert boot is not None and boot.online
    await reserve_account(boot, external_account_id=ACCOUNT)
    await confirm_and_report(
        boot,
        account_pin=ACCOUNT,
        effective_binding_generation=1,
        effective_profile_id="prof_1",
        effective_revision=2,
        authority_kind="sqlite",
        endpoint_mode="paper",
    )
    return provisioned, root, boot


async def test_a_live_lane_learns_its_drain_from_the_heartbeat_and_marks_its_evidence(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain lands while the lane is up: the next beat carries the lesson,
    the evidence flips to its tombstone, and the lane keeps beating."""
    service = _service(control_dir, clock)
    try:
        provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        from app.broker.alpaca.clerk.fleet_boot import close_fleet_lane, start_heartbeat

        start_heartbeat(boot, interval_s=0.05)
        await _await_beat_at(
            service, provisioned.clerk.clerk_id, clock, reported_state="binding_confirmed"
        )
        assert not boot.draining

        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        clock.advance(1_000)
        await _await_beat_at(
            service, provisioned.clerk.clerk_id, clock, reported_state="binding_confirmed"
        )

        assert boot.draining is True
        marked = read_confirmation_evidence(root)
        assert marked is not None
        assert marked.lifecycle_state == "draining"
        # The lane keeps its session and keeps observing while it winds down:
        # the coordinator's directory still sees it, not `unreachable`.
        session = service._store.read_session(provisioned.clerk.clerk_id)
        assert session is not None
        assert session.last_seen_at_ms == clock()
        await close_fleet_lane(boot)
    finally:
        service.close()


def test_a_failed_evidence_mark_leaves_the_drain_lesson_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed tombstone write must not latch the learned state (#2155).

    Review follow-up on PR #2247: ``boot.draining`` flipped before the durable
    mark ran, so an I/O failure on the evidence write left the lane "learned"
    with provisioned evidence still on disk — and the once-only latch then
    suppressed every retry, letting a later offline boot resurrect the drained
    binding after all. The latch must flip only after the mark succeeds.
    """
    from app.broker.alpaca.clerk import fleet_boot

    root = tmp_path
    write_confirmation_evidence(root, _evidence(CLERK, "volm_resurrection0000000000"))
    # ``_learn_drain`` reads no presence; the boot is just the lane's latch.
    boot = fleet_boot.FleetLaneBoot(
        presence=None,
        clerk_id=CLERK,
        worker_key="svct_" + "3" * 32,
        volume_root=root,
        registry_id="fltr_resurrection00000000000",
        volume_id="volm_resurrection0000000000",
    )

    def _disk_full_mark(volume_root: Path) -> bool:
        raise OSError("disk full while re-authoring the tombstone")

    monkeypatch.setattr(fleet_boot, "mark_confirmation_evidence_draining", _disk_full_mark)
    with pytest.raises(OSError):
        fleet_boot._learn_drain(boot)
    # Nothing was durably marked, so the lesson must not latch: the retry owes
    # this volume its tombstone.
    assert boot.draining is False
    survived = read_confirmation_evidence(root)
    assert survived is not None
    assert survived.lifecycle_state == "provisioned"

    monkeypatch.setattr(
        fleet_boot,
        "mark_confirmation_evidence_draining",
        mark_confirmation_evidence_draining,
    )
    fleet_boot._learn_drain(boot)
    assert boot.draining is True
    marked = read_confirmation_evidence(root)
    assert marked is not None
    assert marked.lifecycle_state == "draining"
    # And once the tombstone is durably in place, the latch holds: a second
    # call is the idempotent no-op, not a second write.
    fleet_boot._learn_drain(boot)


async def test_a_drained_lane_restarting_offline_never_resurrects_its_binding(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #2155 regression, end to end: a lane confirmed, then drained while
    live (so its evidence carries the tombstone), cannot boot that binding
    back up against a dead coordinator."""
    service = _service(control_dir, clock)
    try:
        provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        from app.broker.alpaca.clerk.fleet_boot import (
            FleetBootRefused,
            close_fleet_lane,
            offline_boot_matches,
            open_fleet_lane,
            start_heartbeat,
        )

        start_heartbeat(boot, interval_s=0.05)
        await _await_beat_at(
            service, provisioned.clerk.clerk_id, clock, reported_state="binding_confirmed"
        )
        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)
        clock.advance(1_000)
        await _await_beat_at(
            service, provisioned.clerk.clerk_id, clock, reported_state="binding_confirmed"
        )
        await close_fleet_lane(boot)
        tombstone = read_confirmation_evidence(root)
        assert tombstone is not None and tombstone.lifecycle_state == "draining"

        # The coordinator is now unreachable, and the drained lane restarts.
        with pytest.raises(FleetBootRefused, match="marked drained"):
            await open_fleet_lane(
                settings=_offline_settings(provisioned), volume_root=root
            )
        # And the FR-066 predicate itself refuses the tombstone, tuple and
        # generation notwithstanding — had a boot somehow opened offline.
        assert not evidence_vouches_for(
            tombstone,
            canonical_account_id=ACCOUNT,
            effective_profile_id="prof_1",
            effective_revision=2,
            binding_generation=1,
        )
        assert not offline_boot_matches(
            boot,
            canonical_account_id=ACCOUNT,
            effective_profile_id="prof_1",
            effective_revision=2,
            binding_generation=1,
        )
    finally:
        service.close()


async def test_a_drained_lane_restarting_with_the_coordinator_reachable_marks_and_refuses(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The register refusal is the lesson too: boot refuses, and the evidence
    on the volume comes away marked for the next, possibly-offline restart."""
    service = _service(control_dir, clock)
    try:
        provisioned, root, boot = await _open_confirmed_lane(
            service, control_dir, clock, tmp_path, monkeypatch
        )
        from app.broker.alpaca.clerk.fleet_boot import (
            FleetBootRefused,
            close_fleet_lane,
            open_fleet_lane,
        )

        await close_fleet_lane(boot)
        assert read_confirmation_evidence(root) is not None

        service.drain_clerk(clerk_id=provisioned.clerk.clerk_id)

        with pytest.raises(FleetBootRefused, match="drained"):
            await open_fleet_lane(
                settings=_lane_settings(control_dir, provisioned), volume_root=root
            )
        marked = read_confirmation_evidence(root)
        assert marked is not None
        assert marked.lifecycle_state == "draining"
    finally:
        service.close()


async def test_a_v1_volume_still_boots_offline(
    control_dir: Path,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy posture is untouched: a pre-#2155 evidence file on a lane
    nobody drained boots its exact grant offline exactly as before."""
    service = _service(control_dir, clock)
    try:
        provisioned = _enrolled_lane(service, tmp_path, clock)
        root = Path(provisioned.clerk.volume_root)
        _fence_satisfying_roots(monkeypatch, root)
        _write_v1_evidence(
            root,
            clerk_id=provisioned.clerk.clerk_id,
            volume_id=provisioned.clerk.volume_id,
        )
        from app.broker.alpaca.clerk.fleet_boot import offline_boot_matches, open_fleet_lane

        boot = await open_fleet_lane(
            settings=_offline_settings(provisioned), volume_root=root
        )
        assert boot is not None and not boot.online
        assert offline_boot_matches(
            boot,
            canonical_account_id=ACCOUNT,
            effective_profile_id="prof_1",
            effective_revision=2,
            binding_generation=1,
        )
    finally:
        service.close()
