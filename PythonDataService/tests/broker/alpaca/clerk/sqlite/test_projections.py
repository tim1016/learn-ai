"""Public behavior tests for bounded SQLite Clerk projections (#1395)."""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
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
    assert affected.guidance.scope == "BOT"
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
