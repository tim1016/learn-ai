"""A twin experiment compares durable decision content without inventing alignment (#2371)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.paper_live_experiments import ExperimentDecision
from app.services.paper_live_comparison import compare_decisions
from app.utils.session_anchors import MAX_TIMESTAMP_MS

BAR = 1_790_171_100_000  # 2026-09-23 09:45 ET
DIGEST = "a" * 64


def decision(
    seq: int, *, bar_ms: int | None = BAR, digest: str | None = DIGEST, **kwargs: object
) -> ExperimentDecision:
    return ExperimentDecision(
        seq=seq,
        run_id="run-1",
        recorded_at_ms=BAR + seq,
        decision_bar_close_ms=bar_ms,
        trace_digest=digest,
        outcome="no_action",
        reason_code="NO_SIGNAL",
        **kwargs,
    )


def test_alignment_uses_bar_close_not_receipt_arrival_or_sequence() -> None:
    paper = decision(8)
    live = decision(13).model_copy(update={"recorded_at_ms": BAR + 12_000})
    result = compare_decisions([paper], [live], evidence_complete=True)
    assert result.all_decisions_match
    assert result.rows[0].status == "same"
    assert result.rows[0].paper == (paper,)
    assert result.rows[0].live == (live,)
    assert result.matching_decisions == 1


def test_unmatched_startup_and_later_rows_remain_visible() -> None:
    previous_close = 1_790_107_200_000  # 2026-09-22 16:00 ET startup receipt.
    result = compare_decisions(
        [decision(1, bar_ms=previous_close), decision(2)],
        [decision(1), decision(2, bar_ms=BAR + 900_000)],
        evidence_complete=True,
    )
    assert [row.status for row in result.rows] == ["paper_only", "same", "live_only"]
    assert result.unmatched_decisions == 2
    assert not result.all_decisions_match
    assert result.first_difference_at_ms == previous_close


@pytest.mark.parametrize("paper_digest,live_digest", [(None, None), (DIGEST, None), (None, DIGEST)])
def test_missing_digests_are_not_equivalence(paper_digest: str | None, live_digest: str | None) -> None:
    result = compare_decisions(
        [decision(1, digest=paper_digest)],
        [decision(2, digest=live_digest)],
        evidence_complete=True,
    )
    assert result.rows[0].status == "unverifiable"
    assert result.unverifiable_decisions == 1
    assert not result.all_decisions_match


def test_different_trace_is_a_decision_divergence_even_with_the_same_outcome() -> None:
    result = compare_decisions([decision(1)], [decision(1, digest="b" * 64)], evidence_complete=True)
    assert result.rows[0].status == "different"
    assert result.divergent_decisions == 1
    assert result.first_difference_at_ms == BAR


def test_execution_outcome_difference_does_not_rewrite_the_decision_trace_verdict() -> None:
    paper = decision(1).model_copy(update={"outcome": "entered", "reason_code": "ENTERED"})
    live = decision(1).model_copy(update={"outcome": "blocked", "reason_code": "LIVE_NOT_ARMED"})
    result = compare_decisions([paper], [live], evidence_complete=True)
    assert result.rows[0].status == "same"
    assert result.rows[0].outcomes_match is False
    assert result.rows[0].reasons_match is False
    assert result.execution_outcome_differences == 1


def test_empty_or_partial_evidence_never_claims_all_decisions_match() -> None:
    assert not compare_decisions([], [], evidence_complete=True).all_decisions_match
    partial = compare_decisions([decision(1)], [decision(1)], evidence_complete=False)
    assert partial.matching_decisions == 1
    assert not partial.all_decisions_match
    assert not partial.evidence_complete


def test_a_duplicate_bar_is_unverifiable_and_keeps_both_source_records() -> None:
    result = compare_decisions([decision(1), decision(2)], [decision(1)], evidence_complete=True)
    assert result.rows[0].status == "unverifiable"
    assert result.rows[0].detail == "More than one decision receipt names this bar on a lane."
    assert len(result.rows[0].paper) == 2
    assert result.matching_decisions == 0


def test_a_missing_decision_clock_is_not_replaced_by_the_receipt_clock() -> None:
    result = compare_decisions([decision(1, bar_ms=None)], [decision(1, bar_ms=None)], evidence_complete=True)
    assert len(result.rows) == 2
    assert all(row.decision_bar_close_ms is None for row in result.rows)
    assert all(row.status == "unverifiable" for row in result.rows)
    assert not result.all_decisions_match


def test_session_summaries_keep_different_sessions_separate() -> None:
    next_day = BAR + 86_400_000
    result = compare_decisions(
        [decision(1), decision(2, bar_ms=next_day)],
        [decision(1), decision(2, bar_ms=next_day, digest="b" * 64)],
        evidence_complete=True,
    )
    assert len(result.sessions) == 2
    assert [s.matching_decisions for s in result.sessions] == [1, 0]
    assert [s.divergent_decisions for s in result.sessions] == [0, 1]
    assert all(isinstance(s.session_open_ms, int) for s in result.sessions)


def test_twenty_six_matching_no_action_bars_prove_only_trace_equivalence() -> None:
    paper = [decision(seq, bar_ms=BAR + (seq - 1) * 900_000) for seq in range(1, 27)]
    live = [item.model_copy(update={"run_id": "live-run"}) for item in paper]
    result = compare_decisions(paper, live, evidence_complete=True)
    assert result.matching_decisions == 26
    assert result.divergent_decisions == result.unmatched_decisions == result.unverifiable_decisions == 0
    assert result.execution_outcome_differences == 0
    assert result.all_decisions_match
    assert result.sessions[0].session_open_ms == BAR - 900_000
    assert result.sessions[0].paper_run_ids == ("run-1",)
    assert result.sessions[0].live_run_ids == ("live-run",)
    assert all(row.paper[0].outcome == row.live[0].outcome == "no_action" for row in result.rows)


def test_missing_run_identity_cannot_establish_equivalence() -> None:
    result = compare_decisions([decision(1).model_copy(update={"run_id": None})], [decision(1)], evidence_complete=True)
    assert result.rows[0].status == "unverifiable"
    assert not result.all_decisions_match


@pytest.mark.parametrize(
    "bar_ms",
    [
        1_790_430_300_000,  # Saturday 2026-09-26: no scheduled session.
        1_795_802_400_000,  # 2026-11-27 13:00 ET: early close, valid final bar.
        1_795_803_300_000,  # 2026-11-27 13:15 ET: after the early close.
    ],
)
def test_session_membership_uses_the_canonical_calendar(bar_ms: int) -> None:
    result = compare_decisions([decision(1, bar_ms=bar_ms)], [decision(1, bar_ms=bar_ms)], evidence_complete=True)
    if bar_ms == 1_795_802_400_000:
        assert result.all_decisions_match
        assert result.sessions[0].session_open_ms == 1_795_789_800_000
    else:
        assert result.rows[0].status == "unverifiable"
        assert result.sessions == ()
        assert not result.all_decisions_match


@pytest.mark.parametrize("field", ["recorded_at_ms", "decision_bar_close_ms", "seq"])
@pytest.mark.parametrize("value", [True, "1790172900000", 1790172900000.0])
def test_evidence_requires_integer_clocks_and_sequence_numbers(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ExperimentDecision.model_validate(decision(1).model_dump() | {field: value})


def test_missing_clock_row_keys_are_stable_when_capture_order_changes() -> None:
    first, second = decision(1, bar_ms=None), decision(2, bar_ms=None)
    forward = compare_decisions([first, second], [], evidence_complete=True)
    reversed_capture = compare_decisions([second, first], [], evidence_complete=True)
    assert forward == reversed_capture


def test_out_of_calendar_source_clock_is_preserved_without_inventing_a_session() -> None:
    source = decision(1, bar_ms=MAX_TIMESTAMP_MS)
    result = compare_decisions([source], [source], evidence_complete=True)
    assert result.rows[0].paper == (source,)
    assert result.rows[0].status == "unverifiable"
    assert result.sessions == ()
