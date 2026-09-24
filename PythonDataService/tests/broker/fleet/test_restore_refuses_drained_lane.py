"""A registry restore never re-seats a lane its own volume says is drained (#2350).

The seam-hunt prototype (#2298) restored a registry backup captured while lane
L was effective on ``PAPER``. After that backup L was drained, proved quiet,
released and retired, and successor M took ``PAPER``. ``reconcile-registry``
then accepted L's *drained tombstone* as confirmation evidence: the hold
cleared, the restored registry routed ``PAPER`` to the retired lane, and M,
the lane really holding the account, was unknown to the fleet.

The tombstone is proof the lane is out, never a reason to re-seat it. These
tests drive the real registry, ``FleetControlService``, the backup/restore
ceremony and ``reconcile_restored_lane`` (plus the ``reconcile-registry`` CLI
over the production provider set); the only fakes are the conftest provider
adapters and the frozen clock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    mark_confirmation_evidence_draining,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.errors import (
    ClerkUnreachable,
    FleetRegistryBackupPredatesDrain,
    FleetRegistryRecoveryPending,
)
from app.broker.fleet.provider import OperationReadiness
from app.broker.fleet.records import AssignmentState, StoredLifecycleState
from app.broker.fleet.recovery import (
    create_registry_backup,
    read_recovery_state,
    reconcile_restored_lane,
    restore_registry_backup,
)
from app.broker.fleet.schema import SCHEMA_VERSION
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet_composition import production_provider_adapters
from scripts.manage_broker_fleet import main
from tests.broker.fleet.conftest import (
    TEST_CHANGE_REF,
    TEST_OPERATOR,
    FrozenClock,
    Lane,
    bind_lane,
    provision_lane,
)

_PROVIDER_SUMMARY = {"endpoint_mode": "paper", "authority_state": "ready"}


def _write_evidence(
    service: FleetControlService,
    lane: Lane,
    *,
    account: str,
    agent_instance_id: str,
    routing_epoch: int,
    assignment_generation: int,
) -> None:
    """Write the exact grant the lane confirmed, as the lane itself does."""
    clerk = service._store.read_clerk(lane.clerk_id)
    assert clerk is not None
    write_confirmation_evidence(
        lane.volume_root,
        ConfirmationEvidence(
            clerk_id=lane.clerk_id,
            volume_id=clerk.volume_id,
            registry_id=service._store.registry_id,
            assignment_generation=assignment_generation,
            canonical_account_id=account,
            binding_generation=1,
            effective_profile_id=None,
            effective_revision=None,
            confirmed_at_ms=service._clock(),
            agent_instance_id=agent_instance_id,
            routing_epoch=routing_epoch,
        ),
    )


def _bind_with_evidence(service: FleetControlService, lane: Lane, *, account: str):
    """Bind a lane and write its confirmation evidence; return the session."""
    session, assignment = bind_lane(service, lane, account=account)
    _write_evidence(
        service,
        lane,
        account=assignment.canonical_external_account_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        assignment_generation=assignment.assignment_generation,
    )
    return session


def _drain_and_retire(
    service: FleetControlService, clock: FrozenClock, lane: Lane, session, *, account: str
) -> None:
    """The full ADR 0063 handover: drain, tombstone, quiet, release, retire."""
    drained = service.drain_clerk(clerk_id=lane.clerk_id)
    assert mark_confirmation_evidence_draining(lane.volume_root)
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    service.confirm_lane_quiet(
        clerk_id=lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        observed_at_ms=clock(),
        runner_idle=True,
        broker_work_ended=True,
        account_flat=True,
        intents_resolved=True,
    )
    service.release_assignment(
        broker=lane.broker,
        external_account_id=account,
        expected_assignment_generation=1,
        operator=TEST_OPERATOR,
        change_ref=TEST_CHANGE_REF,
    )
    service.retire_clerk(clerk_id=lane.clerk_id)


def _reopen(control_dir: Path, adapters: dict, clock: FrozenClock) -> FleetControlService:
    """The coordinator restarted on the restored registry file."""
    return FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters=adapters,
        clock=clock,
    )


def test_reconcile_refuses_a_drained_lane_whose_successor_the_backup_predates(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """#2298's exact sequence: the hold stays closed and PAPER routes nowhere."""
    old = provision_lane(fleet_service, broker="fake_alpha", label="L", tmp_path=control_dir.parent)
    old_session = _bind_with_evidence(fleet_service, old, account="PAPER")
    backup = control_dir.parent / "backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    assert manifest.active_clerk_ids == (old.clerk_id,)

    clock.advance(1000)
    _drain_and_retire(fleet_service, clock, old, old_session, account="PAPER")
    new = provision_lane(fleet_service, broker="fake_alpha", label="M", tmp_path=control_dir.parent)
    new_session = fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=new.clerk_id, worker_key=new.worker_key
    )
    # A released account is a transfer: the successor proves its own volume.
    fleet_service.reserve_assignment(
        broker="fake_alpha",
        clerk_id=new.clerk_id,
        external_account_id="PAPER",
        volume_root=new.volume_root,
    )
    confirmed = fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=new.clerk_id,
        external_account_id="PAPER",
        binding_generation=1,
        agent_instance_id=new_session.agent_instance_id,
        routing_epoch=new_session.routing_epoch,
    )
    _write_evidence(
        fleet_service,
        new,
        account="PAPER",
        agent_instance_id=new_session.agent_instance_id,
        routing_epoch=new_session.routing_epoch,
        assignment_generation=confirmed.assignment_generation,
    )
    adapters = dict(fleet_service._provider_adapters)
    fleet_service.close()

    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    service = _reopen(control_dir, adapters, clock)
    try:
        with pytest.raises(FleetRegistryBackupPredatesDrain) as refused:
            reconcile_restored_lane(
                service,
                clerk_id=old.clerk_id,
                volume_root=old.volume_root,
                provider_summary=_PROVIDER_SUMMARY,
            )
        # Same refusal family and status as every other open hold.
        assert isinstance(refused.value, FleetRegistryRecoveryPending)
        assert refused.value.reason == "fleet_registry_recovery_pending"
        assert "newer" in refused.value.message

        state = read_recovery_state(control_dir)
        assert state is not None
        assert state.mutations_closed is True
        assert old.clerk_id not in state.reconciled_clerk_ids
        # The tombstone is untouched: it stays the auditable reason L is out.
        evidence = read_confirmation_evidence(old.volume_root)
        assert evidence is not None
        assert evidence.lifecycle_state == StoredLifecycleState.DRAINING.value
        # Nothing routes to the drained lane, and it cannot re-confirm itself
        # back in by restarting against the coordinator.
        with pytest.raises(FleetRegistryRecoveryPending):
            service.resolve_route(
                broker="fake_alpha", clerk_id=old.clerk_id, readiness=OperationReadiness.EXECUTION
            )
        session = service.register_agent_session(
            fleet_protocol_version=2, clerk_id=old.clerk_id, worker_key=old.worker_key
        )
        with pytest.raises(FleetRegistryRecoveryPending):
            service.confirm_assignment(
                broker="fake_alpha",
                clerk_id=old.clerk_id,
                external_account_id="PAPER",
                binding_generation=1,
                agent_instance_id=session.agent_instance_id,
                routing_epoch=session.routing_epoch,
            )
    finally:
        service.close()


def test_reconcile_refuses_a_lane_drained_after_the_backup_even_without_a_successor(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """The restored row says provisioned; the volume says drained. The volume wins."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="L", tmp_path=control_dir.parent)
    _bind_with_evidence(fleet_service, lane, account="PAPER")
    backup = control_dir.parent / "backup"
    create_registry_backup(fleet_service._store, backup_dir=backup)
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    assert mark_confirmation_evidence_draining(lane.volume_root)
    adapters = dict(fleet_service._provider_adapters)
    fleet_service.close()

    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    service = _reopen(control_dir, adapters, clock)
    try:
        with pytest.raises(FleetRegistryBackupPredatesDrain):
            reconcile_restored_lane(
                service,
                clerk_id=lane.clerk_id,
                volume_root=lane.volume_root,
                provider_summary=_PROVIDER_SUMMARY,
            )
        state = read_recovery_state(control_dir)
        assert state is not None and state.mutations_closed is True
    finally:
        service.close()


def test_a_backup_captured_mid_drain_still_reconciles_without_turning_the_lane_on(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Registry and volume agree the lane is draining: the hold clears, the lane stays down.

    Refusing every tombstone would make a backup captured during a drain
    unrestorable forever. When the restored registry already records the
    drain, reconciling cannot turn the lane back on, so it is admitted.
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="L", tmp_path=control_dir.parent)
    _bind_with_evidence(fleet_service, lane, account="PAPER")
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    assert mark_confirmation_evidence_draining(lane.volume_root)
    backup = control_dir.parent / "backup"
    manifest = create_registry_backup(fleet_service._store, backup_dir=backup)
    assert manifest.active_clerk_ids == (lane.clerk_id,)
    adapters = dict(fleet_service._provider_adapters)
    fleet_service.close()

    restore_registry_backup(control_dir=control_dir, backup_dir=backup, max_schema_version=SCHEMA_VERSION)
    service = _reopen(control_dir, adapters, clock)
    try:
        state = reconcile_restored_lane(
            service,
            clerk_id=lane.clerk_id,
            volume_root=lane.volume_root,
            provider_summary=_PROVIDER_SUMMARY,
        )
        assert state.mutations_closed is False
        clerk = service._store.read_clerk(lane.clerk_id)
        assert clerk is not None and clerk.lifecycle_state == StoredLifecycleState.DRAINING
        assignment = service._store.read_assignment(broker="fake_alpha", canonical_account_id="PAPER")
        assert assignment is not None and assignment.state == AssignmentState.EFFECTIVE
        with pytest.raises(ClerkUnreachable):
            service.resolve_route(
                broker="fake_alpha", clerk_id=lane.clerk_id, readiness=OperationReadiness.EXECUTION
            )
    finally:
        service.close()


def test_reconcile_registry_cli_refuses_the_drained_lane_and_reports_the_hold(
    tmp_path: Path, capsys
) -> None:
    """The operator surface: exit 2, the hold's reason, and a message naming the action."""
    control_dir = tmp_path / "control"
    clock = FrozenClock()
    adapters = production_provider_adapters()
    service = _reopen(control_dir, adapters, clock)
    lane = provision_lane(service, broker="alpaca", label="paper", tmp_path=tmp_path)
    _bind_with_evidence(service, lane, account="paper-acct")
    backup = tmp_path / "backup"
    create_registry_backup(service._store, backup_dir=backup)
    service.drain_clerk(clerk_id=lane.clerk_id)
    assert mark_confirmation_evidence_draining(lane.volume_root)
    service.close()

    assert (
        main(["restore-registry", "--control-dir", str(control_dir), "--backup-dir", str(backup)])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "reconcile-registry",
                "--control-dir",
                str(control_dir),
                "--clerk-id",
                lane.clerk_id,
                "--volume-root",
                str(lane.volume_root),
                "--provider-summary",
                json.dumps(_PROVIDER_SUMMARY),
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error.startswith("fleet_registry_recovery_pending:")
    assert "drained" in error and "restore-registry" in error
    state = read_recovery_state(control_dir)
    assert state is not None and state.mutations_closed is True
