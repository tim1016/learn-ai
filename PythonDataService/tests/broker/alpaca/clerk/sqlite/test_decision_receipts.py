"""Decision receipt storage tests for the SQLite Clerk authority."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    MAX_DECISION_RECEIPTS_PER_STRATEGY,
    DecisionReceiptConflictError,
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


def test_append_is_idempotent_per_closed_bar_and_rejects_conflicting_replay(
    receipts: SqliteDecisionReceipts,
) -> None:
    first = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1,
        intent_id="1:ENTER",
        facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_ENTER"},
    )

    replay = receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=2,
        intent_id="1:ENTER",
        facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_ENTER"},
    )

    assert replay == first
    with pytest.raises(DecisionReceiptConflictError, match="different decision"):
        receipts.append(
            outcome="exit_intent",
            symbol="SPY",
            observed_at_ms=3,
            intent_id="1:EXIT",
            facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_EXIT"},
        )


def test_final_outcome_replaces_the_provisional_receipt_without_new_sequence(
    receipts: SqliteDecisionReceipts,
) -> None:
    receipts.append(
        outcome="enter_intent",
        symbol="SPY",
        observed_at_ms=1,
        intent_id="1:ENTER",
        facts={"bar_ref": "SPY@1", "reason_code": "STRATEGY_ENTER"},
    )

    blocked = receipts.update_final_outcome(
        bar_ref="SPY@1",
        outcome="blocked",
        facts={
            "bar_ref": "SPY@1",
            "reason_code": "CLERK_ADMISSION_REJECTED",
            "refusal_reason": "Stream health is stale.",
        },
    )

    assert blocked.seq == 1
    assert blocked.outcome == "blocked"
    assert json.loads(blocked.facts_json)["refusal_reason"] == "Stream health is stale."
    assert [receipt.seq for receipt in receipts.tail(2)] == [1]


def test_append_prunes_receipts_beyond_the_retention_bound(
    receipts: SqliteDecisionReceipts,
    repository: ClerkSqliteRepository,
) -> None:
    for index in range(MAX_DECISION_RECEIPTS_PER_STRATEGY + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    retained = repository.decision_receipt_tail(
        strategy_instance_id="spy-bot",
        limit=MAX_DECISION_RECEIPTS_PER_STRATEGY,
    )

    assert len(retained) == MAX_DECISION_RECEIPTS_PER_STRATEGY
    assert retained[0].seq == 3
    assert retained[-1].seq == MAX_DECISION_RECEIPTS_PER_STRATEGY + 2


def test_candidate_uncaptured_at_crash_survives_retention_pruning(
    receipts: SqliteDecisionReceipts,
    repository: ClerkSqliteRepository,
) -> None:
    """FR-016 / AC #9: crash-window evidence must outlive the ordinary
    sequence-tail compaction the same way ``protected_effect`` and
    ``protected_refusal`` already do (decision_receipts.py:218-229)."""
    crash_receipt = receipts.append(
        outcome="candidate_uncaptured_at_crash",
        symbol="SPY",
        observed_at_ms=0,
        intent_id="crash-candidate-1",
        facts={
            "bar_ref": "SPY@0",
            "reason_code": "CANDIDATE_UNCAPTURED_AT_CRASH",
            "retention_class": "protected_crash_evidence",
        },
    )
    for index in range(1, MAX_DECISION_RECEIPTS_PER_STRATEGY + 6):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    retained = repository.decision_receipt_tail(
        strategy_instance_id="spy-bot",
        limit=MAX_DECISION_RECEIPTS_PER_STRATEGY + 10,
    )
    retained_by_seq = {receipt.seq: receipt for receipt in retained}

    # Ordinary tail retention alone would have pruned seq 1 (and seq 2-6):
    # the newest row is seq MAX+6, so anything at or below seq 6 falls
    # outside the last MAX_DECISION_RECEIPTS_PER_STRATEGY rows.
    assert len(retained) == MAX_DECISION_RECEIPTS_PER_STRATEGY + 1
    assert 2 not in retained_by_seq
    survivor = retained_by_seq[crash_receipt.seq]
    assert survivor.outcome == "candidate_uncaptured_at_crash"
    assert json.loads(survivor.facts_json)["retention_class"] == "protected_crash_evidence"


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
        "(command_id, authority_generation, subject_id, idempotency_key, payload_hash, kind, "
        "strategy_instance_id, run_id, action, intended_end_state, state, "
        "effect_operation_id, receipt_id, created_at_ms, updated_at_ms) "
        "VALUES ('command-1', 1, 'bot:spy-bot', 'command-key', 'hash', 'strategy_decision', "
        "'spy-bot', NULL, 'ENTER', NULL, 'accepted', NULL, NULL, 1, 1)"
    )
    repository._conn.execute(
        "INSERT INTO effect_operations "
        "(effect_operation_id, authority_generation, subject_id, idempotency_key, command_id, "
        "strategy_instance_id, run_id, kind, state, custody_owner, created_at_ms, updated_at_ms) "
        "VALUES ('effect-1', 1, 'bot:spy-bot', 'effect-key', 'command-1', 'spy-bot', NULL, "
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
