"""Decision receipt storage tests for the SQLite Clerk authority."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    SqliteDecisionReceipts,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository


@pytest.fixture()
def repository(tmp_path: Path) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        config_hash="test-config-hash",
    )
    yield repo
    repo.close()


@pytest.fixture()
def receipts(repository: ClerkSqliteRepository) -> SqliteDecisionReceipts:
    return SqliteDecisionReceipts(repository, strategy_instance_id="spy-bot")


def test_append_assigns_sequence_and_preserves_nullable_transaction_fields(
    receipts: SqliteDecisionReceipts,
) -> None:
    first = receipts.append(
        outcome="no_action",
        symbol=None,
        observed_at_ms=1_700_000_000_000,
        facts={"reason_code": "NO_ACTION", "bar_ref": "SPY@1700000000000"},
    )
    second = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1_700_000_060_000,
        intent_id="intent-1",
        order_ref=None,
        facts={"bar_ref": "SPY@1700000060000", "reason_code": "STRATEGY_ENTER"},
    )

    assert first.seq == 1
    assert first.symbol is None
    assert first.intent_id is None
    assert first.order_ref is None
    assert second.seq == 2
    assert json.loads(second.facts_json) == {
        "bar_ref": "SPY@1700000060000",
        "reason_code": "STRATEGY_ENTER",
    }


def test_tail_clamps_and_returns_ascending_suffix(
    receipts: SqliteDecisionReceipts,
) -> None:
    for index in range(MAX_DECISION_RECEIPT_READ + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    tail = receipts.tail(MAX_DECISION_RECEIPT_READ + 10)

    assert len(tail) == MAX_DECISION_RECEIPT_READ
    assert tail[0].seq == 3
    assert tail[-1].seq == MAX_DECISION_RECEIPT_READ + 2


def test_by_transaction_matches_intent_or_order_and_stays_bounded(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    _insert_order_reference(repository, order_ref="order-1")
    receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1,
        intent_id="intent-1",
        facts={"reason_code": "STRATEGY_ENTER"},
    )
    receipts.append(
        outcome="blocked",
        symbol="SPY",
        observed_at_ms=2,
        order_ref="order-1",
        facts={"reason_code": "HOLD_ACTIVE"},
    )
    receipts.append(
        outcome="no_action",
        symbol="SPY",
        observed_at_ms=3,
        facts={"reason_code": "NO_ACTION"},
    )

    assert [receipt.seq for receipt in receipts.by_transaction("intent-1")] == [1]
    assert [receipt.seq for receipt in receipts.by_transaction("order-1")] == [2]
    assert receipts.by_transaction("not-found") == []


def test_receipts_do_not_advance_custody_transition_or_control_revision(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    before = repository.control_meta_snapshot()
    transitions_before = repository.custody_transitions()

    receipts.append(
        outcome="no_action",
        symbol="SPY",
        observed_at_ms=1,
        facts={"reason_code": "NO_ACTION"},
    )

    assert repository.control_meta_snapshot().control_revision == before.control_revision
    assert repository.custody_transitions() == transitions_before


def test_write_rejects_missing_strategy_and_reader_rejects_invalid_bounds(
    repository: ClerkSqliteRepository,
    receipts: SqliteDecisionReceipts,
) -> None:
    unregistered = SqliteDecisionReceipts(repository, strategy_instance_id="missing")

    with pytest.raises(sqlite3.IntegrityError):
        unregistered.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=1,
            facts={"reason_code": "NO_ACTION"},
        )
    with pytest.raises(ValueError, match="positive"):
        receipts.tail(0)
    with pytest.raises(ValueError, match="non-empty"):
        receipts.by_transaction("")


def _insert_order_reference(repository: ClerkSqliteRepository, *, order_ref: str) -> None:
    """Seed the required Clerk-owned order identity for an order-ref receipt."""
    repository._conn.execute(
        "INSERT INTO commands "
        "(command_id, authority_generation, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, intended_end_state, state, "
        "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        "VALUES ('command-1', 1, 'command-key', 'hash', 'strategy_decision', "
        "'spy-bot', NULL, 'ENTER', NULL, 'accepted', NULL, NULL, 1, 1)"
    )
    repository._conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('effect-1', 1, 'effect-key', 'command-1', 'spy-bot', NULL, "
        "'ENTER', 'accepted', 'ACCOUNT_CLERK', 1, 1)"
    )
    repository._conn.execute(
        "INSERT INTO orders "
        "(order_ref, effect_operation_id, client_order_id, broker_order_id, role, "
        "broker_state, submitted_at_ms, updated_at_ms) "
        "VALUES (?, 'effect-1', ?, NULL, 'ENTRY', NULL, NULL, 1)",
        (order_ref, order_ref),
    )
    repository._conn.commit()
