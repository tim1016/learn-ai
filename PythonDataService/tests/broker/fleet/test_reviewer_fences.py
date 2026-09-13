"""Fences added in response to the PR's external review round.

Each test here pins one review finding: assignment-history auditability,
registration serialization, idempotency-key identity, adapter-registry
identity, provisioning compensation, heartbeat freshness at the route gate,
and malformed-marker typing.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    BrokerNotSupported,
    ClerkAssignmentConflict,
    ClerkIdentityMismatch,
    ClerkUnreachable,
    ClerkVolumeIdentityMismatch,
)
from app.broker.fleet.records import AssignmentState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.broker.fleet.volume import marker_path
from tests.broker.fleet.conftest import (
    FakeProviderAdapter,
    FrozenClock,
    fake_alpha,
    provision_lane,
)

RELEASE_PROOF = "old-clerk-offline-and-obligations-clear"


def test_assignment_history_preserves_released_generations(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Re-reservation overwrites the pointer, never the audited past."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="h1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="h2", tmp_path=control_dir.parent)
    reserved = fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=first.clerk_id, external_account_id="acct-h"
    )
    fleet_service.release_assignment(
        broker="fake_alpha",
        external_account_id="acct-h",
        expected_assignment_generation=reserved.assignment_generation,
        proof=RELEASE_PROOF,
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=second.clerk_id, external_account_id="acct-h"
    )

    history = fleet_service._store.list_assignment_history(
        broker="fake_alpha", canonical_account_id="ACCT-H"
    )
    states = [(row.clerk_id, row.state) for row in history]
    assert states == [
        (first.clerk_id, AssignmentState.RESERVED),
        (first.clerk_id, AssignmentState.RELEASED),
        (second.clerk_id, AssignmentState.RESERVED),
    ]
    # The current pointer names only the new owner.
    current = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id="ACCT-H"
    )
    assert current is not None and current.clerk_id == second.clerk_id


def test_concurrent_registrations_serialize_into_distinct_epochs(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Two agents registering at once land on epochs 1 and 2, never both on 1."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="reg", tmp_path=control_dir.parent)

    def register(index: int) -> int:
        return fleet_service.register_agent_session(
            clerk_id=lane.clerk_id,
            worker_key=lane.worker_key,
            agent_instance_id=f"agnt_{index:024x}",
        ).routing_epoch

    with ThreadPoolExecutor(max_workers=2) as pool:
        epochs = sorted(pool.map(register, (1, 2)))

    assert epochs == [1, 2]
    history = fleet_service._store._query(
        "SELECT COUNT(*) AS n FROM clerk_session_history WHERE clerk_id = ?",
        (lane.clerk_id,),
    )
    assert int(history[0]["n"]) == 1


def test_an_idempotency_key_reused_for_another_target_refuses(
    control_dir: Path, fleet_service
) -> None:
    """A retry naming a different operation or target is a conflict, never a
    silent cross-attribution of the first attempt's correlation."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="idem", tmp_path=control_dir.parent)
    fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="key-1",
        state="delivered",
    )
    with pytest.raises(ClerkIdentityMismatch, match="cannot be reused"):
        fleet_service.record_routing_receipt(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation_kind="bot_action",
            nonsecret_target_ref="strategy/sid-2",
            idempotency_key="key-1",
            state="failed",
        )
    with pytest.raises(ClerkIdentityMismatch):
        fleet_service.record_routing_receipt(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            operation_kind="custody_read",
            nonsecret_target_ref="strategy/sid-1",
            idempotency_key="key-1",
            state="failed",
        )
    # The original receipt was not overwritten by either attempt.
    kept = fleet_service._store.find_routing_receipt_by_idempotency(
        broker="fake_alpha", clerk_id=lane.clerk_id, idempotency_key="key-1"
    )
    assert kept is not None and kept.nonsecret_target_ref == "strategy/sid-1"


def test_an_adapter_registered_under_another_provider_id_refuses(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """A registry mapping key must equal the adapter's own provider identity."""
    misregistered = FleetControlService(
        store=fleet_service._store,
        provider_adapters={"fake_beta": fake_alpha()},
        clock=clock,
    )
    with pytest.raises(BrokerNotSupported, match="own identity"):
        misregistered.provision_clerk(
            broker="fake_beta",
            display_label="misregistered",
            volume_root=control_dir.parent / "volumes" / "mis",
        ) or misregistered._adapter("fake_beta")


def test_a_failed_marker_write_retires_the_partial_clerk(
    control_dir: Path, clock: FrozenClock, fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provisioning that cannot mark the volume leaves no wedged lane."""
    from app.broker.fleet import service as service_module

    def broken_write(root: Path, marker: object) -> None:
        del root, marker
        raise OSError("volume is read-only")

    monkeypatch.setattr(service_module.volume_module, "write_volume_marker", broken_write)
    root = control_dir.parent / "volumes" / "doomed"
    root.mkdir(parents=True)
    with pytest.raises(OSError):
        fleet_service.provision_clerk(
            broker="fake_alpha", display_label="doomed", volume_root=root
        )

    # The partial clerk retired; its attestation is free for a clean retry.
    rows = fleet_service._store.list_clerks(include_retired=True)
    assert [clerk.lifecycle_state.value for clerk in rows] == ["retired"]
    monkeypatch.undo()
    retried = fleet_service.provision_clerk(
        broker="fake_alpha", display_label="retried", volume_root=root
    )
    assert marker_path(root).exists()
    assert retried.clerk.clerk_id != rows[0].clerk_id


def test_a_stale_heartbeat_is_not_routable_even_with_a_confirmed_binding(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Routing checks liveness, not just history: an old heartbeat refuses."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="fading", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-f"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-f",
        binding_generation=1,
    )
    fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)

    clock.advance(60_000)
    with pytest.raises(ClerkUnreachable, match="heartbeat"):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)


def test_a_malformed_marker_field_is_a_typed_refusal(
    control_dir: Path, fleet_service
) -> None:
    """Corrupted marker values refuse as identity mismatches, never as TypeErrors."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="corrupt", tmp_path=control_dir.parent
    )
    target = marker_path(lane.volume_root)
    payload = target.read_text(encoding="utf-8").replace('"marker_version":1', '"marker_version":null')
    target.write_text(payload, encoding="utf-8")
    with pytest.raises(ClerkVolumeIdentityMismatch, match="marker_version"):
        fleet_service.verify_clerk_volume(clerk_id=lane.clerk_id, volume_root=lane.volume_root)

    payload = target.read_text(encoding="utf-8").replace('"marker_version":null', '"marker_version":"1"')
    target.write_text(payload, encoding="utf-8")
    with pytest.raises(ClerkVolumeIdentityMismatch, match="marker_version"):
        fleet_service.verify_clerk_volume(clerk_id=lane.clerk_id, volume_root=lane.volume_root)


def test_retirement_races_a_reservation_without_leaving_an_orphan(
    control_dir: Path, clock: FrozenClock, fleet_service
) -> None:
    """Reservation rechecks the lifecycle inside its write transaction, so a
    retirement that lands mid-reserve produces a refusal, not a retired clerk
    owning an active assignment."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="race", tmp_path=control_dir.parent)
    rival_store = FleetRegistryStore.open(control_dir=control_dir)
    rival = FleetControlService(
        store=rival_store,
        provider_adapters={"fake_alpha": FakeProviderAdapter("fake_alpha")},
        clock=clock,
    )
    try:
        original_transaction = fleet_service._store.transaction
        retired = False

        def retire_before_my_write() -> None:
            nonlocal retired
            if not retired:
                retired = True
                rival.retire_clerk(clerk_id=lane.clerk_id)

        import contextlib

        @contextlib.contextmanager
        def interleaved():
            retire_before_my_write()
            with original_transaction() as conn:
                yield conn

        fleet_service._store.transaction = interleaved  # type: ignore[method-assign]
        try:
            with pytest.raises(ClerkAssignmentConflict, match="no longer provisioned"):
                fleet_service.reserve_assignment(
                    broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-x"
                )
        finally:
            fleet_service._store.transaction = original_transaction  # type: ignore[method-assign]
    finally:
        rival.close()
    assert fleet_service._store.list_active_assignments() == []
