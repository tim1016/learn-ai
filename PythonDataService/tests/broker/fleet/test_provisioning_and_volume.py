"""Provisioning, volume identity and the fail-before-authority gate (PRD §9.3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    BrokerNotSupported,
    ClerkNotFound,
    ClerkVolumeAlreadyRegistered,
    ClerkVolumeCloneDetected,
    ClerkVolumeIdentityMismatch,
    ClerkVolumeIdentityMissing,
    ClerkVolumeMountUnproven,
)
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet.volume import marker_path, read_volume_marker
from tests.broker.fleet.conftest import FrozenClock, Lane, provision_lane


def test_provision_mints_distinct_identities_and_marks_the_volume(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    alpha = provision_lane(fleet_service, broker="fake_alpha", label="alpha", tmp_path=control_dir.parent)
    beta = provision_lane(fleet_service, broker="fake_beta", label="beta", tmp_path=control_dir.parent)

    assert alpha.clerk_id != beta.clerk_id
    assert alpha.worker_key != beta.worker_key
    marker = read_volume_marker(alpha.volume_root)
    assert marker is not None
    assert marker.broker == "fake_alpha"
    assert marker.clerk_id == alpha.clerk_id
    assert marker.created_at_ms == clock()


def test_provisioning_an_unknown_production_provider_fails_closed(
    control_dir: Path, clock: FrozenClock
) -> None:
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={},  # the production set is empty in this slice
        clock=clock,
    )
    try:
        root = control_dir.parent / "volumes" / "tradier"
        root.mkdir(parents=True)
        with pytest.raises(BrokerNotSupported):
            service.provision_clerk(
                broker="tradier", display_label="Tradier", volume_root=root
            )
        assert read_volume_marker(root) is None
    finally:
        service.close()


def test_a_copied_volume_refuses_before_any_identity_is_minted(
    control_dir: Path, fleet_service
) -> None:
    original = provision_lane(
        fleet_service, broker="fake_alpha", label="original", tmp_path=control_dir.parent
    )
    copy_root = control_dir.parent / "volumes" / "copied"
    copy_root.mkdir(parents=True)
    # A byte-for-byte volume copy: the original's marker came along.
    shutil.copyfile(marker_path(original.volume_root), marker_path(copy_root))
    with pytest.raises(ClerkVolumeCloneDetected):
        fleet_service.provision_clerk(
            broker="fake_alpha",
            display_label="clone",
            volume_root=copy_root,
            attestation_id="vol-copied",
        )
    # And the registry never learned about the attempted clone.
    assert len(fleet_service._store.list_clerks()) == 1


def test_a_second_clerk_cannot_reuse_an_attested_volume(control_dir: Path, fleet_service) -> None:
    provision_lane(
        fleet_service,
        broker="fake_alpha",
        label="first",
        tmp_path=control_dir.parent,
        attestation_id="shared-volume",
    )
    root = control_dir.parent / "volumes" / "second"
    root.mkdir(parents=True)
    with pytest.raises(ClerkVolumeAlreadyRegistered):
        fleet_service.provision_clerk(
            broker="fake_beta",
            display_label="second",
            volume_root=root,
            attestation_id="shared-volume",
        )


def test_writable_subtrees_of_one_mounted_volume_never_host_two_clerks(
    control_dir: Path, fleet_service
) -> None:
    """FR-020/021: distinct attestations do not make nested roots distinct
    physical volumes — containment refuses in both directions."""
    parent_root = control_dir.parent / "volumes" / "one-physical-volume"
    parent_root.mkdir(parents=True)
    fleet_service.provision_clerk(
        broker="fake_alpha",
        display_label="paper",
        volume_root=parent_root,
        attestation_id="vol-paper",
    )
    sub_root = parent_root / "live"
    sub_root.mkdir()
    with pytest.raises(ClerkVolumeAlreadyRegistered, match="physical volume"):
        fleet_service.provision_clerk(
            broker="fake_alpha",
            display_label="live",
            volume_root=sub_root,
            attestation_id="vol-live",
        )
    # And a would-be parent around an existing clerk's root refuses too.
    container_root = control_dir.parent / "volumes"
    with pytest.raises(ClerkVolumeAlreadyRegistered):
        fleet_service.provision_clerk(
            broker="fake_beta",
            display_label="container",
            volume_root=container_root,
            attestation_id="vol-container",
        )


def test_verify_refuses_missing_marker_wrong_identity_and_symlinked_root(
    control_dir: Path, fleet_service
) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="lane", tmp_path=control_dir.parent
    )
    other = provision_lane(
        fleet_service, broker="fake_beta", label="other", tmp_path=control_dir.parent
    )

    # Missing marker: a volume that lost its identity never gains authority.
    marker_path(lane.volume_root).unlink()
    with pytest.raises(ClerkVolumeIdentityMissing):
        fleet_service.verify_clerk_volume(clerk_id=lane.clerk_id, volume_root=lane.volume_root)

    # Wrong identity: another clerk's marker sitting on this root is a copy
    # or a mis-mount, and the mismatch is named field by field.
    shutil.copyfile(marker_path(other.volume_root), marker_path(lane.volume_root))
    with pytest.raises(ClerkVolumeIdentityMismatch):
        fleet_service.verify_clerk_volume(clerk_id=lane.clerk_id, volume_root=lane.volume_root)

    # Symlinked root: a canonical name over a non-canonical mount refuses.
    marker_path(lane.volume_root).unlink()
    shutil.copyfile(marker_path(other.volume_root), marker_path(lane.volume_root))
    linked_root = control_dir.parent / "volumes" / "lane-link"
    linked_root.symlink_to(lane.volume_root)
    with pytest.raises(ClerkVolumeMountUnproven):
        fleet_service.verify_clerk_volume(clerk_id=other.clerk_id, volume_root=linked_root)


def test_registration_and_reservation_verify_the_volume_before_authority(
    control_dir: Path, fleet_service
) -> None:
    lane: Lane = provision_lane(
        fleet_service, broker="fake_alpha", label="gated", tmp_path=control_dir.parent
    )
    marker_path(lane.volume_root).unlink()
    with pytest.raises(ClerkVolumeIdentityMissing):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            agent_instance_id="agnt_aaaa0000aaaa0000aaaa0000",
            volume_root=lane.volume_root,
        )
    with pytest.raises(ClerkVolumeIdentityMissing):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            external_account_id="acct-1",
            volume_root=lane.volume_root,
        )


def test_retirement_is_terminal_and_refuses_outstanding_assignments(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="retiring", tmp_path=control_dir.parent
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-r"
    )
    from app.broker.fleet.errors import ClerkAssignmentConflict

    with pytest.raises(ClerkAssignmentConflict):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)

    fleet_service.release_assignment(
        broker="fake_alpha",
        external_account_id="acct-r",
        proof="old-clerk-offline-and-obligations-clear",
    )
    retired = fleet_service.retire_clerk(clerk_id=lane.clerk_id)
    assert str(retired.lifecycle_state) == "retired"

    # Terminal: a retired clerk never registers, reserves or routes again.

    with pytest.raises(ClerkNotFound):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id, worker_key=lane.worker_key
        )
    with pytest.raises(ClerkNotFound):
        fleet_service.reserve_assignment(
            broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-x"
        )
    with pytest.raises(ClerkNotFound):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)


def test_a_wrong_worker_key_never_registers(control_dir: Path, fleet_service) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="keyed", tmp_path=control_dir.parent
    )
    from app.broker.fleet.errors import ClerkIdentityMismatch

    with pytest.raises(ClerkIdentityMismatch):
        fleet_service.register_agent_session(
            clerk_id=lane.clerk_id, worker_key="wkrk_00000000000000000000000000000000"
        )
