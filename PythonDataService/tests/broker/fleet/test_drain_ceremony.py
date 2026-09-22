"""The drain ceremony (ADR 0063, #2111): drain, the deadline, and the exits.

Covers the three ceremonies the ADR adds — ``drain_clerk``,
``force_retire_clerk``, and the re-gated ``retire_clerk`` / ``release_assignment``
/ ``reassign_assignment`` — against the frozen clock and the two fake
providers, plus the two-legged calendar deadline in ``drain_deadline.py``
against real NYSE dates (a Thanksgiving-week drain, an observed July
holiday, and a premarket drain where the duration leg wins).

The honest baseline this suite pins: until a provider answers lane quiet
(#2154), every retirement of a served clerk is a ``force-retire`` — the
normal path refuses, naming the outstanding item, and never degrades to an
attestation.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from app.broker.fleet.drain_deadline import (
    DRAIN_DEADLINE_FLOOR_MS,
    drain_deadline_at_ms,
)
from app.broker.fleet.errors import (
    ClerkAssignmentConflict,
    ClerkCommandQuietRequired,
    ClerkDrainDeadlinePending,
    ClerkDrainRequired,
    ClerkLaneQuietUnproven,
    ClerkNotFound,
    ClerkReassignmentBlocked,
)
from app.broker.fleet.records import AssignmentState, RoutingReceiptState, StoredLifecycleState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from tests.broker.fleet.conftest import (
    FrozenClock,
    Lane,
    bind_lane,
    provision_lane,
)

OPERATOR = "inkant"
CHANGE_REF = "incident-2026-09-21-lane-decommission"


def _ms(iso: str) -> int:
    """One fixed instant from an ISO string — hardcoded expectations, not re-derivation."""
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def drain_and_pass_deadline(
    service: FleetControlService, clock: FrozenClock, clerk_id: str
) -> int:
    """Drain one clerk and advance the clock strictly past its stored deadline."""
    drained = service.drain_clerk(clerk_id=clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    return drained.drain_deadline_at_ms


def open_unsettled_attempt(
    service: FleetControlService, lane: Lane, *, key: str = "drain-test-key"
) -> str:
    """One dispatched-but-unsettled receipt: the command-quiet predicate's obstacle."""
    receipt = service.open_routing_attempt(
        broker=lane.broker,
        clerk_id=lane.clerk_id,
        operation_kind="submit_bot_action",
        nonsecret_target_ref="strategy/sid-drain",
        idempotency_key=key,
        pinned_routing_epoch=1,
        pinned_agent_instance_id="agnt_draintest0000000000000",
    )
    service.mark_routing_dispatched(correlation_id=receipt.correlation_id)
    return receipt.correlation_id


# ---- the two-legged deadline floor -------------------------------------------


def test_the_deadline_floor_refuses_a_duration_below_one_day() -> None:
    """A deployment may raise the drain duration, never lower it below 24h."""
    with pytest.raises(ValueError, match=r"below the \d+ ms floor"):
        drain_deadline_at_ms(
            draining_since_ms=_ms("2026-11-25T20:55:00+00:00"),
            drain_deadline_ms=DRAIN_DEADLINE_FLOOR_MS - 1,
        )


def test_the_service_refuses_to_construct_with_a_sub_floor_deadline(
    control_dir: Path,
) -> None:
    """The floor is bound at the constructor too, not only in the helper."""
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        with pytest.raises(ValueError, match="floor"):
            FleetControlService(store=store, drain_deadline_ms=1)
    finally:
        store.close()


def test_a_mid_session_drain_before_a_holiday_reaches_the_next_sessions_close() -> None:
    """Drain Wednesday 2026-11-25 at 15:55 ET, mid-session, ahead of Thanksgiving.

    Thursday is the holiday; Friday 2026-11-27 is the half-day. The calendar
    leg is Friday's actual 13:00 ET close (18:00 UTC) — a session close the
    lane can reach — and it beats the duration leg's Thursday 15:55 ET.
    """
    deadline = drain_deadline_at_ms(
        draining_since_ms=_ms("2026-11-25T20:55:00+00:00"),
        drain_deadline_ms=DRAIN_DEADLINE_FLOOR_MS,
    )
    assert deadline == _ms("2026-11-27T18:00:00+00:00")


def test_a_drain_across_the_observed_july_holiday_reaches_monday() -> None:
    """July 4 2026 falls on a Saturday, so Friday July 3 is the observed holiday.

    A Thursday 10:00 ET drain skips to Monday 2026-07-06's 16:00 ET close —
    the calendar knows the observed holiday a flat duration never could.
    """
    deadline = drain_deadline_at_ms(
        draining_since_ms=_ms("2026-07-02T14:00:00+00:00"),
        drain_deadline_ms=DRAIN_DEADLINE_FLOOR_MS,
    )
    assert deadline == _ms("2026-07-06T20:00:00+00:00")


def test_a_premarket_drain_lets_the_duration_leg_win() -> None:
    """Drain Monday 2026-11-30 at 03:00 ET, before the open.

    The calendar leg is Monday's own 16:00 ET close (its open is at or after
    the drain); the 24h duration leg runs to Tuesday 03:00 ET and wins.
    """
    deadline = drain_deadline_at_ms(
        draining_since_ms=_ms("2026-11-30T08:00:00+00:00"),
        drain_deadline_ms=DRAIN_DEADLINE_FLOOR_MS,
    )
    assert deadline == _ms("2026-12-01T08:00:00+00:00")


# ---- drain entry (Decision 1) -------------------------------------------------


def test_drain_closes_the_door_with_a_durable_start_and_deadline(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Drain enters `draining`, writing both instants in one transaction."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="d1", tmp_path=control_dir.parent)
    clock.advance(123)
    drained = fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    assert drained.lifecycle_state == StoredLifecycleState.DRAINING
    assert drained.draining_since_ms == clock()
    assert drained.drain_deadline_at_ms is not None
    assert drained.drain_deadline_at_ms >= drained.draining_since_ms + DRAIN_DEADLINE_FLOOR_MS
    # The directory projects the drain and carries the window's endpoints.
    descriptor = fleet_service.describe_clerk(lane.clerk_id)
    assert descriptor.public_fields()["lifecycle_state"] == "draining"
    assert descriptor.public_fields()["drain_deadline_at_ms"] == drained.drain_deadline_at_ms


def test_a_repeated_drain_returns_the_existing_record_and_never_extends_the_bound(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """§7.3: a retry loop must not make the deadline decorative."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="d2", tmp_path=control_dir.parent)
    first = fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    clock.advance(3_600_000)
    again = fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    assert again.draining_since_ms == first.draining_since_ms
    assert again.drain_deadline_at_ms == first.drain_deadline_at_ms


def test_a_retired_lane_never_drains(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Retirement is terminal; the drain of a retired lane is simply absent."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="d3", tmp_path=control_dir.parent)
    fleet_service.retire_clerk(clerk_id=lane.clerk_id)
    with pytest.raises(ClerkNotFound, match="retired"):
        fleet_service.drain_clerk(clerk_id=lane.clerk_id)


def test_the_store_level_rewrite_of_a_drains_facts_refuses(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """The write-once trigger is the fence beneath the service seam: even a
    caller that bypassed the idempotent path cannot move a drain's facts."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="d4", tmp_path=control_dir.parent)
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    store = fleet_service._store
    with pytest.raises(sqlite3.IntegrityError, match="written once"), store.transaction() as conn:
        conn.execute(
            "UPDATE clerks SET drain_deadline_at_ms = drain_deadline_at_ms + 1 "
            "WHERE clerk_id = ?",
            (lane.clerk_id,),
        )


# ---- retirement (Decisions 2, 3, 6) -------------------------------------------


def test_a_never_served_clerk_retires_directly(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Decision 6: no history rows, no live session — the direct edge stays open."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r1", tmp_path=control_dir.parent)
    retired = fleet_service.retire_clerk(clerk_id=lane.clerk_id)
    assert retired.lifecycle_state == StoredLifecycleState.RETIRED
    assert retired.retired_at_ms is not None
    assert retired.lane_confirmation is None
    assert retired.retire_operator is None


def test_a_clerk_that_registered_once_is_served_and_cannot_bypass_the_drain(
    fleet_service: FleetControlService, control_dir: Path, clock: FrozenClock
) -> None:
    """The live clerk_sessions row is the load-bearing third clause: a clerk
    that registered exactly once holds zero history rows yet has served."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r2", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    with pytest.raises(ClerkDrainRequired, match="only through draining"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_a_served_drained_clerk_retires_only_through_lane_quiet(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Decision 2: a served lane that has not confirmed lane quiet refuses —
    silence is never read as quiet, and the gate never degrades to an operator
    attestation. The assignment is released first so every earlier gate is
    genuinely green, which makes this refusal the lane-quiet one.

    The confirmed path is covered in ``test_lane_quiet_confirmation.py``; this
    case pins the default, which is refusal."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r3", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-R3")
    drain_and_pass_deadline(fleet_service, clock, lane.clerk_id)
    fleet_service.release_assignment(
        broker=lane.broker,
        external_account_id="ACCT-R3",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    with pytest.raises(ClerkLaneQuietUnproven, match="has not confirmed lane"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_a_held_assignment_refuses_retirement_naming_the_drain_first(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The obligation check precedes every other gate and its remediation
    names the drain as the first step (Decision 6's re-authored refusal)."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r4", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-R4")
    with pytest.raises(ClerkAssignmentConflict, match="after the drain"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_an_unsettled_dispatch_refuses_retirement(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Decision 3: a lane does not leave service past a command whose outcome
    the coordinator lost. The assignment is released first so the earlier
    gates are green and this one is what refuses."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r5", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-R5")
    drain_and_pass_deadline(fleet_service, clock, lane.clerk_id)
    fleet_service.release_assignment(
        broker=lane.broker,
        external_account_id="ACCT-R5",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    open_unsettled_attempt(fleet_service, lane)
    with pytest.raises(ClerkCommandQuietRequired, match="outcome the coordinator lost"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_the_lifecycle_trigger_backstop_blocks_a_direct_write_for_a_served_clerk(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Decision 6's trigger arm: the thing a future caller cannot step over."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="r6", tmp_path=control_dir.parent)
    fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )
    store = fleet_service._store
    with pytest.raises(sqlite3.IntegrityError, match="retires through draining"), store.transaction() as conn:
        conn.execute(
            "UPDATE clerks SET lifecycle_state = 'retired', retired_at_ms = 999 "
            "WHERE clerk_id = ?",
            (lane.clerk_id,),
        )


# ---- force-retire (Decision 5) -------------------------------------------------


def test_force_retire_refuses_before_the_deadline_naming_the_instant(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The bound is what a stalled drain holds a lane to; before it elapses
    the ceremony refuses and names the outstanding instant."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="f1", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-F1")
    drained = fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    clock.advance(1)  # strictly inside the deadline window
    with pytest.raises(ClerkDrainDeadlinePending) as refusal:
        fleet_service.force_retire_clerk(
            clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
    assert str(drained.drain_deadline_at_ms) in refusal.value.message


def test_force_retire_records_obligations_atomically_and_survives_a_retry(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The named exit: past the deadline it settles lost dispatches
    ``outcome_unknown``, releases the assignment under the same attribution
    (a lane that cannot answer is a lane whose release could not run), retires
    with ``lane_confirmation: absent``, and records every forced correlation
    id in the registry's append-only obligations table — one transaction with
    the retirement, so the work queue cannot be orphaned and a re-run of the
    completed ceremony neither duplicates it nor loses it."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="f2", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-F2")
    correlation = open_unsettled_attempt(fleet_service, lane)
    drain_and_pass_deadline(fleet_service, clock, lane.clerk_id)
    retired = fleet_service.force_retire_clerk(
        clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
    )
    assert retired.lifecycle_state == StoredLifecycleState.RETIRED
    assert retired.lane_confirmation == "absent"
    assert retired.retire_operator == OPERATOR
    assert retired.retire_change_ref == CHANGE_REF
    assert retired.draining_since_ms is not None  # the drain facts persist into retirement
    settled = fleet_service._store.read_routing_receipt(correlation)
    assert settled is not None
    assert settled.state == RoutingReceiptState.OUTCOME_UNKNOWN
    # The held assignment left released, attributed to the same ceremony —
    # the account key is never wedged under a retired owner.
    assignment = fleet_service._store.read_assignment(
        broker=lane.broker, canonical_account_id="ACCT-F2"
    )
    assert assignment is not None
    assert assignment.state == AssignmentState.RELEASED
    history = fleet_service._store.list_assignment_history(
        broker=lane.broker, canonical_account_id="ACCT-F2"
    )
    released_rows = [row for row in history if row.state == AssignmentState.RELEASED]
    assert len(released_rows) == 1
    assert released_rows[0].attested_operator == OPERATOR
    assert released_rows[0].lane_confirmation == "absent"
    # The forced-unknown obligations live in the registry, one row per id,
    # with the ceremony's attribution — and a re-run changes nothing.
    obligations = fleet_service._store.list_forced_correlations(lane.clerk_id)
    assert [entry[0] for entry in obligations] == [correlation]
    assert obligations[0][1] == lane.broker
    assert obligations[0][3] == OPERATOR
    assert obligations[0][4] == CHANGE_REF
    again = fleet_service.force_retire_clerk(
        clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
    )
    assert again == retired
    assert fleet_service._store.list_forced_correlations(lane.clerk_id) == obligations
    # A registry table, not a sidecar file: the obligations ride the backup.
    from app.broker.fleet.recovery import BACKUP_DATABASE_FILENAME, create_registry_backup

    backup_dir = control_dir.parent / "force-retire-backup"
    create_registry_backup(fleet_service._store, backup_dir=backup_dir)
    backup = sqlite3.connect(backup_dir / BACKUP_DATABASE_FILENAME)
    try:
        rows = backup.execute(
            "SELECT correlation_id, operator FROM force_retire_correlations"
        ).fetchall()
    finally:
        backup.close()
    assert rows == [(correlation, OPERATOR)]


def test_a_failure_mid_force_retire_rolls_back_everything(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The obligations record is atomic with the retirement: a failure while
    recording it leaves the clerk draining, the dispatch still unsettled, and
    no partial reconciliation queue behind — the retry re-runs the whole
    ceremony rather than silently completing half of one."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="f2b", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-F2B")
    correlation = open_unsettled_attempt(fleet_service, lane)
    drain_and_pass_deadline(fleet_service, clock, lane.clerk_id)
    store = fleet_service._store
    real_record = store.record_forced_correlations

    def refusing_record(*args, **kwargs) -> int:
        raise sqlite3.IntegrityError("injected obligations failure")

    monkeypatch.setattr(store, "record_forced_correlations", refusing_record)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        fleet_service.force_retire_clerk(
            clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )
    clerk = store.read_clerk(lane.clerk_id)
    assert clerk is not None
    assert clerk.lifecycle_state == StoredLifecycleState.DRAINING
    receipt = store.read_routing_receipt(correlation)
    assert receipt is not None
    assert receipt.state == RoutingReceiptState.NOT_DISPATCHED
    assert receipt.dispatched_at_ms is not None
    assignment = store.read_assignment(broker=lane.broker, canonical_account_id="ACCT-F2B")
    assert assignment is not None
    assert assignment.state == AssignmentState.EFFECTIVE
    assert store.list_forced_correlations(lane.clerk_id) == []
    # The retry, with the failure gone, completes the whole ceremony.
    monkeypatch.setattr(store, "record_forced_correlations", real_record)
    retired = fleet_service.force_retire_clerk(
        clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
    )
    assert retired.lifecycle_state == StoredLifecycleState.RETIRED
    assert [entry[0] for entry in store.list_forced_correlations(lane.clerk_id)] == [
        correlation
    ]


def test_force_retire_refuses_a_never_drained_clerk(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Force-retire is the exit for a *stalled drain*; a never-served clerk
    retires directly and a provisioned one drains first."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="f3", tmp_path=control_dir.parent)
    with pytest.raises(ClerkDrainRequired, match="stalled drain"):
        fleet_service.force_retire_clerk(
            clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
        )


def test_a_malformed_attribution_refuses_the_invocation(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The attribution is bounded input, not ceremony state: malformed shape
    is a bad invocation (exit 1), never a typed state refusal."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="f5", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-F5")
    drain_and_pass_deadline(fleet_service, clock, lane.clerk_id)
    with pytest.raises(ValueError, match="attributing who acted"):
        fleet_service.force_retire_clerk(
            clerk_id=lane.clerk_id, operator="   ", change_ref=CHANGE_REF
        )
    with pytest.raises(ValueError, match="attributing who acted"):
        fleet_service.force_retire_clerk(
            clerk_id=lane.clerk_id, operator=OPERATOR, change_ref="x" * 513
        )


# ---- release (Decision 4/4.1) --------------------------------------------------


def test_release_requires_a_draining_predecessor(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Release on a provisioned lane is the closed-door violation."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="x1", tmp_path=control_dir.parent)
    assignment = fleet_service.reserve_assignment(
        broker=lane.broker, clerk_id=lane.clerk_id, external_account_id="ACCT-X1"
    )
    with pytest.raises(ClerkDrainRequired, match="door is closed"):
        fleet_service.release_assignment(
            broker=lane.broker,
            external_account_id="ACCT-X1",
            expected_assignment_generation=assignment.assignment_generation,
            operator=OPERATOR,
            change_ref=CHANGE_REF,
        )


def test_release_waits_out_the_drain_deadline(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """§4.1: release is available but never same-breath with the drain."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="x2", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-X2")
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    with pytest.raises(ClerkDrainDeadlinePending):
        fleet_service.release_assignment(
            broker=lane.broker,
            external_account_id="ACCT-X2",
            expected_assignment_generation=1,
            operator=OPERATOR,
            change_ref=CHANGE_REF,
        )


def test_release_refuses_an_unsettled_dispatch(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Command quiet gates release exactly as it gates retirement."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="x3", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-X3")
    open_unsettled_attempt(fleet_service, lane)
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    clock.advance(DRAIN_DEADLINE_FLOOR_MS * 2)
    with pytest.raises(ClerkCommandQuietRequired):
        fleet_service.release_assignment(
            broker=lane.broker,
            external_account_id="ACCT-X3",
            expected_assignment_generation=1,
            operator=OPERATOR,
            change_ref=CHANGE_REF,
        )


def test_release_after_the_deadline_records_the_attribution_and_absent_confirmation(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The surviving attestation: bounded operator/change_ref on the history
    row, ``lane_confirmation: absent`` counted for every auditor."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="x4", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-X4")
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    clock.advance(DRAIN_DEADLINE_FLOOR_MS * 2)
    released = fleet_service.release_assignment(
        broker=lane.broker,
        external_account_id="ACCT-X4",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    assert released.state == AssignmentState.RELEASED
    history = fleet_service._store.list_assignment_history(
        broker=lane.broker, canonical_account_id="ACCT-X4"
    )
    released_rows = [row for row in history if row.state == AssignmentState.RELEASED]
    assert len(released_rows) == 1
    assert released_rows[0].attested_operator == OPERATOR
    assert released_rows[0].attested_change_ref == CHANGE_REF
    assert released_rows[0].attested_at_ms is not None
    assert released_rows[0].lane_confirmation == "absent"
    # Idempotent with the same audit answer: the retry returns the persisted
    # history row, so the attribution and confirmation the CLI prints are the
    # first run's facts, never nulls off the current-row pointer.
    again = fleet_service.release_assignment(
        broker=lane.broker,
        external_account_id="ACCT-X4",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    assert again == released
    assert again.attested_operator == OPERATOR
    assert again.lane_confirmation == "absent"


# ---- reassignment (§4.1/§7.1) ----------------------------------------------------


def test_reassignment_refuses_a_provisioned_predecessor(
    fleet_service: FleetControlService, control_dir: Path, clock: FrozenClock
) -> None:
    """§4.1's precondition: the drained predecessor is what makes the rest of
    the evidence meaningful."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="z1", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="z2", tmp_path=control_dir.parent)
    bind_lane(fleet_service, first, account="ACCT-Z")
    with pytest.raises(ClerkDrainRequired, match="door is closed"):
        fleet_service.reassign_assignment(
            broker=first.broker,
            external_account_id="ACCT-Z",
            expected_assignment_generation=1,
            operator=OPERATOR,
            change_ref=CHANGE_REF,
            successor_clerk_id=second.clerk_id,
            successor_volume_root=second.volume_root,
        )


def test_reassignment_is_blocked_against_a_drained_lane_until_2154_closes(
    fleet_service: FleetControlService, control_dir: Path, clock: FrozenClock
) -> None:
    """§7.1: even a drained, command-quiet, past-deadline lane is not
    reassigned — no lane-quiet confirmation exists yet (#2154), and without
    it the coordinator cannot tell a genuinely quiet lane from one that
    never learned its drain (#2155's residual window). The fixture creates
    no outage and no missed drain-learning; the refusal stands on the
    missing confirmation alone."""
    first = provision_lane(fleet_service, broker="fake_alpha", label="z3", tmp_path=control_dir.parent)
    second = provision_lane(fleet_service, broker="fake_alpha", label="z4", tmp_path=control_dir.parent)
    bind_lane(fleet_service, first, account="ACCT-Z2")
    drain_and_pass_deadline(fleet_service, clock, first.clerk_id)
    with pytest.raises(ClerkReassignmentBlocked, match="#2154"):
        fleet_service.reassign_assignment(
            broker=first.broker,
            external_account_id="ACCT-Z2",
            expected_assignment_generation=1,
            operator=OPERATOR,
            change_ref=CHANGE_REF,
            successor_clerk_id=second.clerk_id,
            successor_volume_root=second.volume_root,
        )
    # The blocked ceremony left the ownership intact.
    assignment = fleet_service._store.read_assignment(
        broker=first.broker, canonical_account_id="ACCT-Z2"
    )
    assert assignment is not None
    assert assignment.clerk_id == first.clerk_id
    assert assignment.state == AssignmentState.EFFECTIVE


# ---- the full exit path -----------------------------------------------------------


def test_drain_wait_release_force_retire_is_a_complete_exit(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """ADR 0063's reachable exit, end to end, with the token nowhere in it."""
    lane = provision_lane(fleet_service, broker="fake_alpha", label="e1", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="ACCT-E1")
    fleet_service.drain_clerk(clerk_id=lane.clerk_id)
    clock.advance(DRAIN_DEADLINE_FLOOR_MS * 2)
    fleet_service.release_assignment(
        broker=lane.broker,
        external_account_id="ACCT-E1",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    retired = fleet_service.force_retire_clerk(
        clerk_id=lane.clerk_id, operator=OPERATOR, change_ref=CHANGE_REF
    )
    assert retired.lifecycle_state == StoredLifecycleState.RETIRED
    assert retired.lane_confirmation == "absent"


# ---- the release proof token is gone ------------------------------------------------


def test_no_release_proof_token_remains_in_the_fleet_package() -> None:
    """The gate-shaped hole is deleted, not deprecated."""
    import app.broker.fleet.service as service_module

    assert not hasattr(service_module, "RELEASE_PROOF_TOKEN")
