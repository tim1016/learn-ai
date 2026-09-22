"""The lane-quiet confirmation that opens ADR 0063's normal retirement path.

ADR 0063 Decision 2, as amended 2026-09-19 (#2154): lane quiet is five
conditions. Condition 1 (the lane is draining) is the registry's own fact and
is never taken from the lane; the lane answers conditions 2-5 about itself,
and the coordinator records that answer as a *confirmation* — fenced by the
current session's instance and epoch exactly as ``confirm_assignment`` is,
and refused for an observation taken before the door closed.

The suite's spine is the pair at the top: before this ships the gate refuses
every served clerk, and the whole point of #2154 is that a lane which has
genuinely gone quiet now gets through it. Everything below proves the gate
did not become a formality on the way.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.fleet.errors import (
    ClerkDrainRequired,
    ClerkIdentityMismatch,
    ClerkLaneDraining,
    ClerkLaneQuietUnproven,
)
from app.broker.fleet.records import LaneConfirmationState, StoredLifecycleState
from app.broker.fleet.service import DEFAULT_LANE_QUIET_VALID_FOR_MS, FleetControlService
from tests.broker.fleet.conftest import (
    FrozenClock,
    Lane,
    bind_lane,
    provision_lane,
)

OPERATOR = "host-operator"
CHANGE_REF = "incident-2026-09-21-lane-quiet"


def drained_lane(
    service: FleetControlService, clock: FrozenClock, control_dir: Path, label: str
) -> tuple[Lane, object, object]:
    """A served lane, drained, past its deadline, with its assignment released.

    Every gate ahead of lane quiet is green, so a refusal in these tests can
    only be the lane-quiet gate — which is what makes them about #2154.
    """
    lane = provision_lane(
        service, broker="fake_alpha", label=label, tmp_path=control_dir.parent
    )
    session, _ = bind_lane(service, lane, account=f"ACCT-{label.upper()}")
    drained = service.drain_clerk(clerk_id=lane.clerk_id)
    assert drained.drain_deadline_at_ms is not None
    clock.advance(drained.drain_deadline_at_ms - clock() + 1)
    service.release_assignment(
        broker=lane.broker,
        external_account_id=f"ACCT-{label.upper()}",
        expected_assignment_generation=1,
        operator=OPERATOR,
        change_ref=CHANGE_REF,
    )
    return lane, session, drained


def confirm_quiet(
    service: FleetControlService,
    lane: Lane,
    session,
    *,
    observed_at_ms: int,
    runner_idle: bool = True,
    broker_work_ended: bool = True,
    account_flat: bool = True,
    intents_resolved: bool = True,
):
    """The lane's own answer about conditions 2-5, presented as an agent does."""
    return service.confirm_lane_quiet(
        clerk_id=lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
        observed_at_ms=observed_at_ms,
        runner_idle=runner_idle,
        broker_work_ended=broker_work_ended,
        account_flat=account_flat,
        intents_resolved=intents_resolved,
    )


# ---- the path #2154 exists to open -------------------------------------------


def test_a_confirmed_quiet_lane_retires_on_the_normal_path(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The headline: a lane that proves all five conditions retires without
    ``force-retire``, and the retirement records the confirmation as present."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q1")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())

    retired = fleet_service.retire_clerk(clerk_id=lane.clerk_id)

    assert retired.lifecycle_state == StoredLifecycleState.RETIRED
    assert retired.lane_confirmation == LaneConfirmationState.PRESENT
    # force-retire's attribution belongs to force-retire; the normal path
    # takes no operator attestation, because the lane proved it instead.
    assert retired.retire_operator is None
    assert retired.retire_change_ref is None


def test_a_lane_that_never_confirmed_still_refuses_and_names_the_gate(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Silence is never read as quiet: absence refuses, it does not pass."""
    lane, _session, _drained = drained_lane(fleet_service, clock, control_dir, "q2")

    with pytest.raises(ClerkLaneQuietUnproven, match="has not confirmed"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


# ---- an answer that is not quiet is recorded, and names what is outstanding ---


@pytest.mark.parametrize(
    ("condition", "outstanding"),
    [
        ("runner_idle", "a bot is still running"),
        ("broker_work_ended", "a working order on the account has not ended"),
        ("account_flat", "the account is not flat"),
        ("intents_resolved", "an order intent is unresolved"),
    ],
)
def test_each_unsatisfied_condition_refuses_retirement_by_name(
    fleet_service: FleetControlService,
    clock: FrozenClock,
    control_dir: Path,
    condition: str,
    outstanding: str,
) -> None:
    """Decision 2: the refusal names which condition is outstanding, never its
    contents. One case per condition, so a gate that silently stopped reading
    one of them cannot stay green."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, f"q3{condition[:4]}")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock(), **{condition: False})

    with pytest.raises(ClerkLaneQuietUnproven, match=outstanding):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_a_refusal_names_every_outstanding_condition_not_only_the_first(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """An operator fixing one item at a time needs the whole list, not a
    first-failure that sends them round the ceremony four times."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q4")
    confirm_quiet(
        fleet_service,
        lane,
        session,
        observed_at_ms=clock(),
        runner_idle=False,
        account_flat=False,
    )

    with pytest.raises(ClerkLaneQuietUnproven) as refusal:
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)

    assert "a bot is still running" in str(refusal.value)
    assert "the account is not flat" in str(refusal.value)


def test_a_later_confirmation_supersedes_an_earlier_one(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The operator acts, then the lane re-answers: the gate reads the newest
    answer for the session, not the first one it ever gave."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q5")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock(), account_flat=False)
    clock.advance(1_000)
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())

    retired = fleet_service.retire_clerk(clerk_id=lane.clerk_id)

    assert retired.lane_confirmation == LaneConfirmationState.PRESENT


def test_a_quiet_answer_is_not_undone_by_a_later_unquiet_one_being_ignored(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The newest answer wins in both directions. A lane that goes un-quiet
    again after confirming must close the gate it opened, or the confirmation
    becomes a latch."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q6")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    clock.advance(1_000)
    confirm_quiet(
        fleet_service, lane, session, observed_at_ms=clock(), broker_work_ended=False
    )

    with pytest.raises(ClerkLaneQuietUnproven, match="has not ended"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


# ---- the fences ---------------------------------------------------------------


def test_a_superseded_session_cannot_confirm(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """``confirm_assignment``'s fence, inherited exactly: the epoch a
    confirmation was prepared under must be the session's current one."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q7")

    with pytest.raises(ClerkIdentityMismatch, match="superseded session"):
        fleet_service.confirm_lane_quiet(
            clerk_id=lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch + 1,
            observed_at_ms=clock(),
            runner_idle=True,
            broker_work_ended=True,
            account_flat=True,
            intents_resolved=True,
        )


def test_a_lane_that_restarts_mid_drain_can_never_confirm_and_exits_forced(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The honest cost of #2155 meeting #2154, pinned so it cannot be
    discovered in production.

    A confirmation is fenced to the session that made it, and #2155 refuses a
    draining clerk's re-registration outright — a drained lane never returns
    to service. So a lane whose process restarts during its drain cannot
    obtain a session to confirm under, and `force-retire` is its only exit.
    That is the intended trade: letting it re-register to confirm would
    reopen exactly the resurrection hole #2155 closed.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q8")

    with pytest.raises(ClerkLaneDraining, match="never returns to service"):
        fleet_service.register_agent_session(
            fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key
        )

    # The pre-drain session is still the current one, so a lane that kept
    # running confirms normally — the restart is the only thing that is fatal.
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    assert (
        fleet_service.retire_clerk(clerk_id=lane.clerk_id).lane_confirmation
        == LaneConfirmationState.PRESENT
    )


def test_an_observation_taken_before_the_drain_proves_nothing(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Decision 2 states this fence explicitly: quiescence observed before the
    door closed says nothing about the period after it."""
    lane, session, drained = drained_lane(fleet_service, clock, control_dir, "q9")
    assert drained.draining_since_ms is not None

    with pytest.raises(ClerkLaneQuietUnproven, match="before the drain"):
        confirm_quiet(
            fleet_service, lane, session, observed_at_ms=drained.draining_since_ms - 1
        )


def test_an_observation_from_the_future_refuses(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """A lane whose clock runs ahead would otherwise hold a confirmation that
    never goes stale — freshness is only a bound if the instant is real."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q10")

    with pytest.raises(ClerkLaneQuietUnproven, match="ahead of the coordinator"):
        confirm_quiet(fleet_service, lane, session, observed_at_ms=clock() + 1)


def test_a_provisioned_lane_cannot_confirm_lane_quiet(
    fleet_service: FleetControlService, control_dir: Path
) -> None:
    """Condition 1 is the registry's fact, never the lane's claim: a lane that
    is still serving cannot answer a question about a drain it is not in."""
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="q11", tmp_path=control_dir.parent
    )
    session = fleet_service.register_agent_session(
        fleet_protocol_version=2, clerk_id=lane.clerk_id, worker_key=lane.worker_key
    )

    with pytest.raises(ClerkDrainRequired, match="confirms lane quiet only while draining"):
        confirm_quiet(fleet_service, lane, session, observed_at_ms=1)


# ---- freshness ----------------------------------------------------------------


def test_a_confirmation_the_lane_stopped_re_asserting_goes_stale(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The lane re-asserts on its own cadence; a confirmation older than the
    validity window refuses, naming its age."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q12")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    clock.advance(fleet_service.lane_quiet_valid_for_ms + 1)

    with pytest.raises(ClerkLaneQuietUnproven, match="stale"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_staleness_is_the_confirmations_own_age_not_the_sessions(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """Decision 5 forbids heartbeat silence from *moving* anything; this gate
    refuses instead, and on the evidence's age rather than on liveness. A lane
    beating steadily but no longer confirming must refuse exactly as a dead
    one does — otherwise liveness has become the gate."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q13")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    clock.advance(fleet_service.lane_quiet_valid_for_ms + 1)
    # The lane is demonstrably alive: its heartbeat lands on this very tick.
    fleet_service.observe_session(
        clerk_id=lane.clerk_id, agent_instance_id=session.agent_instance_id
    )

    with pytest.raises(ClerkLaneQuietUnproven, match="stale"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_a_confirmation_inside_the_window_still_passes(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The boundary is inclusive on the passing side: the window is a bound,
    not a race the operator has to win."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q14")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    clock.advance(fleet_service.lane_quiet_valid_for_ms)

    retired = fleet_service.retire_clerk(clerk_id=lane.clerk_id)

    assert retired.lane_confirmation == LaneConfirmationState.PRESENT


# ---- the session fence, at the layer where it can still fire ------------------


def test_the_gates_read_never_returns_a_superseded_sessions_confirmation(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The session scoping is defence against a rule that could change, so it
    is tested where it is reachable rather than left decorative.

    Through the service it cannot fire today: a draining clerk's session
    cannot be replaced (#2155 refuses the re-registration), so the newest
    confirmation is always the current session's. If that rule ever relaxes —
    letting a restarted lane confirm — an unscoped read would silently accept
    a dead session's answer. This pins the fence at the store, where the two
    sessions can be written directly, so the guard fails loudly if it is ever
    removed instead of passing because nothing reaches it.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q17")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    store = fleet_service._store

    successor = replace(
        store.read_lane_quiet_confirmation_on(
            store._conn,
            lane.clerk_id,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
        ),
        agent_instance_id="agnt_successor00000000000000",
        routing_epoch=session.routing_epoch + 1,
        observed_at_ms=clock() + 1,
        account_flat=False,
    )
    with store.transaction() as conn:
        store.record_lane_quiet_confirmation(conn, successor)

    scoped = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    assert scoped is not None
    assert scoped.agent_instance_id == session.agent_instance_id
    assert scoped.is_quiet

    # The audit read is deliberately unscoped: a lane's history includes what
    # a superseded session claimed, and the newest of those is this one.
    latest = store.read_latest_lane_quiet_confirmation(lane.clerk_id)
    assert latest == successor


# ---- durability ---------------------------------------------------------------


def test_the_confirmation_survives_a_registry_reopen(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The issue's requirement in one line: the attestation survives a lane
    restart, which means it lives in the registry rather than in a process."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q15")
    recorded = confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())

    read_back = fleet_service.read_lane_quiet_confirmation(clerk_id=lane.clerk_id)

    assert read_back == recorded
    assert read_back.is_quiet


def test_the_confirmation_table_is_append_only(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The registry's standing discipline: a confirmation is never rewritten
    or deleted, so the audit of what a lane claimed, and when, is complete."""
    import sqlite3

    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q16")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    conn = fleet_service._store._conn

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE clerk_lane_confirmations SET account_flat = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM clerk_lane_confirmations")


# ---- the gate reads the lane's LAST answer, on the coordinator's own clock ----


def test_a_lane_whose_clock_steps_back_still_closes_the_gate_it_opened(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The lane supplies ``observed_at_ms``; it must never decide which answer
    the gate reads.

    A lane host whose clock steps back — an NTP correction is enough — re-observes
    a working order and answers "not quiet" bearing an *earlier* instant than the
    quiet answer it gave a moment ago. Ordering the gate's read on that instant
    would hand the lane a way to retire itself while its own latest knowledge says
    a working order is live. The registry orders on its own append sequence
    instead, so the last answer recorded is the one the gate reads.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q18")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    confirm_quiet(
        fleet_service,
        lane,
        session,
        observed_at_ms=clock() - 2_000,
        broker_work_ended=False,
    )

    with pytest.raises(ClerkLaneQuietUnproven, match="has not ended"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_a_retried_confirmation_is_recorded_rather_than_colliding(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """A lane whose response was lost re-sends the same answer.

    That retry is the ceremony's ordinary path once the transport ships, so it
    must not surface the registry's primary key as an unhandled error. Nothing
    lane-supplied is part of the row's identity, so the re-send simply appends.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q19")
    at = clock()
    first = confirm_quiet(fleet_service, lane, session, observed_at_ms=at)
    again = confirm_quiet(fleet_service, lane, session, observed_at_ms=at)

    assert again == first
    assert fleet_service.retire_clerk(clerk_id=lane.clerk_id).lane_confirmation is not None


def test_a_retraction_at_the_same_instant_is_recorded_and_closes_the_gate(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """A lane correcting itself within the same millisecond must be able to say
    so. If the same-instant answer were rejected the gate would stay open on the
    quiet claim the lane has just withdrawn."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q20")
    at = clock()
    confirm_quiet(fleet_service, lane, session, observed_at_ms=at)
    confirm_quiet(fleet_service, lane, session, observed_at_ms=at, runner_idle=False)

    with pytest.raises(ClerkLaneQuietUnproven, match="bot is still running"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_the_scoped_read_ignores_a_successor_that_differs_only_in_epoch(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """One predicate per test, so neither can rot behind the other.

    A single case whose successor differs in both instance and epoch passes
    with either predicate deleted — it only proves that *one* of them survives.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q21")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    store = fleet_service._store
    current = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    assert current is not None
    with store.transaction() as conn:
        store.record_lane_quiet_confirmation(
            conn, replace(current, routing_epoch=session.routing_epoch + 1, account_flat=False)
        )

    scoped = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )

    assert scoped == current


def test_the_scoped_read_ignores_a_successor_that_differs_only_in_instance(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The other half of the fence, for the same reason."""
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q22")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock())
    store = fleet_service._store
    current = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    assert current is not None
    with store.transaction() as conn:
        store.record_lane_quiet_confirmation(
            conn,
            replace(current, agent_instance_id="agnt_successor00000000000000", account_flat=False),
        )

    scoped = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )

    assert scoped == current


def test_the_retirement_gate_reads_the_scoped_answer_not_the_latest_one(
    fleet_service: FleetControlService, clock: FrozenClock, control_dir: Path
) -> None:
    """The gate's *choice* of read, pinned where its consequence is visible.

    Both store reads exist and return different rows here, so a gate wired to
    the unscoped audit read would retire this lane on a dead session's quiet
    answer while its own session says a bot is still running.
    """
    lane, session, _drained = drained_lane(fleet_service, clock, control_dir, "q23")
    confirm_quiet(fleet_service, lane, session, observed_at_ms=clock(), runner_idle=False)
    store = fleet_service._store
    current = store.read_lane_quiet_confirmation_on(
        store._conn,
        lane.clerk_id,
        agent_instance_id=session.agent_instance_id,
        routing_epoch=session.routing_epoch,
    )
    assert current is not None
    with store.transaction() as conn:
        store.record_lane_quiet_confirmation(
            conn,
            replace(
                current,
                agent_instance_id="agnt_successor00000000000000",
                routing_epoch=session.routing_epoch + 1,
                runner_idle=True,
            ),
        )
    # The audit read genuinely sees the superseded session's quiet answer.
    latest = store.read_latest_lane_quiet_confirmation(lane.clerk_id)
    assert latest is not None and latest.is_quiet

    with pytest.raises(ClerkLaneQuietUnproven, match="bot is still running"):
        fleet_service.retire_clerk(clerk_id=lane.clerk_id)


def test_the_validity_window_is_sized_from_the_push_cadence(
    fleet_service: FleetControlService,
) -> None:
    """The window's magnitude is the decision, so it is pinned rather than left
    to whatever every other test reads back off the service.

    Its numeric equality with three times ``DEFAULT_SESSION_STALE_AFTER_MS`` is
    a coincidence of the current numbers and nothing can test it away: the ban
    on re-deriving it from heartbeat staleness is a review-time rule, because
    any such derivation today produces this very value.
    """
    assert DEFAULT_LANE_QUIET_VALID_FOR_MS == 90_000
    assert fleet_service.lane_quiet_valid_for_ms == DEFAULT_LANE_QUIET_VALID_FOR_MS
