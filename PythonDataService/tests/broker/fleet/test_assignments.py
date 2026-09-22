"""Broker-qualified account assignment fencing (PRD §9.6, ADR 0062 Decisions 3–4).

Every invariant that makes the fence a fence: provider-qualified uniqueness,
no expiry into takeover, monotonic generations, transactional updates, and
exactly one winner under a concurrent reserve race.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    ClerkAccountMismatch,
    ClerkAssignmentConflict,
    ClerkBrokerMismatch,
    ClerkReassignmentBlocked,
)
from app.broker.fleet.records import AssignmentState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from tests.broker.fleet.conftest import (
    TEST_CHANGE_REF,
    TEST_OPERATOR,
    FrozenClock,
    Lane,
    fake_alpha,
    provision_lane,
    release_after_drain,
)


def _reserved(fleet_service: FleetControlService, broker: str, clerk_id: str, account: str):
    """Reserve one account for a clerk through the service seam."""
    return fleet_service.reserve_assignment(
        broker=broker, clerk_id=clerk_id, external_account_id=account
    )


def test_a_duplicate_canonical_account_within_one_provider_refuses(
    control_dir: Path, fleet_service
) -> None:
    """The fence canonicalizes before it fences, so whitespace and case cannot smuggle a rival past one provider's reservation."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="a1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="a2", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-shared ")
    # Canonicalization folds the whitespace and case before the fence sees it.
    with pytest.raises(ClerkAssignmentConflict):
        _reserved(fleet_service, "fake_alpha", second.clerk_id, " ACCT-SHARED")


def test_identical_raw_account_ids_coexist_across_providers(
    control_dir: Path, fleet_service
) -> None:
    """Assignment identity is provider-qualified: one raw string, two providers,
    two *different* canonical keys and two independent reservations."""
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="x", tmp_path=control_dir.parent)
    beta = provision_lane(fleet_service, broker="fake_beta", label="y", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", alpha.clerk_id, "acct-1")
    second = _reserved(fleet_service, "fake_beta", beta.clerk_id, "acct-1")
    # Each provider owns its own canonicalization; the registry never folds
    # two providers' account identities into one key.
    assert first.canonical_external_account_id == "ACCT-1"
    assert second.canonical_external_account_id == "acct_1"
    assert first.canonical_external_account_id != second.canonical_external_account_id
    assert first.broker != second.broker
    # Neither reservation is visible under the other provider's key.
    assert fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="acct_1"
    ) is None
    assert fleet_service._store.read_assignment(
        broker="fake_beta", canonical_account_id="ACCT-1"
    ) is None
    assert fleet_service._store.list_active_assignments() == [first, second]


def test_one_canonical_key_belongs_to_each_provider_independently(
    control_dir: Path, fleet_service
) -> None:
    """The key is (broker, canonical): a raw id both providers canonicalize
    *identically* still yields two independent rows, not a conflict."""
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="pk-a", tmp_path=control_dir.parent)
    beta = provision_lane(fleet_service, broker="fake_beta", label="pk-b", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", alpha.clerk_id, " 90210 ")
    second = _reserved(fleet_service, "fake_beta", beta.clerk_id, " 90210 ")
    assert first.canonical_external_account_id == second.canonical_external_account_id == "90210"
    assert fleet_service._store.list_active_assignments() == [first, second]


def test_a_clerk_cannot_reserve_under_another_brokers_route(
    control_dir: Path, fleet_service
) -> None:
    """A reservation under a broker the clerk does not belong to refuses."""
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="mismatch", tmp_path=control_dir.parent)
    with pytest.raises(ClerkBrokerMismatch):
        _reserved(fleet_service, "fake_beta", alpha.clerk_id, "acct-1")


def test_an_empty_canonical_account_refuses(control_dir: Path, fleet_service) -> None:
    """A canonicalization that yields nothing is a refusal, not a reservation of the empty key."""
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="empty", tmp_path=control_dir.parent)
    with pytest.raises(ClerkAccountMismatch):
        _reserved(fleet_service, "fake_alpha", alpha.clerk_id, "   ")


def test_heartbeat_loss_never_releases_ownership(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The load-bearing partition test: staleness projects, it never transfers."""
    owner = provision_lane(fleet_service, broker="fake_alpha", label="owner", tmp_path=control_dir.parent)
    rival = provision_lane(fleet_service, broker="fake_alpha", label="rival", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=owner.clerk_id, worker_key=owner.worker_key)
    _reserved(fleet_service, "fake_alpha", owner.clerk_id, "acct-live")

    # The owner goes silent far past every staleness window.
    clock.advance(24 * 60 * 60 * 1000)
    directory = fleet_service.directory()
    owner_entry = next(
        entry for entry in directory["clerks"] if entry["clerk_id"] == owner.clerk_id
    )
    assert owner_entry["lifecycle_state"] == "unreachable"

    # The rival still cannot take the account — not by reserving…
    with pytest.raises(ClerkAssignmentConflict):
        _reserved(fleet_service, "fake_alpha", rival.clerk_id, "acct-live")
    # …and the assignment row itself is untouched.
    assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-LIVE"
    )
    assert assignment is not None
    assert assignment.clerk_id == owner.clerk_id
    assert assignment.state == AssignmentState.RESERVED


def test_reserve_for_the_same_owner_is_idempotent(control_dir: Path, fleet_service) -> None:
    """Re-reserving returns the existing reservation — reserved or effective."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="same", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-i")
    again = _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-i")
    assert first == again
    # The same-owner resume of an *effective* assignment (audit 2026-09-13,
    # finding 1): a restarted clerk re-runs its reservation step and gets its
    # confirmed facts back untouched, never a refusal.
    fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    session = fleet_service._store.read_session(lane.clerk_id)
    assert session is not None
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-i",
        binding_generation=2,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    resumed = _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-i")
    assert resumed.state == AssignmentState.EFFECTIVE
    assert resumed.confirmed_binding_generation == 2


def test_confirm_moves_reserved_to_effective_and_records_the_binding(
    control_dir: Path, fleet_service
) -> None:
    """Confirmation moves the assignment to effective and records the confirmed observation."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="confirm", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-c")
    confirmed = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-c",
        binding_generation=4,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        effective_profile_id="prof_1",
        effective_revision=2,
    )
    assert confirmed.state == AssignmentState.EFFECTIVE
    assert confirmed.effective_revision == 2
    # The confirmed observation is fenced by the confirming session and is
    # what routed commands are checked against.
    assert confirmed.confirmed_binding_generation == 4
    assert confirmed.confirmed_agent_instance_id == session.agent_instance_id
    assert confirmed.confirmed_routing_epoch == session.routing_epoch
    # #2145: the confirmation write (the assignment CAS update above) and the
    # reported-fact write (the session's projection, via a direct
    # ``touch_session`` call in the same transaction) are two distinct
    # writes — pin both sides so a regression that drops either one reddens
    # here.
    refreshed = fleet_service._store.read_session(lane.clerk_id)
    assert refreshed is not None
    assert refreshed.reported_binding_generation == 4
    assert refreshed.reported_account_id == "ACCT-C"
    assert refreshed.reported_state == "binding_confirmed"


def test_confirm_refuses_a_rival_and_a_released_assignment(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Rivals, session-less clerks and released assignments can never confirm."""
    owner = provision_lane(fleet_service, broker="fake_alpha", label="own", tmp_path=control_dir.parent)
    rival = provision_lane(fleet_service, broker="fake_alpha", label="riv", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", owner.clerk_id, "acct-o")
    # Both lanes have sessions: the refusals below are about assignment
    # ownership, not about a missing worker.
    rival_session = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=rival.clerk_id, worker_key=rival.worker_key
    )
    fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=owner.clerk_id, worker_key=owner.worker_key)
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=rival.clerk_id,
            external_account_id="acct-o",
            binding_generation=1,
            agent_instance_id=rival_session.agent_instance_id,
            routing_epoch=rival_session.routing_epoch,
        )
    # A session-less clerk cannot confirm anything at all: there is no worker
    # to have acknowledged the binding (FR-064's gate, on the write path).
    third = provision_lane(fleet_service, broker="fake_alpha", label="third", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", third.clerk_id, "acct-t")
    from app.broker.fleet.errors import ClerkUnreachable

    with pytest.raises(ClerkUnreachable):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=third.clerk_id,
            external_account_id="acct-t",
            binding_generation=1,
            agent_instance_id="agnt_0000000000000000000000aa",
            routing_epoch=1,
        )

    release_after_drain(
        fleet_service, clock, owner, account="acct-o"
    )
    owner_session = fleet_service._store.read_session(owner.clerk_id)
    assert owner_session is not None
    # Since #2155 the drained-owner refusal names the root cause — the lane
    # is draining, so it confirms nothing. The released-assignment fence
    # stays inside the transaction as the in-order defense it always was.
    from app.broker.fleet.errors import ClerkLaneDraining

    with pytest.raises(ClerkLaneDraining):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=owner.clerk_id,
            external_account_id="acct-o",
            binding_generation=1,
            agent_instance_id=owner_session.agent_instance_id,
            routing_epoch=owner_session.routing_epoch,
        )


def test_release_requires_the_ceremony_gates_and_starts_a_higher_generation(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The release ceremony demands a drained, deadline-elapsed predecessor, a
    bounded attribution, and a pinned generation; the released row's next
    owner arrives by the reassignment ceremony alone (ADR 0063 Decision 4,
    #2157)."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="gen1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="gen2", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-g")
    reserved_g = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-G"
    )
    assert reserved_g is not None

    # A blank attribution is a malformed invocation, not a state refusal.
    with pytest.raises(ValueError, match="attributing who acted"):
        fleet_service.release_assignment(
            broker="fake_alpha",
            external_account_id="acct-g",
            expected_assignment_generation=reserved_g.assignment_generation,
            operator="   ",
            change_ref=TEST_CHANGE_REF,
        )
    # A provisioned predecessor refuses: the door must be closed first.
    from app.broker.fleet.errors import ClerkDrainRequired

    with pytest.raises(ClerkDrainRequired, match="door is closed"):
        fleet_service.release_assignment(
            broker="fake_alpha",
            external_account_id="acct-g",
            expected_assignment_generation=reserved_g.assignment_generation,
            operator=TEST_OPERATOR,
            change_ref=TEST_CHANGE_REF,
        )
    # A stale pin refuses: release evidence cannot cross a reassignment.
    fleet_service.drain_clerk(clerk_id=first.clerk_id)
    with pytest.raises(ClerkAssignmentConflict, match="pinned"):
        fleet_service.release_assignment(
            broker="fake_alpha",
            external_account_id="acct-g",
            expected_assignment_generation=reserved_g.assignment_generation + 5,
            operator=TEST_OPERATOR,
            change_ref=TEST_CHANGE_REF,
        )

    released = release_after_drain(
        fleet_service, clock, first, account="acct-g"
    )
    assert released.state == AssignmentState.RELEASED
    # The released row stays as terminal history…
    assert fleet_service._store.list_assignments_for_clerk(first.clerk_id)[0].state == (
        AssignmentState.RELEASED
    )
    # …and its next owner arrives by the reassignment ceremony alone: a
    # reservation on the released row is that ceremony's two-step reach and
    # refuses on its gate (#2157) — the covering tests live below.
    clock.advance(1)
    with pytest.raises(ClerkReassignmentBlocked, match="#2157"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=second.clerk_id,
            external_account_id="acct-g",
            volume_root=second.volume_root,
        )


def test_a_concurrent_reserve_race_admits_exactly_one_winner(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Two coordinators over one registry race one account: one winner, one typed refusal."""
    adapters = {"fake_alpha": fake_alpha()}
    lanes = []
    seeder = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=adapters,
        clock=clock,
    )
    try:
        for index in range(8):
            lanes.append(
                provision_lane(
                    seeder, broker="fake_alpha", label=f"racer-{index}", tmp_path=control_dir.parent
                )
            )
    finally:
        seeder.close()

    def attempt(lane: Lane) -> object:
        service = FleetControlService(
            store=FleetRegistryStore.open(control_dir=control_dir),
            provider_adapters=adapters,
            clock=clock,
        )
        try:
            return _reserved(service, "fake_alpha", lane.clerk_id, "acct-race")
        except ClerkAssignmentConflict:
            return "refused"
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, lanes))

    winners = [outcome for outcome in outcomes if outcome != "refused"]
    refusals = [outcome for outcome in outcomes if outcome == "refused"]
    assert len(winners) == 1
    assert len(refusals) == len(lanes) - 1

    checker = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=adapters,
        clock=clock,
    )
    try:
        settled = checker._store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-RACE"
        )
    finally:
        checker.close()
    assert settled is not None
    assert settled.clerk_id == winners[0].clerk_id
    assert settled.state == AssignmentState.RESERVED


# ---- release-then-reserve: one transfer gate (#2157) -----------------------


def test_release_then_reserve_reaches_the_same_transfer_gate_as_reassign(
    control_dir: Path, clock: FrozenClock, fleet_service: FleetControlService
) -> None:
    """#2157: the two-step reach of reassignment carries the ceremony's gate.

    Drain, wait out the deadline, release under attribution — then a fresh
    provisioned successor reserves the same account: that is a clerk-to-clerk
    transfer wearing a reservation's clothes, and it refuses on exactly the
    ground reassignment refuses on, leaving the released row untouched. The
    block holds after the predecessor retires too: a released row under a
    retired lane is the runbook's normal end state, not an inconsistency.
    """
    first = provision_lane(
        fleet_service, broker="fake_alpha", label="rel", tmp_path=control_dir.parent
    )
    second = provision_lane(
        fleet_service, broker="fake_alpha", label="succ", tmp_path=control_dir.parent
    )
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-moved")
    released = release_after_drain(fleet_service, clock, first, account="acct-moved")

    with pytest.raises(ClerkReassignmentBlocked, match="#2157") as blocked:
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=second.clerk_id,
            external_account_id="acct-moved",
            volume_root=second.volume_root,
        )
    assert "#2154" in blocked.value.message

    fleet_service.force_retire_clerk(
        clerk_id=first.clerk_id, operator=TEST_OPERATOR, change_ref=TEST_CHANGE_REF
    )
    with pytest.raises(ClerkReassignmentBlocked, match="#2157"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=second.clerk_id,
            external_account_id="acct-moved",
            volume_root=second.volume_root,
        )

    # The refused transfers left the released row exactly as the ceremony
    # wrote it — no generation bump, no new owner.
    unchanged = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-MOVED"
    )
    assert unchanged is not None
    assert unchanged.state == AssignmentState.RELEASED
    assert unchanged.clerk_id == first.clerk_id
    assert unchanged.assignment_generation == released.assignment_generation


def test_a_released_row_never_transfers_without_the_successors_volume_proof(
    control_dir: Path, clock: FrozenClock, fleet_service: FleetControlService
) -> None:
    """The transfer path's volume_root is required, not optional (#2157).

    A remote reservation — the one transport that cannot carry a volume
    proof — never moves a released account on any ground; "the coordinator
    may not have the volume mounted" is a reason to refuse, not to skip.
    """
    first = provision_lane(
        fleet_service, broker="fake_alpha", label="nv", tmp_path=control_dir.parent
    )
    second = provision_lane(
        fleet_service, broker="fake_alpha", label="nv2", tmp_path=control_dir.parent
    )
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-nv")
    release_after_drain(fleet_service, clock, first, account="acct-nv")

    with pytest.raises(ClerkAssignmentConflict, match="volume proof"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=second.clerk_id,
            external_account_id="acct-nv",
        )


def test_a_released_row_under_a_provisioned_clerk_is_registry_inconsistency(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """Only the release ceremony produces released rows, and it requires a
    drained, past-deadline owner; a released row surfacing under a
    still-provisioned clerk reports the inconsistency rather than
    transferring or reusing."""
    from dataclasses import replace

    first = provision_lane(
        fleet_service, broker="fake_alpha", label="bad", tmp_path=control_dir.parent
    )
    second = provision_lane(
        fleet_service, broker="fake_alpha", label="bad2", tmp_path=control_dir.parent
    )
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-bad")
    store = fleet_service._store
    row = store.read_assignment(broker="fake_alpha", canonical_account_id="ACCT-BAD")
    assert row is not None
    forged = replace(row, state=AssignmentState.RELEASED)
    with store.transaction() as conn:
        accepted = store.cas_update_assignment(
            conn,
            forged,
            previous_generation=row.assignment_generation,
            previous_state=AssignmentState.RESERVED,
        )
    assert accepted

    with pytest.raises(ClerkAssignmentConflict, match="still provisioned"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=second.clerk_id,
            external_account_id="acct-bad",
            volume_root=second.volume_root,
        )
