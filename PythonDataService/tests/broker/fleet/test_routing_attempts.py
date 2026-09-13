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


def test_dispatch_is_one_way_and_recorded_before_settlement(
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
