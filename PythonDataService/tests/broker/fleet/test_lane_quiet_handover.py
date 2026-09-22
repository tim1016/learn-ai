"""Lane quiet opens the handover paths, not only retirement (#2154).

ADR 0063 §4.1's amendment: the two-writer hole closes if and only if the
lane-quiet confirmation is wired into release, re-reservation of a released
account, and reassignment — wiring it into retirement alone closes the gate
the ADR opened and leaves the one it named open. Each path here moves an
account only on the predecessor's own fresh, quiet answer, and a lane that
cannot answer never hands its account to another lane.

The confirmation carries no account identity; schema v7's one-live-
assignment-per-clerk rule is what makes the account it covers unambiguous
(``test_directory_and_aggregation`` pins the rule itself).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    ClerkAssignmentConflict,
    ClerkLaneQuietUnproven,
    ClerkReassignmentBlocked,
)
from app.broker.fleet.records import AssignmentState, LaneConfirmationState
from app.broker.fleet.service import DEFAULT_LANE_QUIET_VALID_FOR_MS, FleetControlService
from tests.broker.fleet.conftest import (
    TEST_CHANGE_REF,
    TEST_OPERATOR,
    FrozenClock,
    Lane,
    bind_lane,
    provision_lane,
)

ACCOUNT = "ACCT-HANDOVER"


def _drained(
    service: FleetControlService, clock: FrozenClock, control_dir: Path, label: str
) -> tuple[Lane, object]:
    """A served lane holding ``ACCOUNT``, drained and past its deadline."""
    lane = provision_lane(service, broker="fake_alpha", label=label, tmp_path=control_dir.parent)
    session, _ = bind_lane(service, lane, account=ACCOUNT)
    drained = service.drain_clerk(clerk_id=lane.clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    return lane, session


def _confirm(
    service: FleetControlService, lane: Lane, session, *, observed_at_ms: int, **conditions: bool
) -> None:
    answer = {
        "runner_idle": True,
        "broker_work_ended": True,
        "account_flat": True,
        "intents_resolved": True,
        **conditions,
    }
    service.confirm_lane_quiet(
        clerk_id=lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        observed_at_ms=observed_at_ms,
        **answer,
    )


def _release(service: FleetControlService):
    return service.release_assignment(
        broker="fake_alpha",
        external_account_id=ACCOUNT,
        expected_assignment_generation=1,
        operator=TEST_OPERATOR,
        change_ref=TEST_CHANGE_REF,
    )


def _successor(service: FleetControlService, control_dir: Path, label: str) -> Lane:
    return provision_lane(service, broker="fake_alpha", label=label, tmp_path=control_dir.parent)


# ---- release ------------------------------------------------------------------


def test_a_release_covered_by_a_fresh_quiet_confirmation_records_present(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    lane, session = _drained(fleet_service, clock, control_dir, "r1")
    _confirm(fleet_service, lane, session, observed_at_ms=clock())

    released = _release(fleet_service)

    assert released.state == AssignmentState.RELEASED
    assert released.lane_confirmation == LaneConfirmationState.PRESENT


@pytest.mark.parametrize("evidence", ["none", "not_quiet", "stale"])
def test_a_release_without_fresh_quiet_evidence_still_releases_but_records_absent(
    evidence: str,
    fleet_service: FleetControlService,
    clock: FrozenClock,
    control_dir: Path,
) -> None:
    """Release is the fleet's only exit, so it never refuses on lane quiet —
    it records what it had, and ``absent`` is what re-reservation later reads."""
    lane, session = _drained(fleet_service, clock, control_dir, f"r-{evidence}")
    if evidence == "not_quiet":
        _confirm(fleet_service, lane, session, observed_at_ms=clock(), account_flat=False)
    elif evidence == "stale":
        _confirm(fleet_service, lane, session, observed_at_ms=clock())
        clock.advance(DEFAULT_LANE_QUIET_VALID_FOR_MS + 1)

    released = _release(fleet_service)

    assert released.state == AssignmentState.RELEASED
    assert released.lane_confirmation == LaneConfirmationState.ABSENT


# ---- re-reserving a released account ------------------------------------------


def test_a_quiet_release_can_be_re_reserved_by_a_new_lane(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The re-onboarding path #2157 closed reopens on the release's own evidence."""
    lane, session = _drained(fleet_service, clock, control_dir, "q1")
    _confirm(fleet_service, lane, session, observed_at_ms=clock())
    _release(fleet_service)
    successor = _successor(fleet_service, control_dir, "q1-next")
    # Long after the confirmation went stale: the evidence that counts is
    # what the release recorded, not the retired lane's current answer.
    clock.advance(DEFAULT_LANE_QUIET_VALID_FOR_MS * 10)

    reserved = fleet_service.reserve_assignment(
        broker="fake_alpha",
        clerk_id=successor.clerk_id,
        external_account_id=ACCOUNT,
        volume_root=successor.volume_root,
    )

    assert reserved.clerk_id == successor.clerk_id
    assert reserved.state == AssignmentState.RESERVED
    assert reserved.assignment_generation == 2
    current = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id=ACCOUNT
    )
    assert current == reserved


def test_an_absent_release_is_never_re_reserved(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """A lane that could not answer never proved it stopped writing."""
    lane, _session = _drained(fleet_service, clock, control_dir, "a1")
    _release(fleet_service)
    successor = _successor(fleet_service, control_dir, "a1-next")

    with pytest.raises(ClerkReassignmentBlocked, match="without a lane-quiet confirmation"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=successor.clerk_id,
            external_account_id=ACCOUNT,
            volume_root=successor.volume_root,
        )
    current = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id=ACCOUNT
    )
    assert current is not None and current.clerk_id == lane.clerk_id
    assert current.state == AssignmentState.RELEASED


def test_a_successor_already_holding_an_account_cannot_take_a_second(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    lane, session = _drained(fleet_service, clock, control_dir, "s1")
    _confirm(fleet_service, lane, session, observed_at_ms=clock())
    _release(fleet_service)
    successor = _successor(fleet_service, control_dir, "s1-next")
    bind_lane(fleet_service, successor, account="ACCT-ALREADY-HELD")

    with pytest.raises(ClerkAssignmentConflict, match="already holds a live assignment"):
        fleet_service.reserve_assignment(
            broker="fake_alpha",
            clerk_id=successor.clerk_id,
            external_account_id=ACCOUNT,
            volume_root=successor.volume_root,
        )


# ---- reassignment ---------------------------------------------------------------


def _reassign(service: FleetControlService, successor: Lane):
    return service.reassign_assignment(
        broker="fake_alpha",
        external_account_id=ACCOUNT,
        expected_assignment_generation=1,
        operator=TEST_OPERATOR,
        change_ref=TEST_CHANGE_REF,
        successor_clerk_id=successor.clerk_id,
        successor_volume_root=successor.volume_root,
    )


def test_a_quiet_drained_lane_is_reassigned_in_one_transaction(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    lane, session = _drained(fleet_service, clock, control_dir, "m1")
    _confirm(fleet_service, lane, session, observed_at_ms=clock())
    successor = _successor(fleet_service, control_dir, "m1-next")

    moved = _reassign(fleet_service, successor)

    assert moved.clerk_id == successor.clerk_id
    assert moved.state == AssignmentState.RESERVED
    assert moved.assignment_generation == 2
    assert moved == fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id=ACCOUNT
    )
    history = fleet_service._store.list_assignment_history(
        broker="fake_alpha", canonical_account_id=ACCOUNT
    )
    released = next(
        row for row in history
        if row.state == AssignmentState.RELEASED and row.assignment_generation == 1
    )
    assert released.clerk_id == lane.clerk_id
    assert released.lane_confirmation == LaneConfirmationState.PRESENT
    assert released.attested_operator == TEST_OPERATOR
    assert released.attested_change_ref == TEST_CHANGE_REF
    assert history[-1].clerk_id == successor.clerk_id


@pytest.mark.parametrize(
    ("evidence", "match"),
    [
        ("none", "has not confirmed"),
        ("not_quiet", "a working order on the account has not ended"),
        ("stale", "stale"),
    ],
)
def test_reassignment_refuses_without_fresh_quiet_evidence_and_moves_nothing(
    evidence: str,
    match: str,
    fleet_service: FleetControlService,
    clock: FrozenClock,
    control_dir: Path,
) -> None:
    lane, session = _drained(fleet_service, clock, control_dir, f"m-{evidence}")
    if evidence == "not_quiet":
        _confirm(fleet_service, lane, session, observed_at_ms=clock(), broker_work_ended=False)
    elif evidence == "stale":
        _confirm(fleet_service, lane, session, observed_at_ms=clock())
        clock.advance(DEFAULT_LANE_QUIET_VALID_FOR_MS + 1)
    successor = _successor(fleet_service, control_dir, f"m-{evidence}-next")

    with pytest.raises(ClerkLaneQuietUnproven, match=match):
        _reassign(fleet_service, successor)

    current = fleet_service._store.read_assignment(
        broker="fake_alpha", canonical_account_id=ACCOUNT
    )
    assert current is not None
    assert current.clerk_id == lane.clerk_id
    assert current.state == AssignmentState.EFFECTIVE
