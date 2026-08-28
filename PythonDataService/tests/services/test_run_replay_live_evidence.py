"""Assembling one run's durable decision evidence for replay alignment."""

from __future__ import annotations

import json

from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.services.run_replay_proof import live_run_decision_evidence_from_rows


def _row(
    seq: int,
    *,
    outcome: str,
    run_id: str,
    evaluation_id: str,
    reason_code: str = "",
    trace_digest: str = "",
    bar_close_ms: int = 0,
) -> DecisionReceiptResource:
    return DecisionReceiptResource(
        strategy_instance_id="bot-a",
        seq=seq,
        outcome=outcome,
        symbol="SPY",
        intent_id=evaluation_id,
        order_ref=None,
        observed_at_ms=1_700_000_000_000 + seq,
        facts_json=json.dumps(
            {
                "run_id": run_id,
                "evaluation_id": evaluation_id,
                "reason_code": reason_code,
                "bar_ref": f"bar-{seq}",
                "trace_digest": trace_digest,
                "decision_bar_close_ms": bar_close_ms,
            }
        ),
    )


def test_live_run_decision_evidence_from_rows_filters_orders_and_classifies() -> None:
    rows = [
        _row(1, outcome="no_action", run_id="run-0", evaluation_id="e0", reason_code="NO_ACTION"),
        _row(2, outcome="candidate_uncaptured_at_crash", run_id="run-1", evaluation_id="e1",
             reason_code="CANDIDATE_UNCAPTURED_AT_CRASH"),
        _row(3, outcome="no_action", run_id="run-1", evaluation_id="e2", reason_code="NO_ACTION"),
        _row(4, outcome="blocked", run_id="run-1", evaluation_id="e3", reason_code="MARKET_CLOSED",
             trace_digest="a" * 64, bar_close_ms=1_700_000_900_000),
        _row(5, outcome="enter_intent", run_id="run-1", evaluation_id="e4"),
    ]

    evidence = live_run_decision_evidence_from_rows(rows, "run-1")

    assert [record.evaluation_id for record in evidence.records] == ["e2", "e3", "e4"]
    assert [record.evaluation_id for record in evidence.crash_records] == ["e1"]
    assert evidence.records[1].reason_code == "MARKET_CLOSED"
    assert evidence.records[1].trace_digest == "a" * 64
    assert evidence.records[1].bar_close_ms == 1_700_000_900_000
    assert evidence.records[0].trace_digest == ""  # legacy row: digest-less, disclosed not guessed
    assert evidence.records[0].bar_close_ms == 0
    assert evidence.captured_decisions == {
        "e0": "no_action",
        "e1": "candidate_uncaptured_at_crash",
        "e2": "no_action",
        "e3": "blocked",
        "e4": "enter_intent",
    }
    assert evidence.truncated is False


def test_live_run_decision_evidence_from_rows_flags_truncation_by_pruning_not_fullness() -> None:
    from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY

    # A full window that still starts at seq 1 is COMPLETE, not truncated:
    # nothing has been pruned yet (Codex PR #1767 — fullness must not deny parity).
    full_but_unpruned = [
        _row(seq, outcome="no_action", run_id="run-1", evaluation_id=f"e{seq}", reason_code="NO_ACTION")
        for seq in range(1, MAX_DECISION_RECEIPTS_PER_STRATEGY + 1)
    ]
    assert live_run_decision_evidence_from_rows(full_but_unpruned, "run-1").truncated is False

    # An earliest retained seq > 1 proves earlier rows were pruned -> truncated.
    pruned = [
        _row(seq, outcome="no_action", run_id="run-1", evaluation_id=f"e{seq}", reason_code="NO_ACTION")
        for seq in range(2, MAX_DECISION_RECEIPTS_PER_STRATEGY + 2)
    ]
    assert live_run_decision_evidence_from_rows(pruned, "run-1").truncated is True


def _quarantine_row(seq: int, *, run_id: str) -> DecisionReceiptResource:
    """A quarantine receipt exactly as `bot_decision_quarantine` writes it.

    Deliberately built by hand rather than borrowing ``_row``: the point of
    these tests is the two fields ``_row`` always supplies and this row never
    has -- ``intent_id`` and ``evaluation_id``.
    """
    return DecisionReceiptResource(
        strategy_instance_id="bot-a",
        seq=seq,
        outcome="decision_bar_quarantined",
        symbol="SPY",
        intent_id=None,
        order_ref=None,
        observed_at_ms=1_700_000_000_000 + seq,
        facts_json=json.dumps(
            {
                "run_id": run_id,
                "reason_code": "TIMEFRAME_MISMATCH",
                "bar_ref": "quarantined-bar:SPY:1700000000000-1700000060000",
                "retention_class": "protected_quarantine",
                "first_of_reason": True,
            }
        ),
    )


def test_a_quarantine_receipt_does_not_make_the_whole_run_unalignable() -> None:
    """Issue #1827's sharpest hazard, pinned.

    A row for this run carrying no evaluation identity raises
    ``RunReplayUnavailableError`` -- correctly, for a *decision*, since
    replay alignment is keyed on that identity. A quarantined bar is not a
    decision and legitimately has no identity, so reaching that guard would
    let one refused bar deny replay proof to the entire run.
    """
    rows = [
        _quarantine_row(1, run_id="run-1"),
        _row(2, outcome="no_action", run_id="run-1", evaluation_id="e1", reason_code="NO_ACTION"),
    ]

    evidence = live_run_decision_evidence_from_rows(rows, "run-1")

    assert [record.evaluation_id for record in evidence.records] == ["e1"]


def test_a_quarantine_receipt_is_neither_a_decision_nor_a_disposition() -> None:
    """It must not align, and it must not answer for any bucket.

    In ``records`` it would be a permanent unmatched divergence -- replaying
    the same bar refuses it again and produces no evaluation to match. In
    ``captured_decisions`` it would be offered to FR-016 warmup replay as
    some bucket's known outcome, which it never is.
    """
    rows = [_quarantine_row(1, run_id="run-1")]

    evidence = live_run_decision_evidence_from_rows(rows, "run-1")

    assert evidence.records == ()
    assert evidence.crash_records == ()
    assert evidence.captured_decisions == {}

