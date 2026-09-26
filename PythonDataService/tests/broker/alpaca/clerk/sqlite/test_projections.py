"""Public behavior tests for bounded SQLite Clerk projections (#1395)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import RecoveryPricing
from app.broker.alpaca.clerk.sqlite import projections
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.exit_recovery import RecoveryResult, record_exit_recovery
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projections import (
    SqliteClerkProjectionReader,
    timeline_sequences,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerOrderLeg
from app.utils.timestamps import to_ms_utc

ACCOUNT_ID = "PA-PROJECTION"
SID = "spy-bot"
OTHER_SID = "qqq-bot"

# The authority's pricing seam an Alpaca lane with a sealed exit allowance
# re-drives from: the 04:00-20:00 declared window, so the watchdog prices a
# limit in pre-market and after-hours (#2229). No quote is ever read by a
# projection.
_XH_PRICING = RecoveryPricing(
    policy_for=lambda _sid: ProgramLegPolicy(
        window=ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60),
        allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")),
    ),
    quote_source=lambda symbol, now_ms: None,
)
_ET = ZoneInfo("America/New_York")


def _et(hour: int, minute: int = 0, second: int = 0, *, day: int = 2, month: int = 9) -> int:
    """An ET wall-clock instant in September 2026 (the 2nd is a Wednesday, the 3rd a Thursday)."""
    return to_ms_utc(datetime(2026, month, day, hour, minute, second, tzinfo=_ET))


@pytest.fixture(autouse=True)
def _fresh_unreadable_log() -> Iterator[None]:
    """The once-per-record log memory is process-wide; each test starts without it."""
    projections._UNREADABLE_LOGGED.clear()
    yield
    projections._UNREADABLE_LOGGED.clear()


class _Clock:
    def __init__(self) -> None:
        self.value = 1_700_000_000_000

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _repository(tmp_path: Path, clock: _Clock) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="spy-hash",
    )
    repo.register_strategy_instance(
        strategy_instance_id=OTHER_SID,
        symbol="QQQ",
        config_hash="qqq-hash",
    )
    return repo


def test_bot_snapshot_reads_fold_state_and_backend_authors_recovery(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert snapshot is not None
    assert snapshot.authority_health == "healthy"
    assert snapshot.custody_owner == "ACCOUNT_CLERK"
    assert snapshot.runs[0].state == "ACTIVE"
    assert snapshot.commands[0].action == "START"
    assert snapshot.guidance.may_create_exposure is True
    actions = {action.action_id: action for action in snapshot.recovery_actions}
    assert actions["stop_bot_decisions"].available is True
    assert actions["stop_bot_decisions"].confirmation is not None
    assert "clear_hold" not in actions
    assert "rebuild_from_mirror" not in actions
    assert "reset_authority" not in actions


def test_bot_snapshot_exposes_immutable_order_leg_and_verified_zero_fill_total(
    tmp_path: Path,
) -> None:
    """Working-order presentation reads requested leg data from SQLite facts."""
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="order-details",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=3),
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert snapshot is not None
    order = next(
        order
        for operation in snapshot.operations
        for order in operation.orders
        if order.order_ref == accepted.order_ref
    )
    assert order.symbol == "SPY"
    assert order.side == "buy"
    assert order.quantity == 3.0
    assert order.filled_quantity == 0.0


def test_account_snapshot_projects_sparse_exit_reducing_order_facts(
    tmp_path: Path,
) -> None:
    """A valid reducing-order fact must not make every Operator read fail."""
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="reducing-projection",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=3),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.command.run_id is not None
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=SID,
            run_id=accepted.command.run_id,
            command_id=accepted.command.command_id,
            effect_operation_id=accepted.effect_operation_id,
            order_ref="learn-ai/spy/v1:reducing-projection",
            transition_kind="EXIT_REDUCING_ORDER_CREATED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            clerk_observed_at_ms=clock(),
            summary_code="EXIT_REDUCING_ORDER_CREATED",
            facts_json=ExitReducingOrderCreatedFacts(
                symbol="SPY",
                side="SELL",
                quantity=3,
            ).to_facts_json(),
        )
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        snapshot = reader.account_snapshot()
    finally:
        reader.close()
        repo.close()

    reducing = next(
        order
        for operation in snapshot.operations
        for order in operation.orders
        if order.order_ref == "learn-ai/spy/v1:reducing-projection"
    )
    assert reducing.symbol == "SPY"
    assert reducing.side == "sell"
    assert reducing.quantity == 3.0
    assert reducing.order_type is None
    assert reducing.limit_price is None
    assert reducing.time_in_force is None


def test_bot_uncertainty_does_not_leak_to_another_bot_projection(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="SPY order outcome is unknown",
        explanation="Alpaca has not proven the exact order terminal.",
        operator_impact="Only SPY bot entries are paused.",
        next_step="The Clerk will reconcile automatically.",
        evidence_refs=("order:spy",),
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        affected = reader.bot_snapshot(SID)
        unaffected = reader.bot_snapshot(OTHER_SID)
    finally:
        reader.close()
        repo.close()

    assert affected is not None
    assert affected.guidance.scope == "CUSTODY_SUBJECT"
    assert affected.guidance.may_create_exposure is False
    assert [item.reason_code for item in affected.uncertainties] == [
        "ORDER_OUTCOME_UNKNOWN"
    ]
    assert unaffected is not None
    assert unaffected.uncertainties == ()
    assert unaffected.guidance.may_create_exposure is True


def test_one_unreadable_uncertainty_is_projected_as_unreadable_without_failing_the_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#2440 review: one episode whose facts cannot be read fails loudly, but only itself.

    The custody read parses each open episode's facts for when the Clerk next
    tries. A row it cannot parse — damaged, or written by a later schema —
    used to fail the whole read, blanking every episode on the page. It is
    now projected from its own columns, flagged unreadable, and logged at
    error level; every other episode reads as before.
    """
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code="EXIT_NOT_FLAT",
        headline="An exit could not be sent after its session ended",
        explanation="10 SPY is still held.",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Let the automatic re-drive reduce it.",
        evidence_refs=("order:exit",),

    )
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="SPY order outcome is unknown",
        explanation="Alpaca has not proven the exact order terminal.",
        operator_impact="Only SPY bot entries are paused.",
        next_step="The Clerk will reconcile automatically.",
        evidence_refs=("order:spy",),
    )
    repo._conn.execute(
        "UPDATE uncertainties SET facts_json = ? WHERE reason_code = 'ORDER_OUTCOME_UNKNOWN'",
        ('{"written_by_a_later_schema":true}',),
    )
    repo._conn.commit()
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        with caplog.at_level(logging.ERROR):
            snapshot = reader.bot_snapshot(SID)
            # Every surface polls; the damaged record is logged once, not per read.
            reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert snapshot is not None
    by_reason = {item.reason_code: item for item in snapshot.uncertainties}
    readable = by_reason["EXIT_NOT_FLAT"]
    assert readable.recovery_status.reason_code == "RECOVERY_NOT_CHECKED"
    unreadable = by_reason["ORDER_OUTCOME_UNKNOWN"]
    assert unreadable.recovery_status.reason_code == "RECOVERY_RECORD_UNREADABLE"
    assert unreadable.headline == "SPY order outcome is unknown"
    assert unreadable.blocks_new_exposure is True
    (logged,) = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "uncertainty_facts_unreadable"
    ]
    assert logged.levelno == logging.ERROR
    assert logged.__dict__["uncertainty_id"] == unreadable.uncertainty_id


def _raise_exit_not_flat(repo: ClerkSqliteRepository, sid: str, *, next_attempt_at_ms: int) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=sid,
        reason_code="EXIT_NOT_FLAT",
        headline="An exit could not be sent after its session ended",
        explanation="10 SPY is still held.",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Let the automatic re-drive reduce it.",
        evidence_refs=(f"order:exit:{sid}",),
        cause_facts={"symbol": "SPY", "attributed_qty": 10.0},
        severity="error",

    )


def _observe_retry(repo: ClerkSqliteRepository, sid: str, allowed_from_ms: int | None) -> None:
    episode = repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=sid)
    assert episode is not None
    record_exit_recovery(
        repo, strategy_instance_id=sid, uncertainty_id=episode["uncertainty_id"],
        result=RecoveryResult("hold", "NO_SESSION_OPEN", "The next session has not opened.", allowed_from_ms),
        evaluated_at_ms=repo.clock(), pass_started_at_ms=repo.clock(),
    )


@pytest.mark.parametrize("offset,kind", [(3_600_000, "allowed_from"), (-60_000, "allowed_now")])
def test_recovery_eligibility_is_evaluated_and_does_not_show_a_past_due_time(tmp_path: Path, offset: int, kind: str) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    allowed = clock.value + offset
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=allowed)
    _observe_retry(repo, SID, allowed)
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        snapshot = reader.bot_snapshot(SID)
        status = snapshot.uncertainties[0].recovery_status
        assert status.kind == kind
        assert status.allowed_from_ms == (allowed if offset > 0 else None)
        assert snapshot.guidance.recovery_status == status
    finally:
        reader.close()
        repo.close()


def test_an_escalated_exit_projects_no_next_attempt(tmp_path: Path) -> None:
    """#2440 review: once the watchdog escalates to EXIT_STUCK it re-drives no more.

    The EXIT_NOT_FLAT episode stays open beside EXIT_STUCK, still carrying
    the time its fold recorded; the projection drops it for that strategy
    only, since nothing will try then.
    """
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    future_at_ms = clock.value + 3_600_000
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=future_at_ms)
    _raise_exit_not_flat(repo, OTHER_SID, next_attempt_at_ms=future_at_ms)
    _observe_retry(repo, SID, future_at_ms)
    _observe_retry(repo, OTHER_SID, future_at_ms)
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code="EXIT_STUCK",
        headline="Automatic exit re-drives stopped",
        explanation="The re-drive budget is spent.",
        operator_impact="New exposure is paused for this strategy.",
        next_step="Flatten the position.",
        evidence_refs=("order:exit:spy-bot",),
        severity="error",
    )
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        snapshot = reader.account_snapshot()
    finally:
        reader.close()
        repo.close()

    next_attempts = {
        (item.strategy_instance_id, item.reason_code): None if item.recovery_status is None else item.recovery_status.allowed_from_ms
        for item in snapshot.uncertainties
    }
    assert next_attempts == {
        (SID, "EXIT_NOT_FLAT"): None,
        (SID, "EXIT_STUCK"): None,
        (OTHER_SID, "EXIT_NOT_FLAT"): future_at_ms,
    }
    # Y m8: the escalated episode's own next step pointed at the time it no
    # longer shows; it now says the automatic attempts stopped.
    next_steps = {
        item.strategy_instance_id: item.next_step
        for item in snapshot.uncertainties
        if item.reason_code == "EXIT_NOT_FLAT"
    }
    assert next_steps == {
        SID: (
            "Automatic attempts to reduce this position have stopped. Run Reconcile now, "
            "then execute the presented safe flatten."
        ),
        OTHER_SID: "Let the automatic re-drive reduce it.",
    }


@pytest.mark.parametrize("delay_ms", [0, 60_000, 86_400_000, 4 * 86_400_000])
def test_an_unevaluated_exit_never_infers_retry_eligibility(tmp_path: Path, delay_ms: int) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=clock.value + 120_000)
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=lambda: clock.value + delay_ms)
    try:
        [episode] = reader.bot_snapshot(SID).uncertainties
        assert episode.recovery_status.allowed_from_ms is None
        assert episode.recovery_status.kind == "unknown"
        assert episode.recovery_status.last_checked_at_ms is None
    finally:
        reader.close()
        repo.close()


def test_status_keeps_original_episode_age_without_refreshing_evaluation(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=clock.value + 120_000)
    _observe_retry(repo, SID, clock.value + 120_000)
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        original = reader.bot_snapshot(SID).uncertainties[0].recovery_status
        for _ in range(6):
            clock.value += 10_000
            repo.renew_execution_lease()
        _raise_exit_not_flat(repo, SID, next_attempt_at_ms=clock.value + 120_000)
        current = reader.bot_snapshot(SID).uncertainties[0].recovery_status
        assert current.kind == "unknown"
        assert current.last_checked_at_ms == original.last_checked_at_ms
        assert current.stuck_since_ms == original.stuck_since_ms
    finally:
        reader.close()
        repo.close()


def test_a_record_with_mistyped_facts_is_unreadable_and_never_breaks_the_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#2440 review (Y m5): facts that parse but carry the wrong types fail only their own row.

    A cause that is not an object used to raise AttributeError reading its
    symbol, and a next attempt that is not an int64 ms UTC raised TypeError
    outside the guard — either one blanked the desk, the bot page and the
    bell. Each is now projected unreadable and logged once, however often the
    surfaces poll.
    """
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=clock.value + 3_600_000)
    _raise_exit_not_flat(repo, OTHER_SID, next_attempt_at_ms=clock.value + 3_600_000)
    for sid, field, value in (
        (SID, "cause_facts", ["SPY"]),
        (OTHER_SID, "cause_facts", "not an object"),
    ):
        (facts_json,) = repo._conn.execute(
            "SELECT facts_json FROM uncertainties WHERE strategy_instance_id = ?", (sid,)
        ).fetchone()
        facts = json.loads(facts_json)
        facts[field] = value
        repo._conn.execute(
            "UPDATE uncertainties SET facts_json = ? WHERE strategy_instance_id = ?",
            (json.dumps(facts), sid),
        )
    repo._conn.commit()
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        with caplog.at_level(logging.ERROR):
            snapshot = reader.account_snapshot()
            reader.account_snapshot()
    finally:
        reader.close()
        repo.close()

    assert {
        item.strategy_instance_id: (item.symbol, item.recovery_status.allowed_from_ms, item.recovery_status.reason_code)
        for item in snapshot.uncertainties
    } == {SID: (None, None, "RECOVERY_RECORD_UNREADABLE"), OTHER_SID: (None, None, "RECOVERY_RECORD_UNREADABLE")}
    logged = [
        record.__dict__["strategy_instance_id"]
        for record in caplog.records
        if getattr(record, "action", None) == "uncertainty_facts_unreadable"
    ]
    assert sorted(logged) == sorted([SID, OTHER_SID])


def test_a_legacy_future_time_is_not_reinterpreted_by_the_projection(
    tmp_path: Path,
) -> None:
    """#2440 final review (m3): an in-range int64 the calendar cannot project fails only its row.

    ``_checked_record`` admits any ``next_attempt_at_ms`` up to
    ``MAX_TIMESTAMP_MS``, but re-projecting one past pandas' range (year 3000
    here) raised ``OverflowError`` outside the guard, blanking the desk, the
    bot page and the bell. Only a damaged record holds such a value; it is
    now projected unreadable like any other.
    """
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    year_3000_ms = 32_503_680_000_000
    _raise_exit_not_flat(repo, SID, next_attempt_at_ms=year_3000_ms)
    _raise_exit_not_flat(repo, OTHER_SID, next_attempt_at_ms=clock.value + 3_600_000)
    for sid, legacy_ms in ((SID, year_3000_ms), (OTHER_SID, clock.value + 3_600_000)):
        row = repo._conn.execute("SELECT facts_json FROM uncertainties WHERE strategy_instance_id=?", (sid,)).fetchone()
        facts = json.loads(row[0])
        facts["next_attempt_at_ms"] = legacy_ms
        repo._conn.execute("UPDATE uncertainties SET facts_json=? WHERE strategy_instance_id=?", (json.dumps(facts), sid))
    repo._conn.commit()
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
    try:
        snapshot = reader.account_snapshot()
    finally:
        reader.close()
        repo.close()

    shown = {item.strategy_instance_id: item.recovery_status.kind for item in snapshot.uncertainties}
    assert shown == {SID: "unknown", OTHER_SID: "unknown"}


def test_timeline_cursor_is_stable_while_new_transitions_append(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        first_page = reader.timeline_page(page_size=1)
        assert first_page.next_cursor is not None

        repo.register_strategy_instance(
            strategy_instance_id="iwm-bot",
            symbol="IWM",
            config_hash="iwm-hash",
        )
        second_page = reader.timeline_page(
            cursor=first_page.next_cursor,
            page_size=10,
        )
    finally:
        reader.close()
        repo.close()

    assert first_page.anchor_sequence == 2
    assert timeline_sequences(first_page.entries) == (2,)
    assert timeline_sequences(second_page.entries) == (1,)
    assert all(
        entry.sequence <= first_page.anchor_sequence for entry in second_page.entries
    )


def test_timeline_exposes_source_observation_and_record_clocks(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        page = reader.timeline_page(strategy_instance_id=SID)
    finally:
        reader.close()
        repo.close()

    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.operation_ref == f"transition:{entry.sequence}"
    assert entry.source_event_at_ms is None
    assert entry.clerk_observed_at_ms > 0
    assert entry.recorded_at_ms > 0


def test_timeline_can_filter_by_effect_operation_identity(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-1",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        page = reader.timeline_page(
            strategy_instance_id=SID,
            effect_operation_id=accepted.effect_operation_id,
        )
    finally:
        reader.close()
        repo.close()

    assert page.total_entries > 0
    assert {entry.effect_operation_id for entry in page.entries} == {
        accepted.effect_operation_id
    }


def test_bot_snapshot_is_one_coherent_read_despite_a_concurrent_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """#1396 P2: `_snapshot` issues its reads (`_meta`, `_runs`, `_operations`,
    ..., `_uncertainties`) without wrapping them in one transaction, so the
    Python lock alone cannot stop the live repository connection from
    committing a fold in between two of them. Simulate exactly that: commit a
    brand-new account-wide uncertainty while `_snapshot` is mid-read (right
    after `_runs`, before `_uncertainties`) and assert the returned
    projection reflects neither that uncertainty nor a control_revision
    ahead of the point where the read began — one coherent snapshot, not a
    mix of two."""
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    control_revision_before = repo.control_meta_snapshot().control_revision
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    injected_control_revision: list[int] = []
    original_runs = reader._runs

    def _runs_then_concurrent_write(strategy_instance_id: str | None):
        result = original_runs(strategy_instance_id)
        raise_uncertainty(
            repo,
            strategy_instance_id=None,
            reason_code="CONCURRENT_WRITE_DURING_READ",
            headline="Injected mid-read write",
            explanation="Proves the reader's transaction isolates a concurrent commit.",
            operator_impact="none — test only",
            next_step="none",
        )
        injected_control_revision.append(repo.control_meta_snapshot().control_revision)
        return result

    monkeypatch.setattr(reader, "_runs", _runs_then_concurrent_write)

    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert injected_control_revision, "the concurrent write must have actually landed"
    assert injected_control_revision[0] > control_revision_before
    assert snapshot is not None
    assert snapshot.control_revision == control_revision_before
    assert snapshot.uncertainties == ()


def test_operation_page_is_stable_when_a_new_operation_appends(tmp_path: Path, monkeypatch) -> None:
    """This test is about `SqliteClerkProjectionReader`'s read-side keyset
    pagination, not write-side admission — but exercising it needs more than
    one ENTER effect operation live under one strategy at once, and #1722
    fenced a fresh ENTER behind ATTRIBUTED_EXPOSURE_EXISTS/ENTER_IN_PROGRESS
    whenever attributed exposure or a nonterminal ENTER already exists (ADR
    0042, PRD FR-020). The read layer must still paginate correctly over
    however many operations exist (idempotent replay, legacy data, a future
    carve-out), so bypass the fence to construct that state directly.
    """
    monkeypatch.setattr("app.broker.alpaca.clerk.sqlite.enter.require_admission", lambda *a, **kw: None)
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)
    accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-1",
        lifecycle_run_id="run-1",
        leg=leg,
    )
    accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-2",
        lifecycle_run_id="run-1",
        leg=leg,
    )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        first = reader.operation_page(strategy_instance_id=SID, page_size=1)
        assert first.next_cursor is not None
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id="decision-3",
            lifecycle_run_id="run-1",
            leg=leg,
        )
        second = reader.operation_page(
            strategy_instance_id=SID,
            cursor=first.next_cursor,
            page_size=10,
        )
    finally:
        reader.close()
        repo.close()

    operation_ids = {
        operation.effect_operation_id
        for operation in (*first.operations, *second.operations)
    }
    assert operation_ids == {
        f"effect:{SID}:decision-1",
        f"effect:{SID}:decision-2",
    }


def test_operation_page_does_not_drop_an_operation_whose_updated_at_ms_advances_mid_traversal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """#1396 P2: the keyset cursor must anchor on an immutable key. Folding a
    fill (or any other evidence) onto a not-yet-paged operation moves its
    `updated_at_ms` forward — if the cursor anchored on that mutable column,
    the operation would jump above the anchor and vanish from every
    remaining page. It must still be reachable via the next page.

    Exercising three simultaneously-live ENTER operations under one
    strategy is itself now fenced (#1722, ADR 0042, PRD FR-020:
    ATTRIBUTED_EXPOSURE_EXISTS/ENTER_IN_PROGRESS) — bypass that write-side
    fence here since this test is about read-side pagination, which must
    still be correct over however many operations the ledger holds.
    """
    monkeypatch.setattr("app.broker.alpaca.clerk.sqlite.enter.require_admission", lambda *a, **kw: None)
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)
    for decision_id in ("decision-1", "decision-2", "decision-3"):
        accept_enter(
            repo,
            account_id=ACCOUNT_ID,
            strategy_instance_id=SID,
            decision_id=decision_id,
            lifecycle_run_id="run-1",
            leg=leg,
        )
    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        first = reader.operation_page(strategy_instance_id=SID, page_size=1)
        assert first.next_cursor is not None
        assert {op.effect_operation_id for op in first.operations} == {
            f"effect:{SID}:decision-3"
        }

        # decision-1 is the oldest operation, not yet shown on any page.
        # Advance its `updated_at_ms` far past every other operation's,
        # exactly as a fold would on new evidence — without touching
        # `created_at_ms`, the immutable key the cursor now anchors on.
        repo._conn.execute(
            "UPDATE effect_operations SET updated_at_ms = ? WHERE effect_operation_id = ?",
            (clock() + 1_000_000, f"effect:{SID}:decision-1"),
        )
        repo._conn.commit()

        second = reader.operation_page(
            strategy_instance_id=SID,
            cursor=first.next_cursor,
            page_size=10,
        )
    finally:
        reader.close()
        repo.close()

    operation_ids = {
        operation.effect_operation_id for operation in (*first.operations, *second.operations)
    }
    assert operation_ids == {
        f"effect:{SID}:decision-1",
        f"effect:{SID}:decision-2",
        f"effect:{SID}:decision-3",
    }


def test_recovery_policy_reads_working_orders_outside_the_operation_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Needs two simultaneously-live ENTER operations under one strategy,
    which #1722's ENTER fence (ADR 0042, PRD FR-020) now refuses through the
    normal decision path — bypass it here since this test is about the
    read-side recovery policy, not write-side admission.
    """
    monkeypatch.setattr("app.broker.alpaca.clerk.sqlite.enter.require_admission", lambda *a, **kw: None)
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)
    older = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-older",
        lifecycle_run_id="run-1",
        leg=leg,
    )
    newer = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-newer",
        lifecycle_run_id="run-1",
        leg=leg,
    )
    observed_at_ms = clock()
    repo._conn.execute(
        "UPDATE orders SET broker_order_id = ?, broker_state = 'accepted', "
        "submitted_at_ms = ?, updated_at_ms = ? WHERE order_ref = ?",
        ("alpaca-order-older", observed_at_ms, observed_at_ms, older.order_ref),
    )
    repo._conn.execute(
            "INSERT INTO positions "
            "(subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES (?, ?, 'SPY', 1.0, ?)",
            (f"bot:{SID}", SID, observed_at_ms),
    )
    repo._conn.commit()

    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        snapshot = reader.bot_snapshot(SID, operation_limit=1)
    finally:
        reader.close()
        repo.close()

    assert snapshot is not None
    assert [operation.effect_operation_id for operation in snapshot.operations] == [
        newer.effect_operation_id
    ]
    actions = {action.action_id: action for action in snapshot.recovery_actions}
    assert actions["cancel_verified_working_orders"].available is True
    assert [item.reference for item in actions["cancel_verified_working_orders"].evidence] == [
        f"order:{older.order_ref}"
    ]
    assert actions["prepare_safe_flatten"].unavailable_reason_code == (
        "WORKING_ORDERS_REQUIRE_CANCEL_FIRST"
    )


def test_safe_flatten_uses_account_reconciliation_not_newer_effect_attempt(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    submit_start_run(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        lifecycle_run_id="run-1",
        clock=clock,
    )
    accepted = accept_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="decision-1",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    position_at_ms = clock()
    account_reconciliation_at_ms = clock()
    effect_reconciliation_at_ms = clock()
    repo._conn.execute(
            "INSERT INTO positions "
            "(subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES (?, ?, 'SPY', 1.0, ?)",
            (f"bot:{SID}", SID, position_at_ms),
    )
    repo._conn.execute(
        "INSERT INTO reconciliations "
        "(reconciliation_id, effect_operation_id, order_ref, trigger, "
        "attempted_at_ms, outcome, evidence_refs_json) "
        "VALUES ('reconciliation:account', NULL, NULL, 'OPERATOR_RECONCILE_NOW', "
        "?, 'RESOLVED_SUCCESS', NULL)",
        (account_reconciliation_at_ms,),
    )
    repo._conn.execute(
        "INSERT INTO reconciliations "
        "(reconciliation_id, effect_operation_id, order_ref, trigger, "
        "attempted_at_ms, outcome, evidence_refs_json) "
        "VALUES ('reconciliation:effect', ?, ?, 'AUTOMATIC', "
        "?, 'RESOLVED_SUCCESS', NULL)",
        (
            accepted.effect_operation_id,
            accepted.order_ref,
            effect_reconciliation_at_ms,
        ),
    )
    repo._conn.commit()

    reader = SqliteClerkProjectionReader.from_repository(
        repo, clock=clock
    )
    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
        repo.close()

    assert snapshot is not None
    assert snapshot.latest_reconciliation is not None
    assert snapshot.latest_reconciliation.reconciliation_id == "reconciliation:effect"
    capability = next(
        action
        for action in snapshot.recovery_actions
        if action.action_id == "prepare_safe_flatten"
    )
    assert capability.available is True
    assert capability.reduction_plan is not None
    assert capability.reduction_plan.reconciliation_id == "reconciliation:account"


def test_hot_projection_queries_use_covering_fold_indexes(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)

    plans = {
        "account_commands": repo._conn.execute(
            "EXPLAIN QUERY PLAN SELECT command_id FROM commands "
            "ORDER BY updated_at_ms DESC, command_id DESC LIMIT 50"
        ).fetchall(),
        "bot_commands": repo._conn.execute(
            "EXPLAIN QUERY PLAN SELECT command_id FROM commands "
            "WHERE strategy_instance_id = ? "
            "ORDER BY updated_at_ms DESC, command_id DESC LIMIT 50",
            (SID,),
        ).fetchall(),
        "account_operations": repo._conn.execute(
            "EXPLAIN QUERY PLAN SELECT effect_operation_id FROM effect_operations "
            "ORDER BY updated_at_ms DESC, effect_operation_id DESC LIMIT 50"
        ).fetchall(),
        "bot_operations": repo._conn.execute(
            "EXPLAIN QUERY PLAN SELECT effect_operation_id FROM effect_operations "
            "WHERE strategy_instance_id = ? "
            "ORDER BY updated_at_ms DESC, effect_operation_id DESC LIMIT 50",
            (SID,),
        ).fetchall(),
        "bot_timeline": repo._conn.execute(
            "EXPLAIN QUERY PLAN SELECT sequence FROM custody_transitions "
            "WHERE sequence <= ? AND sequence < ? AND strategy_instance_id = ? "
            "ORDER BY sequence DESC LIMIT 25",
            (100, 101, SID),
        ).fetchall(),
    }
    repo.close()

    details = {
        name: " | ".join(str(row[3]) for row in rows)
        for name, rows in plans.items()
    }
    assert "ix_commands_updated_at" in details["account_commands"]
    assert "ix_commands_strategy_updated_at" in details["bot_commands"]
    assert "ix_effect_operations_updated_at" in details["account_operations"]
    assert "ix_effect_operations_strategy_updated_at" in details["bot_operations"]
    assert "ix_custody_transitions_strategy_sequence" in details["bot_timeline"]


@pytest.mark.parametrize("stopped,working,kind", [(True, False, "stuck"), (False, True, "working")])
def test_current_recovery_custody_facts_outrank_a_missing_check(tmp_path: Path, stopped, working, kind):
    from app.broker.alpaca.clerk.sqlite.recovery_status import read_recovery_status
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    try:
        _raise_exit_not_flat(repo, SID, next_attempt_at_ms=None)
        episode = repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=SID)
        status = read_recovery_status(repo._conn, strategy_instance_id=SID, uncertainty_id=episode["uncertainty_id"],
            now_ms=clock(), stopped=stopped, working=working)
        assert status.kind == kind
    finally:
        repo.close()


def test_related_account_and_bot_reads_share_one_revision(tmp_path: Path) -> None:
    repo = _repository(tmp_path, _Clock())
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        with reader.snapshot():
            account = reader.account_snapshot()
            submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="later-run")
            bot = reader.bot_snapshot(SID)
            assert bot is not None
            assert bot.control_revision == account.control_revision
            assert bot.runs == ()
        refreshed = reader.bot_snapshot(SID)
        assert refreshed is not None
        assert refreshed.runs[0].state == "ACTIVE"
        assert refreshed.control_revision > account.control_revision
    finally:
        reader.close()
        repo.close()
