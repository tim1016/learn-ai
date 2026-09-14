"""Agent sessions, epoch fencing, route resolution and routing attempts (PRD §9.7–9.8)."""

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
from app.broker.fleet.service import OperationReadiness
from tests.broker.fleet.conftest import provision_lane


def _live_lane(fleet_service, tmp_path: Path, label: str):
    """Provision, register, reserve and confirm one routable lane."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label=label, tmp_path=tmp_path)
    session = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id=f"acct-{label}"
    )
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id=f"acct-{label}",
        binding_generation=2,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    return lane, session


def test_registration_bumps_the_epoch_and_archives_the_previous_session(
    control_dir: Path, fleet_service
) -> None:
    """A new instance bumps the epoch and archives the old session; the same instance is idempotent."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="epochs", tmp_path=control_dir.parent
    )
    first = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_111111111111111111111111"
    )
    again = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_111111111111111111111111"
    )
    assert again.routing_epoch == first.routing_epoch  # idempotent, not a new epoch

    restarted = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_222222222222222222222222"
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
    """A superseded instance's heartbeat is a no-op against the current session."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="stale", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_aaaaaaaaaaaaaaaaaaaaaaaa"
    )
    current = fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_bbbbbbbbbbbbbbbbbbbbbbbb"
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
    """Routing verifies broker identity, epoch fences and rejects unknown identities."""
    lane, session = _live_lane(fleet_service, control_dir.parent, "routed")

    clerk, resolved, assignment = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id
    )
    assert resolved.routing_epoch == session.routing_epoch
    assert clerk.broker == "fake_alpha"
    assert assignment is not None
    assert assignment.confirmed_binding_generation == 2

    # Wrong provider on the path: the clerk's broker is immutable (FR-071).
    with pytest.raises(ClerkBrokerMismatch):
        fleet_service.resolve_route(broker="fake_beta", clerk_id=lane.clerk_id)

    # A pinned stale epoch refuses instead of silently retargeting (FR-078).
    fleet_service.register_agent_session(
        fleet_protocol_version=2,clerk_id=lane.clerk_id, worker_key=lane.worker_key, agent_instance_id="agnt_cccccccccccccccccccccccc"
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
    """A clerk with no session is not routable."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="silent", tmp_path=control_dir.parent
    )
    with pytest.raises(ClerkUnreachable):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)


def test_an_unconfirmed_assignment_is_not_command_routable(
    control_dir: Path, fleet_service
) -> None:
    """FR-064: a session is command-routable only once its assignment carries
    a confirmed binding observation."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="unconfirmed", tmp_path=control_dir.parent
    )
    session = fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-u"
    )
    # Registered but never confirmed: routing refuses.
    with pytest.raises(ClerkUnreachable, match="effective account assignment"):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)

    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-u",
        binding_generation=1,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    clerk, resolved, assignment = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id
    )
    assert assignment is not None
    assert assignment.confirmed_binding_generation == 1
    assert clerk.clerk_id == lane.clerk_id
    assert resolved.agent_instance_id == session.agent_instance_id


def test_a_stale_expected_binding_generation_refuses_instead_of_retargeting(
    control_dir: Path, fleet_service
) -> None:
    """FR-073: a command carries the binding generation it was prepared
    against; a mismatch is a typed conflict, never a silent retarget."""
    lane, session = _live_lane(fleet_service, control_dir.parent, "generation")
    fleet_service.confirm_assignment(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        external_account_id="acct-generation",
        binding_generation=5,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    from app.broker.fleet.errors import ClerkBindingGenerationConflict

    with pytest.raises(ClerkBindingGenerationConflict, match="binding generation"):
        fleet_service.resolve_route(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            expected_binding_generation=4,
        )
    # The current generation still routes.
    clerk, _resolved, assignment = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id, expected_binding_generation=5
    )
    assert assignment is not None
    assert assignment.confirmed_binding_generation == 5
    assert clerk.broker == "fake_alpha"


def test_an_expected_account_must_match_the_confirmed_account(
    control_dir: Path, fleet_service
) -> None:
    """An execution route naming a different account refuses (FR-073)."""
    lane, _session = _live_lane(fleet_service, control_dir.parent, "account")
    from app.broker.fleet.errors import ClerkAccountMismatch

    with pytest.raises(ClerkAccountMismatch):
        fleet_service.resolve_route(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            expected_account_id="acct-other",
        )
    clerk, _resolved, assignment = fleet_service.resolve_route(
        broker="fake_alpha", clerk_id=lane.clerk_id, expected_account_id=" acct-account "
    )
    assert assignment is not None
    assert assignment.canonical_external_account_id == "ACCT-ACCOUNT"
    assert clerk.clerk_id == lane.clerk_id


def test_configuration_access_stays_routable_without_a_confirmed_binding(
    control_dir: Path, fleet_service
) -> None:
    """Audit 2026-09-13, finding 6: an unbound lane's configuration surface
    stays routable so the operator can reach the repair path."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="unbound", tmp_path=control_dir.parent
    )
    fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    with pytest.raises(ClerkUnreachable):
        fleet_service.resolve_route(broker="fake_alpha", clerk_id=lane.clerk_id)
    clerk, session, assignment = fleet_service.resolve_route(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        readiness=OperationReadiness.CONFIGURATION_ACCESS,
    )
    assert assignment is None
    assert clerk.clerk_id == lane.clerk_id
    assert session is not None


def test_routing_attempt_identity_is_per_lane_and_idempotent(
    control_dir: Path, fleet_service
) -> None:
    """Context is pinned before dispatch, dispatch marking is idempotent, and a
    retried attempt's idempotency key lands on the same row — but only within
    one lane; another lane's identical key opens a different receipt."""
    lane, _session = _live_lane(fleet_service, control_dir.parent, "receipts")

    first = fleet_service.open_routing_attempt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        pinned_routing_epoch=_session.routing_epoch,
        pinned_binding_generation=2,
        pinned_agent_instance_id=_session.agent_instance_id,
    )
    assert first.state == RoutingReceiptState.NOT_DISPATCHED
    assert first.dispatched_at_ms is None

    dispatched = fleet_service.mark_routing_dispatched(correlation_id=first.correlation_id)
    assert dispatched.dispatched_at_ms is not None
    # Marking is idempotent: the first dispatch timestamp survives.
    again = fleet_service.mark_routing_dispatched(correlation_id=first.correlation_id)
    assert again.dispatched_at_ms == dispatched.dispatched_at_ms

    settled = fleet_service.settle_routing_attempt(
        correlation_id=first.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="upstream/receipt-9",
    )
    assert settled.state == RoutingReceiptState.DELIVERED
    assert settled.upstream_receipt_ref == "upstream/receipt-9"

    # The retry keeps the same idempotency identity and lands on the same row.
    retried = fleet_service.open_routing_attempt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        pinned_routing_epoch=_session.routing_epoch,
        pinned_binding_generation=2,
        pinned_agent_instance_id=_session.agent_instance_id,
    )
    assert retried.correlation_id == first.correlation_id
    assert len(fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)) == 1

    # Another lane's same key is a different receipt: idempotency is per lane.
    other, other_session = _live_lane(fleet_service, control_dir.parent, "receipts-b")
    cross = fleet_service.open_routing_attempt(
        broker="fake_alpha",
        clerk_id=other.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-1",
        idempotency_key="idem-1",
        pinned_routing_epoch=other_session.routing_epoch,
        pinned_binding_generation=2,
        pinned_agent_instance_id=other_session.agent_instance_id,
    )
    assert cross.correlation_id != first.correlation_id


def test_routing_attempt_identities_and_pinned_context_are_immutable(
    fleet_service, control_dir: Path
) -> None:
    """A receipt's identity and pinned context are immutable and rows are never deleted."""
    import sqlite3

    lane, session = _live_lane(fleet_service, control_dir.parent, "immutable")
    receipt = fleet_service.open_routing_attempt(
        broker="fake_alpha",
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref="strategy/sid-2",
        idempotency_key="idem-2",
        pinned_routing_epoch=session.routing_epoch,
        pinned_binding_generation=2,
        pinned_agent_instance_id=session.agent_instance_id,
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), fleet_service._store.transaction() as conn:
        conn.execute(
            "UPDATE routing_receipts SET idempotency_key = 'moved' "
            "WHERE correlation_id = ?",
            (receipt.correlation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="pinned context"), fleet_service._store.transaction() as conn:
        conn.execute(
            "UPDATE routing_receipts SET pinned_binding_generation = 99 "
            "WHERE correlation_id = ?",
            (receipt.correlation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"), fleet_service._store.transaction() as conn:
        conn.execute(
            "DELETE FROM routing_receipts WHERE correlation_id = ?",
            (receipt.correlation_id,),
        )
