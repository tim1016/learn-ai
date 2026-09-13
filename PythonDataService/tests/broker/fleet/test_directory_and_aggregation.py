"""The broker-neutral directory and provenance-preserving aggregation (PRD §9.9, §10.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import ClerkUnreachable
from tests.broker.fleet.conftest import (
    FAKE_ALPHA_CAPABILITIES,
    FAKE_BETA_CAPABILITIES,
    FrozenClock,
    provision_lane,
)

#: Every field the directory may carry. Anything else — especially a balance,
#: position, P&L or exposure quantity — is a fleet-computed financial fact,
#: which FR-034 forbids.
_ALLOWED_DIRECTORY_FIELDS = {
    "broker",
    "clerk_id",
    "display_label",
    "lifecycle_state",
    "volume_id",
    "last_seen_at_ms",
    "routing_epoch",
    "effective_binding_generation",
    "capabilities",
    "provider_summary",
    "observed_at_ms",
}


def _live(fleet_service, tmp_path: Path, broker: str, label: str):
    """Provision, register, reserve and confirm one lane so it projects ready."""
    lane = provision_lane(fleet_service, broker=broker, label=label, tmp_path=tmp_path)
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker=broker, clerk_id=lane.clerk_id, external_account_id=f"acct-{label}"
    )
    fleet_service.confirm_assignment(
        broker=broker,
        clerk_id=lane.clerk_id,
        external_account_id=f"acct-{label}",
        binding_generation=3,
    )
    return lane


def test_every_entry_carries_broker_clerk_identity_and_no_internal_secrets(
    control_dir: Path, fleet_service
) -> None:
    """Directory entries carry exactly the allowed fields and never the worker key."""
    alpha = _live(fleet_service, control_dir.parent, "fake_alpha", "paper")
    beta = _live(fleet_service, control_dir.parent, "fake_beta", "live")
    directory = fleet_service.directory()

    assert set(directory) == {"observed_at_ms", "clerks"}
    entries = {entry["clerk_id"]: entry for entry in directory["clerks"]}
    assert set(entries) == {alpha.clerk_id, beta.clerk_id}

    for entry in entries.values():
        assert set(entry) == _ALLOWED_DIRECTORY_FIELDS
        rendered = repr(entry)
        # The worker key never crosses the public projection (FR-012).
        for lane in (alpha, beta):
            assert lane.worker_key not in rendered

    alpha_entry = entries[alpha.clerk_id]
    assert alpha_entry["broker"] == "fake_alpha"
    assert alpha_entry["lifecycle_state"] == "ready"
    assert alpha_entry["effective_binding_generation"] == 3
    assert alpha_entry["capabilities"] == sorted(cap.value for cap in FAKE_ALPHA_CAPABILITIES)
    assert alpha_entry["provider_summary"]["provider_id"] == "fake_alpha"
    assert entries[beta.clerk_id]["capabilities"] == sorted(
        cap.value for cap in FAKE_BETA_CAPABILITIES
    )


def test_capability_evidence_differs_by_provider_and_undeclared_actions_refuse(
    control_dir: Path, fleet_service
) -> None:
    """Capability evidence is per provider; no parity is inferred in either direction."""
    from app.broker.fleet.errors import BrokerClerkCapabilityUnavailable
    from app.broker.fleet.provider import Capability

    _live(fleet_service, control_dir.parent, "fake_alpha", "alpha")
    _live(fleet_service, control_dir.parent, "fake_beta", "beta")

    # Both declare account_read…
    fleet_service.require_capability(broker="fake_alpha", capability=Capability.ACCOUNT_READ)
    fleet_service.require_capability(broker="fake_beta", capability=Capability.ACCOUNT_READ)
    # …but gallery_read is beta's alone, and bot_action alpha's — no parity is
    # inferred in either direction (FR-006).
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_alpha", capability=Capability.GALLERY_READ)
    with pytest.raises(BrokerClerkCapabilityUnavailable):
        fleet_service.require_capability(broker="fake_beta", capability=Capability.BOT_ACTION)


def test_lifecycle_projects_from_observations_not_stored_flags(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """FR-081: a historical acknowledgement never presents as current liveness."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="projecting", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)

    def entry() -> dict:
        """The directory entry for the projecting clerk, re-read per assertion."""
        return next(
            e for e in fleet_service.directory()["clerks"] if e["clerk_id"] == lane.clerk_id
        )

    assert entry()["lifecycle_state"] == "starting"  # session, no binding acknowledged

    fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=fleet_service._store.read_session(lane.clerk_id).agent_instance_id,
        reported_binding_generation=1,
        reported_state="binding_confirmed",
    )
    assert entry()["lifecycle_state"] == "ready"

    fleet_service.observe_session(
        clerk_id=lane.clerk_id,
        agent_instance_id=fleet_service._store.read_session(lane.clerk_id).agent_instance_id,
        reported_state="degraded",
    )
    assert entry()["lifecycle_state"] == "degraded"

    clock.advance(60_000)  # far past the 30 s staleness window
    assert entry()["lifecycle_state"] == "unreachable"


def test_retired_lanes_leave_the_default_directory(control_dir: Path, fleet_service) -> None:
    """Retired lanes disappear from the default directory and stay marked when included."""
    keeper = _live(fleet_service, control_dir.parent, "fake_alpha", "keeper")
    retiring = _live(fleet_service, control_dir.parent, "fake_alpha", "retiring")
    retiring_assignment = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-RETIRING"
    )
    assert retiring_assignment is not None
    fleet_service.release_assignment(
        broker="fake_alpha",
        external_account_id="acct-retiring",
        expected_assignment_generation=retiring_assignment.assignment_generation,
        proof="old-clerk-offline-and-obligations-clear",
    )
    fleet_service.retire_clerk(clerk_id=retiring.clerk_id)

    default = fleet_service.directory()
    assert [entry["clerk_id"] for entry in default["clerks"]] == [keeper.clerk_id]
    with_retired = fleet_service.directory(include_retired=True)
    assert {entry["lifecycle_state"] for entry in with_retired["clerks"]} == {"ready", "retired"}


def test_partial_aggregation_reports_each_lane_without_omission_or_substitution(
    control_dir: Path, fleet_service
) -> None:
    """One lane's failure is reported explicitly and never fails the healthy lane."""
    healthy = _live(fleet_service, control_dir.parent, "fake_alpha", "healthy")
    broken = _live(fleet_service, control_dir.parent, "fake_beta", "broken")
    result = fleet_service.aggregate_lane_reads(
        [
            (
                "fake_alpha",
                healthy.clerk_id,
                lambda: {"observation": "alpha-facts"},
            ),
            (
                "fake_beta",
                broken.clerk_id,
                lambda: (_ for _ in ()).throw(ClerkUnreachable("agent timed out")),
            ),
        ]
    )

    lanes = result["lanes"]
    assert [lane["clerk_id"] for lane in lanes] == [healthy.clerk_id, broken.clerk_id]
    assert lanes[0] == {
        "broker": "fake_alpha",
        "clerk_id": healthy.clerk_id,
        "ok": True,
        "value": {"observation": "alpha-facts"},
    }
    # The failed lane is explicit partial failure: reported, not omitted, and
    # it did not fail the healthy lane (FR-084).
    assert lanes[1]["ok"] is False
    assert lanes[1]["broker"] == "fake_beta"
    assert lanes[1]["error_reason"] == "clerk_unreachable"
