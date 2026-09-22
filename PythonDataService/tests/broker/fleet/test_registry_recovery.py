"""Delivery E registry recovery, reassignment and D-compatible rollback exercises."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.fleet import recovery as recovery_module
from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    ConfirmationEvidenceError,
    write_confirmation_evidence,
)
from app.broker.fleet.errors import (
    ClerkAssignmentConflict,
    ClerkIdentityMismatch,
    ClerkReassignmentBlocked,
    ClerkUnreachable,
    FleetRegistryRecoveryPending,
    FleetRegistryUnavailable,
)
from app.broker.fleet.provider import OperationReadiness
from app.broker.fleet.recovery import (
    BACKUP_MANIFEST_FILENAME,
    D_COMPATIBLE_SCHEMA_VERSION,
    closeout_empty_registry_recovery,
    create_registry_backup,
    read_recovery_state,
    reconcile_restored_lane,
    restore_registry_backup,
)
from app.broker.fleet.schema import SCHEMA_VERSION
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.fleet.conftest import (
    TEST_CHANGE_REF,
    TEST_OPERATOR,
    FrozenClock,
    bind_lane,
    downgrade_backup_to_v2,
    provision_lane,
)


def _write_lane_evidence(
    service: FleetControlService,
    *,
    lane_root: Path,
    clerk_id: str,
    session_id: str,
    routing_epoch: int,
    account: str,
    assignment_generation: int,
    binding_generation: int,
) -> None:
    """Write the same nonsecret grant the original lane actually confirmed."""
    clerk = service._store.read_clerk(clerk_id)
    assert clerk is not None
    write_confirmation_evidence(
        lane_root,
        ConfirmationEvidence(
            clerk_id=clerk_id,
            volume_id=clerk.volume_id,
            registry_id=service._store.registry_id,
            assignment_generation=assignment_generation,
            canonical_account_id=account,
            binding_generation=binding_generation,
            effective_profile_id=None,
            effective_revision=None,
            confirmed_at_ms=1_789_000_000_000,
            agent_instance_id=session_id,
            routing_epoch=routing_epoch,
        ),
    )


def test_registry_restore_holds_routing_and_assignment_mutation_closed_until_all_lanes_reconcile(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """An old registry never reopens solely because its SQLite file was restored."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="live", tmp_path=control_dir.parent)
    first_session, first_assignment = bind_lane(fleet_service, first, account="PAPER", binding_generation=2)
    second_session, second_assignment = bind_lane(fleet_service, second, account="LIVE", binding_generation=3)
    _write_lane_evidence(
        fleet_service,
        lane_root=first.volume_root,
        clerk_id=first.clerk_id,
        session_id=first_session.agent_instance_id,
        routing_epoch=first_session.routing_epoch,
        account="PAPER",
        assignment_generation=first_assignment.assignment_generation,
        binding_generation=2,
    )
    _write_lane_evidence(
        fleet_service,
        lane_root=second.volume_root,
        clerk_id=second.clerk_id,
        session_id=second_session.agent_instance_id,
        routing_epoch=second_session.routing_epoch,
        account="LIVE",
        assignment_generation=second_assignment.assignment_generation,
        binding_generation=3,
    )
    backup = control_dir.parent / "registry-backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    registry_id = fleet_service._store.registry_id
    fleet_service.close()

    restored = restore_registry_backup(
        control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION
    )
    assert restored.registry_id == registry_id == manifest.registry_id
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
        clock=clock,
    )
    try:
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(broker="fake_alpha", clerk_id=first.clerk_id)
        with pytest.raises(FleetRegistryRecoveryPending):
            service.reserve_assignment(
                broker="fake_alpha", clerk_id=first.clerk_id, external_account_id="OTHER"
            )

        partial = reconcile_restored_lane(
            service,
            clerk_id=first.clerk_id,
            volume_root=first.volume_root,
            provider_summary={"endpoint_mode": "paper", "authority_state": "ready"},
        )
        assert partial.mutations_closed is True
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(broker="fake_alpha", clerk_id=first.clerk_id)

        complete = reconcile_restored_lane(
            service,
            clerk_id=second.clerk_id,
            volume_root=second.volume_root,
            provider_summary={"endpoint_mode": "live", "authority_state": "ready"},
        )
        assert complete.mutations_closed is False
        assert read_recovery_state(control_dir) == complete
        assignment = service._store.read_assignment(broker="fake_alpha", canonical_account_id="PAPER")
        assert assignment is not None
        assert assignment.confirmed_binding_generation == 2
        assert assignment.confirmed_routing_epoch == first_session.routing_epoch
        # The original session can route only after every lane in the backup,
        # including the other broker-qualified account, reconciles.
        _, _, resolved = service.resolve_route(
            broker="fake_alpha",
            clerk_id=first.clerk_id,
            readiness=OperationReadiness.EXECUTION,
        )
        assert resolved is not None
        assert resolved.canonical_external_account_id == "PAPER"
    finally:
        service.close()


def test_pre_restore_store_connection_stays_fenced_after_reconciliation(
    control_dir: Path,
    fleet_service: FleetControlService,
) -> None:
    """An old coordinator cannot resume through its replaced SQLite inode."""
    lane = provision_lane(
        fleet_service,
        broker="fake_alpha",
        label="paper",
        tmp_path=control_dir.parent,
    )
    session, assignment = bind_lane(fleet_service, lane, account="PAPER")
    _write_lane_evidence(
        fleet_service,
        lane_root=lane.volume_root,
        clerk_id=lane.clerk_id,
        session_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        account="PAPER",
        assignment_generation=assignment.assignment_generation,
        binding_generation=1,
    )
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)

    # Deliberately violate the runbook's shutdown step. Recovery replaces the
    # path while this simulated old coordinator still owns an open connection.
    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    replacement = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
    )
    try:
        complete = reconcile_restored_lane(
            replacement,
            clerk_id=lane.clerk_id,
            volume_root=lane.volume_root,
            provider_summary={"endpoint_mode": "paper", "authority_state": "ready"},
        )
        assert complete.mutations_closed is False

        with pytest.raises(FleetRegistryRecoveryPending, match="pre-restore"):
            fleet_service.resolve_route(
                broker="fake_alpha",
                clerk_id=lane.clerk_id,
                readiness=OperationReadiness.EXECUTION,
            )
    finally:
        replacement.close()


def test_registry_recovery_refuses_corrupted_lane_evidence(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """A malformed evidence file cannot be used to reopen an old registry."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    session, assignment = bind_lane(fleet_service, lane, account="PAPER")
    _write_lane_evidence(
        fleet_service,
        lane_root=lane.volume_root,
        clerk_id=lane.clerk_id,
        session_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        account="PAPER",
        assignment_generation=assignment.assignment_generation,
        binding_generation=1,
    )
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    fleet_service.close()
    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    (lane.volume_root / "fleet" / "confirmation.json").write_text("not-json", encoding="utf-8")
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
    )
    try:
        with pytest.raises(ConfirmationEvidenceError):
            reconcile_restored_lane(
                service,
                clerk_id=lane.clerk_id,
                volume_root=lane.volume_root,
                provider_summary={"endpoint_mode": "paper", "authority_state": "ready"},
            )
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    finally:
        service.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"assignment_generation": 0},
        {"binding_generation": 0},
        {"routing_epoch": 0},
        {"confirmed_at_ms": MAX_TIMESTAMP_MS + 1},
        {"effective_profile_id": "profile-without-revision"},
    ],
    ids=(
        "zero-assignment-generation",
        "zero-binding-generation",
        "zero-routing-epoch",
        "timestamp-above-int64-ms-boundary",
        "incoherent-profile-revision",
    ),
)
def test_registry_recovery_refuses_semantically_invalid_lane_evidence(
    control_dir: Path,
    fleet_service: FleetControlService,
    updates: dict[str, object],
) -> None:
    """Syntactically valid JSON cannot inject an invalid recovered grant."""
    lane = provision_lane(
        fleet_service,
        broker="fake_alpha",
        label="paper",
        tmp_path=control_dir.parent,
    )
    session, assignment = bind_lane(fleet_service, lane, account="PAPER")
    _write_lane_evidence(
        fleet_service,
        lane_root=lane.volume_root,
        clerk_id=lane.clerk_id,
        session_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        account="PAPER",
        assignment_generation=assignment.assignment_generation,
        binding_generation=1,
    )
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    fleet_service.close()
    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    evidence_path = lane.volume_root / "fleet" / "confirmation.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload.update(updates)
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
    )
    try:
        with pytest.raises(ConfirmationEvidenceError):
            reconcile_restored_lane(
                service,
                clerk_id=lane.clerk_id,
                volume_root=lane.volume_root,
                provider_summary={"endpoint_mode": "paper", "authority_state": "ready"},
            )
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    finally:
        service.close()


def test_registry_recovery_translates_residual_database_constraint_failure(
    control_dir: Path,
    fleet_service: FleetControlService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A residual SQLite constraint failure remains a typed recovery refusal."""
    lane = provision_lane(
        fleet_service,
        broker="fake_alpha",
        label="paper",
        tmp_path=control_dir.parent,
    )
    session, assignment = bind_lane(fleet_service, lane, account="PAPER")
    _write_lane_evidence(
        fleet_service,
        lane_root=lane.volume_root,
        clerk_id=lane.clerk_id,
        session_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        account="PAPER",
        assignment_generation=assignment.assignment_generation,
        binding_generation=1,
    )
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    fleet_service.close()
    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
    )
    try:
        def fail_constraint(*args, **kwargs) -> bool:
            del args, kwargs
            raise sqlite3.IntegrityError("injected constraint failure")

        monkeypatch.setattr(service._store, "cas_update_assignment", fail_constraint)
        with pytest.raises(ClerkIdentityMismatch, match="violates the registry protocol"):
            reconcile_restored_lane(
                service,
                clerk_id=lane.clerk_id,
                volume_root=lane.volume_root,
                provider_summary={"endpoint_mode": "paper", "authority_state": "ready"},
            )
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    finally:
        service.close()


def test_restore_writes_the_recovery_hold_before_database_replacement(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed database swap leaves even the old registry closed, never briefly open."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="PAPER")
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    target = fleet_service._store.db_path
    # The temporary state file uses os.replace too; fail only the final
    # database target replacement after the durable hold has been installed.
    replace = recovery_module.os.replace

    def fail_database_replace(source: Path, destination: Path) -> None:
        if destination == target:
            raise OSError("injected restore swap failure")
        replace(source, destination)

    monkeypatch.setattr(recovery_module.os, "replace", fail_database_replace)
    with pytest.raises(OSError, match="injected restore swap failure"):
        restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    state = read_recovery_state(control_dir)
    assert state is not None
    assert state.mutations_closed is True
    with pytest.raises(FleetRegistryRecoveryPending):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)


def test_restore_with_only_provisioned_lane_requires_explicit_host_closeout(
    control_dir: Path,
    fleet_service: FleetControlService,
) -> None:
    """An empty effective-assignment inventory is still a closed recovery hold."""
    lane = provision_lane(
        fleet_service,
        broker="fake_alpha",
        label="paper",
        tmp_path=control_dir.parent,
    )
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    fleet_service.close()
    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    store = FleetRegistryStore.open(control_dir=control_dir)
    service = FleetControlService(
        store=store,
        provider_adapters={"fake_alpha": fleet_service._provider_adapters["fake_alpha"]},
    )
    try:
        state = read_recovery_state(control_dir)
        assert state is not None
        assert state.required_clerk_ids == ()
        assert state.mutations_closed is True
        with pytest.raises(FleetRegistryRecoveryPending, match="host closeout"):
            service.reserve_assignment(
                broker="fake_alpha",
                clerk_id=lane.clerk_id,
                external_account_id="PAPER",
            )

        closed = closeout_empty_registry_recovery(
            store,
            control_dir=control_dir,
            operator="fleet-operator",
            change_ref="incident-2049",
        )
        assert closed.mutations_closed is False
        assert closed.empty_inventory_attestation is not None
        reserved = service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            external_account_id="PAPER",
        )
        assert reserved.state.value == "reserved"
    finally:
        service.close()


def test_restore_refuses_a_manifest_or_database_newer_than_delivery_d(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """D rollback never guesses how to open a schema written by a newer binary."""
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    manifest_path = backup / BACKUP_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database = backup / "fleet-registry.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE fleet_meta SET schema_version = 3 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()
    from app.broker.fleet.recovery import _sha256

    manifest["registry_schema_version"] = 3
    manifest["database_sha256"] = _sha256(database)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FleetRegistryUnavailable, match="newer"):
        restore_registry_backup(
            control_dir=control_dir,
            backup_dir=backup,
            max_schema_version=D_COMPATIBLE_SCHEMA_VERSION,
        )


def test_same_owner_restart_preserves_identity_and_reassignment_is_blocked(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Restart does not remint ownership; explicit reassignment is blocked
    against a drained lane until #2154 closes (ADR 0063 §4.1/§7.1), leaving
    the original ownership intact and unroutable for a successor."""
    original = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    successor = provision_lane(fleet_service, broker="fake_alpha", label="live", tmp_path=control_dir.parent)
    _, confirmed = bind_lane(fleet_service, original, account="ACCOUNT", binding_generation=2)
    resumed = fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=original.clerk_id, external_account_id="ACCOUNT"
    )
    assert resumed == confirmed

    drained = fleet_service.drain_clerk(clerk_id=original.clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    with pytest.raises(ClerkReassignmentBlocked, match="#2154"):
        fleet_service.reassign_assignment(
            broker="fake_alpha",
            external_account_id="ACCOUNT",
            expected_assignment_generation=confirmed.assignment_generation,
            operator=TEST_OPERATOR,
            change_ref=TEST_CHANGE_REF,
            successor_clerk_id=successor.clerk_id,
            successor_volume_root=successor.volume_root,
        )
    unchanged = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCOUNT"
    )
    assert unchanged == confirmed
    with pytest.raises(ClerkUnreachable, match="no registered agent session"):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=successor.clerk_id)


def test_reassignment_successor_failure_rolls_back_the_original_owner(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second half of reassignment cannot strand a released old owner.

    Exercised at the store seam: the service ceremony is blocked against a
    drained lane until #2154 closes, but the transactional atomicity it will
    hand these records to is a property of the store and stays covered."""
    from dataclasses import replace

    from app.broker.fleet.records import AssignmentState

    original = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    successor = provision_lane(fleet_service, broker="fake_alpha", label="live", tmp_path=control_dir.parent)
    _, confirmed = bind_lane(fleet_service, original, account="ACCOUNT")
    store = fleet_service._store
    cas = store.cas_update_assignment
    attempts = 0

    def refuse_successor(*args, **kwargs) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            return False
        return cas(*args, **kwargs)

    released = replace(confirmed, state=AssignmentState.RELEASED)
    reserved = replace(
        confirmed,
        clerk_id=successor.clerk_id,
        assignment_generation=confirmed.assignment_generation + 1,
        state=AssignmentState.RESERVED,
        effective_profile_id=None,
        effective_revision=None,
        confirmed_binding_generation=None,
        confirmed_profile_id=None,
        confirmed_revision=None,
        confirmed_at_ms=None,
        confirmed_agent_instance_id=None,
        confirmed_routing_epoch=None,
    )
    monkeypatch.setattr(store, "cas_update_assignment", refuse_successor)
    with pytest.raises(sqlite3.IntegrityError, match="successor reservation refused"), store.transaction() as conn:
        store.reassign_assignment(
            conn,
            released=released,
            reserved_successor=reserved,
            previous_state=AssignmentState.EFFECTIVE,
        )
    unchanged = store.read_assignment(broker="fake_alpha", canonical_account_id="ACCOUNT")
    assert unchanged == confirmed


def test_reassignment_to_current_owner_refuses_without_releasing(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """The successor identity is checked before any ownership transition."""
    original = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    _, confirmed = bind_lane(fleet_service, original, account="ACCOUNT")
    with pytest.raises(ClerkAssignmentConflict, match="distinct successor"):
        fleet_service.reassign_assignment(
            broker="fake_alpha",
            external_account_id="ACCOUNT",
            expected_assignment_generation=confirmed.assignment_generation,
            operator=TEST_OPERATOR,
            change_ref=TEST_CHANGE_REF,
            successor_clerk_id=original.clerk_id,
            successor_volume_root=original.volume_root,
        )
    unchanged = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCOUNT"
    )
    assert unchanged == confirmed


def test_d_compatible_rollback_restores_exact_registry_without_reminting_identities(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """The D-compatible restore uses v2 evidence and still starts with a hold."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    backup = control_dir.parent / "backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    original_id = lane.clerk_id
    fleet_service.close()
    downgrade_backup_to_v2(backup)
    restored = restore_registry_backup(
        control_dir=control_dir,
        backup_dir=backup,
        max_schema_version=D_COMPATIBLE_SCHEMA_VERSION,
    )
    # The rollback places the evidence as it stands — a D-era binary opens a v2
    # registry — and this build migrates the same file forward when it opens it.
    assert restored.registry_schema_version == D_COMPATIBLE_SCHEMA_VERSION
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        clerk = store.read_clerk(original_id)
        assert clerk is not None
        assert clerk.clerk_id == original_id
        assert store.registry_id == manifest.registry_id
        assert store.schema_version == SCHEMA_VERSION
    finally:
        store.close()


def test_normal_restore_accepts_a_v2_backup_without_reminting_identities(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """The normal restore path accepts pre-upgrade evidence too; only the max
    schema bound distinguishes it from a D-compatible rollback."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    backup = control_dir.parent / "backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    original_id = lane.clerk_id
    fleet_service.close()
    downgrade_backup_to_v2(backup)
    restored = restore_registry_backup(
        control_dir=control_dir,
        backup_dir=backup,
        max_schema_version=SCHEMA_VERSION,
    )
    # The evidence is placed as it stands — a v2 backup restored under the
    # normal path still reports v2 — and this build migrates the same file
    # forward the next time it opens it.
    assert restored.registry_schema_version == D_COMPATIBLE_SCHEMA_VERSION
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        clerk = store.read_clerk(original_id)
        assert clerk is not None
        assert clerk.clerk_id == original_id
        assert store.registry_id == manifest.registry_id
        assert store.schema_version == SCHEMA_VERSION
    finally:
        store.close()


def test_a_current_build_backup_is_not_d_compatible_rollback_evidence(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """#2073b: v3 carries a fence a D-era binary cannot enforce, so a backup
    this build takes is not rollback evidence for that binary — the ceremony
    refuses rather than reopening a registry whose invariant would go
    unenforced. A D rollback needs evidence captured before the upgrade."""
    provision_lane(fleet_service, broker="fake_alpha", label="paper", tmp_path=control_dir.parent)
    backup = control_dir.parent / "backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    assert manifest.registry_schema_version == SCHEMA_VERSION
    fleet_service.close()
    with pytest.raises(FleetRegistryUnavailable, match="newer"):
        restore_registry_backup(
            control_dir=control_dir,
            backup_dir=backup,
            max_schema_version=D_COMPATIBLE_SCHEMA_VERSION,
        )
