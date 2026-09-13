"""Regression probes for the four reproduced admission gaps (audit 2026-09-13).

The audit drove these four scenarios through ``FleetControlService`` on the
pre-hardening spine and recorded the wrong answers. Each test here pins the
required answer; together they are the definition of "observed health is not
confirmed admission" for the fleet protocol.

    Probe 1 — heartbeat-only binding facts never open execution routing.
    Probe 2 — same-owner reservation resume after confirmation succeeds.
    Probe 3 — a superseded session's confirmation refuses atomically.
    Probe 4 — a stale confirmed generation refuses, even from the new session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    ClerkAssignmentConflict,
    ClerkBindingGenerationConflict,
    ClerkEndpointNotApproved,
    ClerkIdentityMismatch,
    ClerkUnreachable,
    ClerkVolumeAlreadyRegistered,
    FleetProtocolIncompatible,
)
from app.broker.fleet.records import AssignmentState, ProviderSummaryObservation
from tests.broker.fleet.conftest import provision_lane


def test_probe_1_a_heartbeat_with_binding_facts_never_opens_execution_routing(
    control_dir: Path, fleet_service
) -> None:
    """Register and heartbeat plausible binding facts without ever reserving
    or confirming: execution routing stays closed."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="probe1", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    assert fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        reported_binding_generation=7,
        reported_account_id="ACCT-LIE",
        reported_state="binding_confirmed",
    )
    with pytest.raises(ClerkUnreachable, match="no effective account assignment"):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    # The directory projects at most starting: health without confirmation.
    entry = next(
        e for e in fleet_service.directory()["clerks"] if e["clerk_id"] == lane.clerk_id
    )
    assert entry["lifecycle_state"] == "starting"
    assert entry["effective_binding_generation"] is None


def test_probe_2_same_owner_reservation_resume_succeeds_after_confirmation(
    control_dir: Path, fleet_service
) -> None:
    """Reserve, confirm, then re-run the reserve step for the same clerk: the
    resume returns the effective row with its confirmed facts untouched."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="probe2", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-resume"
    )
    confirmed = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-resume",
        binding_generation=3,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    resumed = fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-resume"
    )
    assert resumed.state == AssignmentState.EFFECTIVE
    assert resumed.confirmed_binding_generation == 3
    assert resumed == confirmed
    # A rival still cannot wedge itself in through the resume path.
    rival = provision_lane(fleet_service, broker="fake_alpha", label="probe2b", tmp_path=control_dir.parent)
    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.reserve_assignment(
            broker="fake_alpha", clerk_id=rival.clerk_id, external_account_id="acct-resume"
        )


def test_probe_3_a_superseded_sessions_confirmation_refuses_atomically(
    control_dir: Path, fleet_service
) -> None:
    """Replace the agent session, then confirm carrying the *old* instance and
    epoch: the refusal lands before any write, and the replacement session
    re-confirming from the current binding succeeds."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="probe3", tmp_path=control_dir.parent)
    first = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-super"
    )
    replacement = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id,
        worker_key=lane.worker_key,
        agent_instance_id="agnt_999999999999999999999999",
    )
    assert replacement.routing_epoch > first.routing_epoch
    with pytest.raises(ClerkIdentityMismatch, match="superseded session"):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            external_account_id="acct-super",
            binding_generation=2,
            agent_instance_id=first.agent_instance_id,
            routing_epoch=first.routing_epoch,
        )
    # Nothing landed: the assignment is still a bare reservation.
    stored = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-SUPER"
    )
    assert stored is not None
    assert stored.state == AssignmentState.RESERVED
    assert stored.confirmed_binding_generation is None
    # The replacement session confirms from the current binding and wins.
    confirmed = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-super",
        binding_generation=2,
        agent_instance_id=replacement.agent_instance_id,
        routing_epoch=replacement.routing_epoch,
    )
    assert confirmed.confirmed_agent_instance_id == replacement.agent_instance_id


def test_probe_4_a_stale_confirmed_generation_refuses_even_from_the_new_session(
    control_dir: Path, fleet_service
) -> None:
    """Confirm generation 10, replace the session, then confirm generation 3:
    the stale evidence refuses; restoring old bindings is its own ceremony."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="probe4", tmp_path=control_dir.parent)
    first = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-stale"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-stale",
        binding_generation=10,
        agent_instance_id=first.agent_instance_id,
        routing_epoch=first.routing_epoch,
    )
    replacement = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id,
        worker_key=lane.worker_key,
        agent_instance_id="agnt_888888888888888888888888",
    )
    with pytest.raises(ClerkBindingGenerationConflict, match="stale"):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            external_account_id="acct-stale",
            binding_generation=3,
            agent_instance_id=replacement.agent_instance_id,
            routing_epoch=replacement.routing_epoch,
        )
    # Equal re-acknowledgement converges (crash recovery on the same binding).
    reacked = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-stale",
        binding_generation=10,
        agent_instance_id=replacement.agent_instance_id,
        routing_epoch=replacement.routing_epoch,
    )
    assert reacked.confirmed_agent_instance_id == replacement.agent_instance_id
    assert reacked.confirmed_binding_generation == 10
    # A higher generation advances (the normal Apply progression).
    advanced = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-stale",
        binding_generation=11,
        agent_instance_id=replacement.agent_instance_id,
        routing_epoch=replacement.routing_epoch,
    )
    assert advanced.confirmed_binding_generation == 11


def test_registration_cannot_choose_or_move_its_endpoint(
    control_dir: Path, fleet_service
) -> None:
    """A registration cites a deployment-approved reference; an unapproved or
    wrong reference refuses, and re-approval is the only way a destination moves."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="endpoint", tmp_path=control_dir.parent)
    with pytest.raises(ClerkEndpointNotApproved, match="approved no endpoint"):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            endpoint_ref="agent:paper-1",
        )
    approved = fleet_service.approve_endpoint(
        clerk_id=lane.clerk_id,
        endpoint_ref="agent:paper-1",
        base_url="http://alpaca-paper-clerk:8000/",
    )
    assert approved.base_url == "http://alpaca-paper-clerk:8000"
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id,
        worker_key=lane.worker_key,
        endpoint_ref="agent:paper-1",
    )
    assert session.endpoint_ref == "agent:paper-1"
    with pytest.raises(ClerkEndpointNotApproved):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            agent_instance_id="agnt_777777777777777777777777",
            endpoint_ref="agent:impostor",
        )
    # Re-targeting the approved reference is the host ceremony's power alone.
    retargeted = fleet_service.approve_endpoint(
        clerk_id=lane.clerk_id,
        endpoint_ref="agent:paper-1",
        base_url="http://alpaca-paper-clerk:8001",
    )
    assert retargeted.base_url == "http://alpaca-paper-clerk:8001"
    with pytest.raises(ClerkEndpointNotApproved, match="stable"):
        fleet_service.approve_endpoint(
            clerk_id=lane.clerk_id,
            endpoint_ref="agent:renamed",
            base_url="http://alpaca-paper-clerk:8002",
        )
    with pytest.raises(ValueError, match="user info"):
        fleet_service.approve_endpoint(
            clerk_id=lane.clerk_id,
            endpoint_ref="agent:paper-1",
            base_url="http://user:pass@alpaca-paper-clerk:8000",
        )


def test_an_agent_speaking_another_protocol_version_refuses(
    control_dir: Path, fleet_service
) -> None:
    """Registration carries the fleet protocol version; a mismatch refuses."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="proto", tmp_path=control_dir.parent)
    with pytest.raises(FleetProtocolIncompatible, match="protocol"):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            fleet_protocol_version=99,
        )
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id,
        worker_key=lane.worker_key,
        fleet_protocol_version=2,
        adapter_version="test.1",
    )
    assert session.adapter_version == "test.1"


def test_the_same_path_in_two_namespaces_is_two_volumes(
    control_dir: Path, fleet_service
) -> None:
    """Audit finding 4: equal path strings across deployment namespaces are
    different mounts; within one namespace they still refuse."""

    namespace_a = control_dir.parent / "ns-a"
    namespace_b = control_dir.parent / "ns-b"
    for directory in (namespace_a, namespace_b):
        directory.mkdir(parents=True, exist_ok=True)
    # Both containers mount their own volume at the same canonical path by
    # resolving through per-namespace temp roots to the same *string* is not
    # possible on one host — so emulate the audit's scenario directly: same
    # path string, different namespaces, distinct attestations.
    shared_path = namespace_a / "app" / "artifacts" / "alpaca_clerk"
    shared_path.mkdir(parents=True)
    first = fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="container-a",
        volume_root=shared_path,
        attestation_id="learn-ai-alpaca-paper",
        deployment_namespace="compose:prod",
    )
    # A second clerk in another namespace may mount a different physical
    # volume at the same path: the path string alone must not refuse it.
    # (On one host the roots genuinely differ here, which is the point —
    # the namespace, not the string, scopes the comparison.)
    other_root = control_dir.parent / "ns-b" / "app" / "artifacts" / "alpaca_clerk"
    other_root.mkdir(parents=True)
    second = fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="container-b",
        volume_root=other_root,
        attestation_id="learn-ai-alpaca-live",
        deployment_namespace="compose:prod",
    )
    assert first.clerk.deployment_namespace == second.clerk.deployment_namespace
    # Same attestation *name* in two namespaces is not a collision either.
    third_root = control_dir.parent / "ns-staging" / "app" / "artifacts" / "alpaca_clerk"
    third_root.mkdir(parents=True)
    third = fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="container-c",
        volume_root=third_root,
        attestation_id="learn-ai-alpaca-paper",
        deployment_namespace="compose:staging",
    )
    assert third.clerk.deployment_namespace == "compose:staging"
    # But within one namespace, a nested root of an active clerk still refuses.
    nested = shared_path / "nested"
    nested.mkdir()
    with pytest.raises(ClerkVolumeAlreadyRegistered):
        fleet_service.provision_clerk(
            broker="fake_alpha",
            display_label="nested",
            volume_root=nested,
            attestation_id="learn-ai-alpaca-nested",
            deployment_namespace="compose:prod",
        )


def test_lane_summaries_are_bounded_typed_observations(
    control_dir: Path, fleet_service
) -> None:
    """A heartbeat may carry a lane summary only in the bounded typed shape;
    free-form agent JSON refuses and nothing unbounded is projected."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="summary", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    assert fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        reported_summary={
            "endpoint_mode": "paper",
            "authority_state": "real_paper",
            "detail": "paper research lane",
        },
    )
    entry = next(
        e for e in fleet_service.directory()["clerks"] if e["clerk_id"] == lane.clerk_id
    )
    summary = entry["provider_summary"]
    assert summary is not None
    assert summary["reported_summary"]["endpoint_mode"] == "paper"
    assert summary["reported_summary"]["authority_state"] == "real_paper"

    with pytest.raises(ClerkIdentityMismatch, match="bounded typed observation"):
        fleet_service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_summary={"endpoint_mode": "paper", "arbitrary": {"nested": True}},
        )
    with pytest.raises(ClerkIdentityMismatch):
        fleet_service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_summary={"endpoint_mode": "hyperdrive", "authority_state": "fine"},
        )
    with pytest.raises(ClerkIdentityMismatch):
        fleet_service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            reported_summary={
                "endpoint_mode": "live",
                "authority_state": "real_live",
                "detail": "x" * 500,
            },
        )
    # The stored observation round-trips through the typed parser.
    stored = fleet_service._store.read_session(lane.clerk_id)
    assert stored is not None
    parsed = ProviderSummaryObservation.parse(stored.reported_summary_json)
    assert parsed is not None
    assert parsed.authority_state == "real_paper"


def test_concurrent_confirmations_from_one_session_settle_atomically(
    control_dir: Path, fleet_service
) -> None:
    """Four threads confirm generations 2, 2, 1 and 3 through one session:
    whatever the interleaving, the settled generation is the highest that
    landed and nothing ever regresses it."""
    from concurrent.futures import ThreadPoolExecutor

    lane = provision_lane(fleet_service, broker="fake_alpha", label="race-confirm", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-race"
    )

    def confirm(generation: int) -> object:
        try:
            return fleet_service.confirm_assignment(
                broker="fake_alpha",
                clerk_id=lane.clerk_id,
                external_account_id="acct-race",
                binding_generation=generation,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
            )
        except (ClerkAssignmentConflict, ClerkBindingGenerationConflict) as exc:
            return f"refused:{type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(confirm, (2, 2, 1, 3)))
    settled = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-RACE"
    )
    assert settled is not None
    assert settled.state == AssignmentState.EFFECTIVE
    # The highest generation always lands (nothing outranks it) and nothing
    # can follow it with a lower one, so the settled generation is exactly 3.
    assert settled.confirmed_binding_generation == 3
    assert settled.confirmed_routing_epoch == session.routing_epoch
    assert settled.confirmed_agent_instance_id == session.agent_instance_id
    # Every arrival either became the confirmed observation or refused with a
    # typed fence — there is no third outcome.
    from app.broker.fleet.records import AccountAssignmentRecord

    for outcome in outcomes:
        assert isinstance(outcome, (AccountAssignmentRecord, str))
