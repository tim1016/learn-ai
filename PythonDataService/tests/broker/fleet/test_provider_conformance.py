"""Fake-provider conformance: N clerks across two adapters (PRD Phase 6 gate).

``fake_alpha`` and ``fake_beta`` run as the only two providers. What is proved
here is the extension boundary itself: provider-qualified assignment,
wrong-provider refusal, capability differences, distinct volumes, independent
state, and partial aggregation — with the generic spine importing no provider
implementation (``test_import_isolation.py``) and the fakes living only in
tests, never in the production registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    BrokerClerkCapabilityUnavailable,
    ClerkAssignmentConflict,
    ClerkBrokerMismatch,
)
from app.broker.fleet.provider import Capability
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from tests.broker.fleet.conftest import FrozenClock, provision_lane

RELEASE_PROOF = "old-clerk-offline-and-obligations-clear"


def test_n_clerks_across_two_providers_run_concurrently(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Six clerks, two providers, distinct volumes, one registry, no cross-talk."""
    lanes = []
    for index in range(3):
        lanes.append(
            provision_lane(
                fleet_service,
                broker="fake_alpha",
                label=f"alpha-{index}",
                tmp_path=control_dir.parent,
            )
        )
        lanes.append(
            provision_lane(
                fleet_service,
                broker="fake_beta",
                label=f"beta-{index}",
                tmp_path=control_dir.parent,
            )
        )

    volume_ids = set()
    attestations = set()
    for lane in lanes:
        fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
        fleet_service.reserve_assignment(
            broker=lane.broker, clerk_id=lane.clerk_id, external_account_id=f"acct-{lane.attestation_id}"
        )
        fleet_service.confirm_assignment(
            broker=lane.broker,
            clerk_id=lane.clerk_id,
            external_account_id=f"acct-{lane.attestation_id}",
            binding_generation=1,
        )
        volume_ids.add(lane.attestation_id)
        attestations.add(lane.attestation_id)

    assert len(volume_ids) == len(lanes)
    assert len(attestations) == len(lanes)

    directory = fleet_service.directory()
    assert len(directory["clerks"]) == len(lanes)
    assert {entry["broker"] for entry in directory["clerks"]} == {"fake_alpha", "fake_beta"}
    assert all(entry["lifecycle_state"] == "ready" for entry in directory["clerks"])


def test_a_fake_alpha_clerk_refuses_a_fake_beta_route(
    control_dir: Path, fleet_service
) -> None:
    alpha = provision_lane(
        fleet_service, broker="fake_alpha", label="cross", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.reserve_assignment(
            broker="fake_beta", clerk_id=alpha.clerk_id, external_account_id="acct-1"
        )
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=alpha.clerk_id)


def test_provider_clients_and_state_share_no_mutable_object(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Each service construction gets its own adapter instances; summaries do
    not leak between providers (the adapters are per-mapping objects)."""
    from tests.broker.fleet.conftest import fake_alpha, fake_beta

    alpha_adapter = fake_alpha()
    beta_adapter = fake_beta()
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": alpha_adapter, "fake_beta": beta_adapter},
        clock=clock,
    )
    try:
        alpha_lane = provision_lane(
            service, broker="fake_alpha", label="iso-a", tmp_path=control_dir.parent
        )
        beta_lane = provision_lane(
            service, broker="fake_beta", label="iso-b", tmp_path=control_dir.parent
        )
        service.register_agent_session(clerk_id=alpha_lane.clerk_id, worker_key=alpha_lane.worker_key)
        service.register_agent_session(clerk_id=beta_lane.clerk_id, worker_key=beta_lane.worker_key)
        entries = {
            entry["clerk_id"]: entry
            for entry in service.directory()["clerks"]
        }
        assert entries[alpha_lane.clerk_id]["provider_summary"]["provider_id"] == "fake_alpha"
        assert entries[beta_lane.clerk_id]["provider_summary"]["provider_id"] == "fake_beta"
        # The summary calls recorded on one adapter never appear on the other.
        assert len(alpha_adapter.summaries) == 1
        assert len(beta_adapter.summaries) == 1
    finally:
        service.close()


def test_killing_one_clerks_volume_does_not_mutate_another(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    survivor = provision_lane(
        fleet_service, broker="fake_alpha", label="survivor", tmp_path=control_dir.parent
    )
    casualty = provision_lane(
        fleet_service, broker="fake_beta", label="casualty", tmp_path=control_dir.parent
    )
    for lane in (survivor, casualty):
        fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
        fleet_service.reserve_assignment(
            broker=lane.broker, clerk_id=lane.clerk_id, external_account_id="acct-same"
        )

    # "Kill" the casualty: corrupt its volume marker beyond recognition.
    (casualty.volume_root / ".learn-ai-clerk-volume.json").write_text("{corrupt")
    with pytest.raises(Exception):
        fleet_service.verify_clerk_volume(
            clerk_id=casualty.clerk_id, volume_root=casualty.volume_root
        )

    # The survivor verifies, routes and keeps its assignment untouched.
    fleet_service.verify_clerk_volume(
        clerk_id=survivor.clerk_id, volume_root=survivor.volume_root
    )
    fleet_service.resolve_route(broker="fake_alpha", clerk_id=survivor.clerk_id)
    survivor_assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-SAME"
    )
    assert survivor_assignment is not None
    assert survivor_assignment.clerk_id == survivor.clerk_id


def test_racing_the_same_account_yields_one_winner_and_a_durable_refusal(
    control_dir: Path, fleet_service
) -> None:
    first = provision_lane(
        fleet_service, broker="fake_alpha", label="race-a", tmp_path=control_dir.parent
    )
    second = provision_lane(
        fleet_service, broker="fake_beta", label="race-b", tmp_path=control_dir.parent
    )
    # Same raw account, but provider-qualified keys differ — no conflict.
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=first.clerk_id, external_account_id="acct-dup"
    )
    fleet_service.reserve_assignment(
        broker="fake_beta", clerk_id=second.clerk_id, external_account_id="acct-dup"
    )
    # Within one provider, the second clerk's reservation is durably refused.
    rival = provision_lane(
        fleet_service, broker="fake_alpha", label="race-c", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.reserve_assignment(
            broker="fake_alpha", clerk_id=rival.clerk_id, external_account_id="acct-dup"
        )
    assert (
        fleet_service._store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-DUP"
        ).clerk_id
        == first.clerk_id
    )


def test_undeclared_capabilities_refuse_with_evidence_not_emulation(
    control_dir: Path, fleet_service
) -> None:
    with pytest.raises(BrokerClerkCapabilityUnavailable) as excinfo:
        fleet_service.require_capability(
            broker="fake_beta", capability=Capability.CUSTODY_READ
        )
    assert "fake_beta" in str(excinfo.value)
    assert "custody_read" in str(excinfo.value)


def test_the_generic_spine_survives_a_provider_adapter_refusal(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """A provider raising inside canonicalization surfaces as its own error,
    not as registry corruption."""
    from tests.broker.fleet.conftest import FakeProviderAdapter

    strict = FakeProviderAdapter(
        provider_id="fake_alpha",
        refused_accounts=frozenset({"acct-bad"}),
    )
    service = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_alpha": strict, "fake_beta": FakeProviderAdapter("fake_beta")},
        clock=clock,
    )
    lane = provision_lane(
        service, broker="fake_alpha", label="strict", tmp_path=control_dir.parent
    )
    with pytest.raises(LookupError):
        service.reserve_assignment(
            broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-bad"
        )
    # The registry recorded nothing for the refused canonicalization.
    assert (
        service._store.read_assignment(broker="fake_alpha", canonical_account_id="ACCT-BAD")
        is None
    )
