"""Agent sessions, epoch fencing, route resolution and routing receipts (PRD §9.7–9.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    ClerkBrokerMismatch,
    ClerkIdentityMismatch,
    ClerkNotFound,
    ClerkUnreachable,
)
from app.broker.fleet.records import RoutingReceiptState
from tests.broker.fleet.conftest import provision_lane


def _live_lane(fleet_service, tmp_path: Path, label: str):
    lane = provision_lane(fleet_service, broker="fake_alpha", label=label, tmp_path=tmp_path)
    session = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id=f"acct-{label}"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id=f"acct-{label}",
        binding_generation=2,
    )
    return lane, session


def test_registration_bumps_the_epoch_and_archives_the_previous_session(
    control_dir: Path, fleet_service
) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="epochs", tmp_path=control_dir.parent
    )
    first = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_111111111111111111111111"
    )
    again = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_111111111111111111111111"
    )
    assert again.routing_epoch == first.routing_epoch  # idempotent, not a new epoch

    restarted = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_222222222222222222222222"
    )
    assert restarted.routing_epoch == first.routing_epoch + 1

    history = fleet_service._store._query(
        "SELECT routing_epoch, agent_instance_id FROM clerk_session_history "
        "WHERE clerk_id = ? ORDER BY routing_epoch",
        (lane.clerk_id,),
    )
    assert [(row["routing_epoch"], row["agent_instance_id"]) for row in history] == [
        (1, "agnt_111111111111111111111111")
    ]


def test_a_stale_instance_cannot_heartbeat_over_the_current_session(
    control_dir: Path, fleet_service
) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="stale", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_aaaaaaaaaaaaaaaaaaaaaaaa"
    )
    current = fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_bbbbbbbbbbbbbbbbbbbbbbbb"
    )
    # The superseded instance's heartbeat is a no-op, not an overwrite.
    assert (
        fleet_service.observe_session(
            clerk_id=lane.clerk_id,
            agent_instance_id="agnt_aaaaaaaaaaaaaaaaaaaaaaaa",
            reported_state="degraded",
        )
        is False
    )
    session = fleet_service._store.read_session(lane.clerk_id)
    assert session is not None
    assert session.agent_instance_id == current.agent_instance_id
    assert session.reported_state is None


def test_route_resolution_verifies_broker_identity_and_epoch_fencing(
    control_dir: Path, fleet_service
) -> None:
    lane, session = _live_lane(fleet_service, control_dir.parent, "routed")

    clerk, resolved = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id
    )
    assert resolved.routing_epoch == session.routing_epoch
    assert clerk.broker == "fake_alpha"

    # Wrong provider on the path: the clerk's broker is immutable (FR-071).
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=lane.clerk_id)

    # A pinned stale epoch refuses instead of silently retargeting (FR-078).
    fleet_service.register_agent_session(
        clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_cccccccccccccccccccccccc"
    )
    with pytest.raises(ClerkIdentityMismatch):
        fleet_service.resolve_route(
            broker="fake_alpha", clerk_id=lane.clerk_id, expected_routing_epoch=1
        )

    # Unknown and malformed identities are both simply absent.
    with pytest.raises(ClerkNotFound):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id="not-a-clerk")
    with pytest.raises(ClerkNotFound):
        fleet_service.resolve_route(
            broker="fake_alpha", clerk_id="clrk_0000000000000000000000ff"
        )


def test_a_clerk_without_a_session_is_not_routable(control_dir: Path, fleet_service) -> None:
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="silent", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkUnreachable):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)


def test_an_unacknowledged_session_is_not_command_routable(
    control_dir: Path, fleet_service
) -> None:
    """FR-064: the coordinator makes a session command-routable only after the
    worker has acknowledged the local binding and confirmed the assignment."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="unconfirmed", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-u"
    )
    # Registered but never confirmed: routing refuses.
    with pytest.raises(ClerkUnreachable, match="effective binding"):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)

    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-u",
        binding_generation=1,
    )
    clerk, session = fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    assert session.reported_binding_generation == 1
    assert clerk.clerk_id == lane.clerk_id


def test_a_stale_expected_binding_generation_refuses_instead_of_retargeting(
    control_dir: Path, fleet_service
) -> None:
    """FR-073: a command carries the binding generation it was prepared
    against; a mismatch is a typed conflict, never a silent retarget."""
    lane, _session = _live_lane(fleet_service, control_dir.parent, "generation")
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-generation",
        binding_generation=5,
    )
    from app.broker.fleet.errors import ClerkBindingGenerationConflict

    with pytest.raises(ClerkBindingGenerationConflict, match="binding generation"):
        fleet_service.resolve_route(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            expected_binding_generation=4,
        )
    # The current generation still routes.
    clerk, session = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id, expected_binding_generation=5
    )
    assert session.reported_binding_generation == 5
    assert clerk.broker == "fake_alpha"


def test_routing_receipts_correlate_and_never_replace_upstream_evidence(
    control_dir: Path, fleet_service
) -> None:
    lane, _session = _live_lane(fleet_service, control_dir.parent, "receipts")

    first = fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        state=RoutingReceiptState.OUTCOME_UNKNOWN,
    )
    assert first.state == RoutingReceiptState.OUTCOME_UNKNOWN
    assert first.upstream_receipt_ref is None

    # The retry keeps the same idempotency identity and lands on the same row.
    retried = fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        state=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="upstream/receipt-9",
    )
    assert retried.correlation_id == first.correlation_id
    assert retried.state == RoutingReceiptState.DELIVERED
    assert retried.upstream_receipt_ref == "upstream/receipt-9"
    assert len(fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)) == 1

    # Another lane's same key is a different receipt: idempotency is per lane.
    other, _ = _live_lane(fleet_service, control_dir.parent, "receipts-b")
    cross = fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=other.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        state=RoutingReceiptState.FAILED,
    )
    assert cross.correlation_id != first.correlation_id


def test_routing_receipt_identities_are_immutable(fleet_service, control_dir: Path) -> None:
    import sqlite3

    lane, _ = _live_lane(fleet_service, control_dir.parent, "immutable")
    receipt = fleet_service.record_routing_receipt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-2",
        idempotency_key="idem-2",
        state=RoutingReceiptState.DELIVERED,
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), fleet_service._store.transaction() as conn:
        conn.execute(
            "UPDATE routing_receipts SET idempotency_key = 'moved' "
            "WHERE correlation_id = ?",
            (receipt.correlation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), fleet_service._store.transaction() as conn:
        conn.execute(
            "DELETE FROM routing_receipts WHERE correlation_id = ?",
            (receipt.correlation_id,),
        )
