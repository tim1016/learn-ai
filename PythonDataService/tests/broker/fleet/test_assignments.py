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
)
from app.broker.fleet.records import AssignmentState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from tests.broker.fleet.conftest import FrozenClock, Lane, fake_alpha, provision_lane

RELEASE_PROOF = "old-clerk-offline-and-obligations-clear"


def _reserved(fleet_service: FleetControlService, broker: str, clerk_id: str, account: str):
    return fleet_service.reserve_assignment(
        broker=broker, clerk_id=clerk_id, external_account_id=account
    )


def test_a_duplicate_canonical_account_within_one_provider_refuses(
    control_dir: Path, fleet_service
) -> None:
    first = provision_lane(fleet_service, broker="fake_alpha", label="a1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="a2", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-shared ")
    # Canonicalization folds the whitespace and case before the fence sees it.
    with pytest.raises(ClerkAssignmentConflict):
        _reserved(fleet_service, "fake_alpha", second.clerk_id, " ACCT-SHARED")


def test_identical_raw_account_ids_coexist_across_providers(
    control_dir: Path, fleet_service
) -> None:
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="x", tmp_path=control_dir.parent)
    beta = provision_lane(fleet_service, broker="fake_beta", label="y", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", alpha.clerk_id, "acct-1")
    second = _reserved(fleet_service, "fake_beta", beta.clerk_id, "acct-1")
    assert first.canonical_external_account_id == second.canonical_external_account_id
    assert first.broker != second.broker
    assert fleet_service._store.list_active_assignments() == [first, second]


def test_a_clerk_cannot_reserve_under_another_brokers_route(
    control_dir: Path, fleet_service
) -> None:
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="mismatch", tmp_path=control_dir.parent)
    with pytest.raises(ClerkBrokerMismatch):
        _reserved(fleet_service, "fake_beta", alpha.clerk_id, "acct-1")


def test_an_empty_canonical_account_refuses(control_dir: Path, fleet_service) -> None:
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="empty", tmp_path=control_dir.parent)
    with pytest.raises(ClerkAccountMismatch):
        _reserved(fleet_service, "fake_alpha", alpha.clerk_id, "   ")


def test_heartbeat_loss_never_releases_ownership(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """The load-bearing partition test: staleness projects, it never transfers."""
    owner = provision_lane(fleet_service, broker="fake_alpha", label="owner", tmp_path=control_dir.parent)
    rival = provision_lane(fleet_service, broker="fake_alpha", label="rival", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(clerk_id=owner.clerk_id, worker_key=owner.worker_key)
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
    lane = provision_lane(fleet_service, broker="fake_alpha", label="same", tmp_path=control_dir.parent)
    first = _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-i")
    again = _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-i")
    assert first == again


def test_confirm_moves_reserved_to_effective_and_records_the_binding(
    control_dir: Path, fleet_service
) -> None:
    lane = provision_lane(fleet_service, broker="fake_alpha", label="confirm", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    _reserved(fleet_service, "fake_alpha", lane.clerk_id, "acct-c")
    confirmed = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-c",
        binding_generation=4,
        effective_profile_id="prof_1",
        effective_revision=2,
    )
    assert confirmed.state == AssignmentState.EFFECTIVE
    assert confirmed.effective_revision == 2
    # The session's reported binding is what routed commands are checked against.
    refreshed = fleet_service._store.read_session(lane.clerk_id)
    assert refreshed is not None
    assert refreshed.reported_binding_generation == 4
    assert refreshed.reported_account_id == "ACCT-C"


def test_confirm_refuses_a_rival_and_a_released_assignment(
    control_dir: Path, fleet_service
) -> None:
    owner = provision_lane(fleet_service, broker="fake_alpha", label="own", tmp_path=control_dir.parent)
    rival = provision_lane(fleet_service, broker="fake_alpha", label="riv", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", owner.clerk_id, "acct-o")
    fleet_service.register_agent_session(clerk_id=rival.clerk_id, worker_key=rival.worker_key)
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=rival.clerk_id,
            external_account_id="acct-o",
            binding_generation=1,
        )

    fleet_service.release_assignment(
        broker="fake_alpha", external_account_id="acct-o", proof=RELEASE_PROOF
    )
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=owner.clerk_id,
            external_account_id="acct-o",
            binding_generation=1,
        )


def test_release_requires_the_ceremony_proof_and_starts_a_higher_generation(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    first = provision_lane(fleet_service, broker="fake_alpha", label="gen1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="gen2", tmp_path=control_dir.parent)
    _reserved(fleet_service, "fake_alpha", first.clerk_id, "acct-g")

    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.release_assignment(broker="fake_alpha", external_account_id="acct-g", proof="")
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.release_assignment(
            broker="fake_alpha", external_account_id="acct-g", proof="just-trust-me"
        )

    released = fleet_service.release_assignment(
        broker="fake_alpha", external_account_id="acct-g", proof=RELEASE_PROOF
    )
    assert released.state == AssignmentState.RELEASED
    # The released row stays as terminal history…
    assert fleet_service._store.list_assignments_for_clerk(first.clerk_id)[0].state == (
        AssignmentState.RELEASED
    )
    # …and reuse starts a fresh reservation at a higher generation.
    clock.advance(1)
    reassigned = _reserved(fleet_service, "fake_alpha", second.clerk_id, "acct-g")
    assert reassigned.clerk_id == second.clerk_id
    assert reassigned.assignment_generation == released.assignment_generation + 1


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
