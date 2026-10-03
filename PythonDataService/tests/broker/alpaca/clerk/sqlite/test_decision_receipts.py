"""Decision receipt storage tests for the SQLite Clerk authority."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.decision_receipts as decision_receipts_module
from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    CRASH_CANDIDATE_OUTCOME,
    MAX_DECISION_RECEIPT_READ,
    MAX_DECISION_RECEIPTS_PER_STRATEGY,
    QUARANTINE_OUTCOME,
    DecisionOutcome,
    DecisionReceiptConflictError,
    SqliteDecisionReceipts,
    rotated_out_before_ms,
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


def test_retained_window_reads_the_whole_retention_not_the_presentation_cap(
    receipts: SqliteDecisionReceipts,
) -> None:
    """FR-016 replay (``captured_decision_outcomes``) must see every ordinary
    receipt still retained. A one-minute program writes more receipts over
    its warmup than ``MAX_DECISION_RECEIPT_READ`` returns; read through the
    presentation cap and the earliest buckets look uncaptured (#1740)."""
    for index in range(MAX_DECISION_RECEIPT_READ + 2):
        receipts.append(
            outcome="no_action",
            symbol="SPY",
            observed_at_ms=index,
            intent_id=f"{index}:NO_ACTION",
            facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
        )

    window = receipts.retained_window()

    assert len(window) == MAX_DECISION_RECEIPT_READ + 2
    assert window[0].seq == 1
    assert window[-1].seq == MAX_DECISION_RECEIPT_READ + 2
    assert len(receipts.tail(MAX_DECISION_RECEIPT_READ + 10)) == MAX_DECISION_RECEIPT_READ


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


def test_protected_effect_history_survives_retention_pruning(
    repository: ClerkSqliteRepository, receipts: SqliteDecisionReceipts, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.broker.alpaca.clerk.sqlite.decision_receipts as module

    monkeypatch.setattr(module, "MAX_DECISION_RECEIPTS_PER_STRATEGY", 3)
    for index in range(6):
        receipts.append(outcome="no_action", symbol="SPY", observed_at_ms=index, facts={
            "bar_ref": f"SPY@{index}", "retention_class": "protected_effect" if index == 0 else "ordinary",
        })
    retained = repository.decision_receipt_tail(strategy_instance_id="spy-bot", limit=500)
    assert [r.seq for r in retained] == [1, 4, 5, 6]


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


# ---------------------------------------------------------------------------
# How far back the retained window reaches once it has dropped a receipt (#2850).
# ---------------------------------------------------------------------------

_SHORT_WINDOW = 3
_MINUTE_MS = 60_000
_FIRST_CLOSE_MS = 1_790_000_060_000


@pytest.fixture()
def short_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep and read three receipts, so a fourth drops the first."""
    monkeypatch.setattr(decision_receipts_module, "MAX_DECISION_RECEIPTS_PER_STRATEGY", _SHORT_WINDOW)


def _close_ms(minute: int) -> int:
    return _FIRST_CLOSE_MS + minute * _MINUTE_MS


def _receipt_bucket(
    receipts: SqliteDecisionReceipts,
    minute: int,
    *,
    outcome: DecisionOutcome = "no_action",
    records_close: bool = True,
) -> None:
    """Receipt the bucket that closed at ``minute``, the way a runner or the Clerk does.

    A quarantine row names no evaluation. Every row's wall-clock
    ``observed_at_ms`` is far from any bar close, so a boundary read from it
    would be plainly wrong.
    """
    facts: dict[str, str | int] = {"bar_ref": f"SPY@{minute}"}
    if records_close:
        facts["decision_bar_close_ms"] = _close_ms(minute)
    receipts.append(
        outcome=outcome,
        symbol="SPY",
        observed_at_ms=9_000_000_000_000 + minute,
        intent_id=None if outcome == QUARANTINE_OUTCOME else f"evaluation-{minute}",
        facts=facts,
    )


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_is_the_oldest_retained_decision_s_bar_close(
    receipts: SqliteDecisionReceipts,
) -> None:
    """A window that still opens at sequence 1 has dropped nothing, so a bucket
    missing from it was never receipted and no boundary is named. Once the
    prune drops a receipt, a bucket that closed before the oldest decision
    still retained may have lost its own: the boundary is that decision's bar
    close, not the later wall clock its receipt was written at."""
    for minute in range(_SHORT_WINDOW):
        _receipt_bucket(receipts, minute)
    assert rotated_out_before_ms(receipts.retained_window()) is None

    _receipt_bucket(receipts, _SHORT_WINDOW)

    window = receipts.retained_window()
    assert [receipt.seq for receipt in window] == [2, 3, 4]
    assert rotated_out_before_ms(window) == _close_ms(1)


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_passes_over_rows_not_written_when_their_bucket_was_decided(
    receipts: SqliteDecisionReceipts,
) -> None:
    """Crash evidence is written by a later Resume for an older bucket, and a
    quarantine row names no decision. Neither says how far back the retained
    decisions reach: reading the crash evidence's own (older) bar close as
    the boundary would leave every rotated-out bucket after it flaggable."""
    _receipt_bucket(receipts, 5)
    _receipt_bucket(receipts, 2, outcome=CRASH_CANDIDATE_OUTCOME)
    _receipt_bucket(receipts, 6, outcome=QUARANTINE_OUTCOME, records_close=False)
    _receipt_bucket(receipts, 7)

    window = receipts.retained_window()
    assert [receipt.outcome for receipt in window] == [CRASH_CANDIDATE_OUTCOME, QUARANTINE_OUTCOME, "no_action"]
    assert rotated_out_before_ms(window) == _close_ms(7)


@pytest.mark.usefixtures("short_window")
def test_the_rotation_boundary_passes_over_a_decision_that_records_no_bar_close(
    receipts: SqliteDecisionReceipts,
) -> None:
    """A decided row with no close does not switch the boundary off, which
    would fail the Resume the way it failed before #2850. The next decision
    that records its close is read instead. Every decision in the window was
    receipted before the crash a Resume recovers from, so the bucket that
    crash left staged closed after that one too."""
    _receipt_bucket(receipts, 0)
    _receipt_bucket(receipts, 1, records_close=False)
    _receipt_bucket(receipts, 2)
    _receipt_bucket(receipts, 3)

    assert rotated_out_before_ms(receipts.retained_window()) == _close_ms(2)


@pytest.mark.usefixtures("short_window")
def test_a_rotated_window_that_records_no_bar_close_names_no_boundary_and_says_so(
    receipts: SqliteDecisionReceipts, caplog: pytest.LogCaptureFixture
) -> None:
    """The boundary is never guessed from when a receipt was written: a bucket
    decided late closed before that wall clock, so a real crash candidate
    could fall under it and go unflagged. With no close to read nothing is
    exempt, and the log says why the Resume that follows may fail."""
    for minute in range(_SHORT_WINDOW + 1):
        _receipt_bucket(receipts, minute, records_close=False)

    with caplog.at_level(logging.WARNING, logger=decision_receipts_module.__name__):
        boundary = rotated_out_before_ms(receipts.retained_window())

    assert boundary is None
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "action", None) == "decision_receipt_rotation_boundary_unknown"
    ]
    assert record.strategy_instance_id == "spy-bot"
    assert (record.first_retained_seq, record.last_retained_seq) == (2, 4)


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
