"""The routing attempt model: pin before dispatch, settle under terminal rules.

Audit 2026-09-13, finding 7: an attempt's pinned context is persisted before
dispatch, the four outcome states are distinguished, a delivered outcome is
terminal, and a late failed retry can never erase a known success.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.broker.fleet.errors import ClerkRoutingAttemptConflict
from app.broker.fleet.records import RoutingReceiptState
from tests.broker.fleet.conftest import bind_lane, provision_lane


def _attempt(fleet_service, lane, *, key: str, target: str = "strategy/sid-1"):
    """Open one pinned attempt for a lane's bot action."""
    return fleet_service.open_routing_attempt(
        broker=lane.broker,
        clerk_id=lane.clerk_id,
        operation_kind="bot_action",
        nonsecret_target_ref=target,
        idempotency_key=key,
        pinned_routing_epoch=1,
        pinned_binding_generation=1,
        pinned_agent_instance_id="agnt_111111111111111111111111",
    )


def test_the_attempt_lifecycle_covers_every_outcome(
    control_dir: Path, fleet_service
) -> None:
    """not_dispatched → dispatched → each outcome settles exactly as allowed."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="life", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-life")

    opened = _attempt(fleet_service, lane, key="life-1")
    assert opened.state == RoutingReceiptState.NOT_DISPATCHED
    assert opened.pinned_routing_epoch == 1
    assert opened.pinned_binding_generation == 1

    # Refused before dispatch: definitively not sent.
    refused = fleet_service.settle_routing_attempt(
        correlation_id=opened.correlation_id,
        outcome=RoutingReceiptState.PROVIDER_REFUSED,
    )
    assert refused.state == RoutingReceiptState.PROVIDER_REFUSED

    # Delivered carries the provider's durable reference and is terminal.
    delivered = fleet_service.settle_routing_attempt(
        correlation_id=opened.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="provider/command-77",
    )
    assert delivered.state == RoutingReceiptState.DELIVERED
    assert delivered.upstream_receipt_ref == "provider/command-77"
    with pytest.raises(ClerkRoutingAttemptConflict, match="never downgraded"):
        fleet_service.settle_routing_attempt(
            correlation_id=opened.correlation_id,
            outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
        )
    with pytest.raises(ClerkRoutingAttemptConflict, match="never downgraded"):
        fleet_service.settle_routing_attempt(
            correlation_id=opened.correlation_id,
            outcome=RoutingReceiptState.PROVIDER_REFUSED,
        )

    # Outcome-unknown reconciles to delivered by identity, never to a resubmit.
    other = _attempt(fleet_service, lane, key="life-2")
    fleet_service.mark_routing_dispatched(correlation_id=other.correlation_id)
    unknown = fleet_service.settle_routing_attempt(
        correlation_id=other.correlation_id,
        outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
    )
    assert unknown.state == RoutingReceiptState.OUTCOME_UNKNOWN
    reconciled = fleet_service.settle_routing_attempt(
        correlation_id=other.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="provider/command-78",
    )
    assert reconciled.state == RoutingReceiptState.DELIVERED
    # A settlement is never a rewind to the pre-dispatch state.
    with pytest.raises(ClerkRoutingAttemptConflict, match="pre-dispatch"):
        fleet_service.settle_routing_attempt(
            correlation_id=other.correlation_id,
            outcome=RoutingReceiptState.NOT_DISPATCHED,
        )
    with pytest.raises(ClerkRoutingAttemptConflict, match="No routing attempt"):
        fleet_service.settle_routing_attempt(
            correlation_id="corr_0000000000000000000000ff",
            outcome=RoutingReceiptState.DELIVERED,
        )


def test_a_dispatched_attempt_can_never_present_as_un_sent(
    control_dir: Path, fleet_service
) -> None:
    """Once dispatched, an attempt can never present as un-sent."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="dispatch", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-dispatch")
    attempt = _attempt(fleet_service, lane, key="disp-1")

    dispatched = fleet_service.mark_routing_dispatched(
        correlation_id=attempt.correlation_id
    )
    assert dispatched.dispatched_at_ms is not None
    with (
        pytest.raises(sqlite3.IntegrityError, match="never un-dispatched"),
        fleet_service._store.transaction() as conn,
    ):
        conn.execute(
            "UPDATE routing_receipts SET dispatched_at_ms = NULL "
            "WHERE correlation_id = ?",
            (attempt.correlation_id,),
        )
    # Settling an unmarked attempt is still possible: the provider may refuse
    # or deliver between open and the dispatch marking in a crashed caller,
    # and the receipt records the truth the caller last knew.


def test_a_concurrent_open_race_resolves_to_one_attempt(
    control_dir: Path, fleet_service
) -> None:
    """Two callers opening the same lane-scoped key at once: one row, both get it."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="open-race", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-race")

    def open_attempt(_: int):
        return _attempt(fleet_service, lane, key="race-key")

    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(open_attempt, range(4)))
    assert len({attempt.correlation_id for attempt in attempts}) == 1
    assert (
        len(fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)) == 1
    )


def test_a_mismatched_effective_tuple_refuses_as_input_validation(
    control_dir: Path, fleet_service
) -> None:
    """Regression (independent review): a profile/revision nullity mismatch is
    a boundary validation error, never a raw constraint traceback."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="nullity", tmp_path=control_dir.parent)
    session = fleet_service.register_agent_session(fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key)
    fleet_service.reserve_assignment(
        broker="fake_alpha", clerk_id=lane.clerk_id, external_account_id="acct-null"
    )
    with pytest.raises(ValueError, match="profile id and revision"):
        fleet_service.confirm_assignment(
            broker="fake_alpha",
            clerk_id=lane.clerk_id,
            external_account_id="acct-null",
            binding_generation=1,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
            effective_profile_id="prof_1",
            effective_revision=None,
        )


def test_a_stale_pre_delivery_read_cannot_downgrade_a_delivered_outcome(
    control_dir: Path, fleet_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (independent review): a settlement that loses the race to a
    delivered outcome gets the typed conflict, not the raw trigger error."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="race-settle", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-settle")
    attempt = _attempt(fleet_service, lane, key="settle-race")
    fleet_service.mark_routing_dispatched(correlation_id=attempt.correlation_id)
    fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="provider/ok",
    )

    # Serve the racer a stale pre-delivery read: the schema trigger refuses
    # the downgrade inside the write, and the service surfaces it typed.
    stale = attempt
    monkeypatch.setattr(
        fleet_service._store,
        "read_routing_receipt",
        lambda _correlation_id: stale,
    )
    try:
        with pytest.raises(ClerkRoutingAttemptConflict, match="outcome fence"):
            fleet_service.settle_routing_attempt(
                correlation_id=stale.correlation_id,
                outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
            )
    finally:
        monkeypatch.undo()


def test_an_unmarked_attempt_may_still_settle(
    control_dir: Path, fleet_service
) -> None:
    """A caller that crashed between open and the dispatch marking still
    records the truth it last knew when settling."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="unmarked", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-unmarked")
    attempt = _attempt(fleet_service, lane, key="unmarked-1")
    settled = fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id,
        outcome=RoutingReceiptState.PROVIDER_REFUSED,
    )
    assert settled.state == RoutingReceiptState.PROVIDER_REFUSED
    assert settled.dispatched_at_ms is None


def test_the_first_delivered_provider_receipt_is_frozen(
    control_dir: Path, fleet_service
) -> None:
    """Regression (PR review): re-settling a delivered attempt cannot swap its
    upstream receipt reference for a different command identity."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="frozen", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-frozen")
    attempt = _attempt(fleet_service, lane, key="frozen-1")
    fleet_service.mark_routing_dispatched(correlation_id=attempt.correlation_id)
    fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="provider/command-77",
    )
    # The identical reference re-settles idempotently…
    settled = fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id,
        outcome=RoutingReceiptState.DELIVERED,
        upstream_receipt_ref="provider/command-77",
    )
    assert settled.upstream_receipt_ref == "provider/command-77"
    # …a different one refuses.
    with pytest.raises(ClerkRoutingAttemptConflict, match="cannot replace"):
        fleet_service.settle_routing_attempt(
            correlation_id=attempt.correlation_id,
            outcome=RoutingReceiptState.DELIVERED,
            upstream_receipt_ref="provider/command-99",
        )
    stored = fleet_service._store.read_routing_receipt(attempt.correlation_id)
    assert stored is not None
    assert stored.upstream_receipt_ref == "provider/command-77"
