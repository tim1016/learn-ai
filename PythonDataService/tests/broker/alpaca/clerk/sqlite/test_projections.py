"""Public behavior tests for bounded SQLite Clerk projections (#1395)."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projections import (
    SqliteClerkProjectionReader,
    timeline_sequences,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.contract.models import BrokerOrderLeg

ACCOUNT_ID = "PA-PROJECTION"
SID = "spy-bot"
OTHER_SID = "qqq-bot"


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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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


def test_timeline_cursor_is_stable_while_new_transitions_append(tmp_path: Path) -> None:
    clock = _Clock()
    repo = _repository(tmp_path, clock)
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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


def test_operation_page_is_stable_when_a_new_operation_appends(tmp_path: Path) -> None:
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
) -> None:
    """#1396 P2: the keyset cursor must anchor on an immutable key. Folding a
    fill (or any other evidence) onto a not-yet-paged operation moves its
    `updated_at_ms` forward — if the cursor anchored on that mutable column,
    the operation would jump above the anchor and vanish from every
    remaining page. It must still be reachable via the next page."""
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
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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

    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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

    reader = SqliteClerkProjectionReader.from_repository(repo, clock=clock)
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
