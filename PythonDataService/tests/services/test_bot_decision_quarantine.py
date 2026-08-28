"""A refused decision bar becomes durable evidence, not only a log line.

Issue #1827. ``SignalSession.advance`` can refuse a decision bar outright
(TIMEFRAME_MISMATCH, NON_MONOTONIC_DECISION_CLOCK, UNSETTLED_STAGE). #1733
made those refusals visible as a throttled structured warning; a bot that
consumes data and decides nothing is exactly the condition this repo answers
everywhere else with an operator-visible receipt, and a WARNING is not one.

These cover the journal in isolation. The same seam driven end to end through
``_build_signal_strategy`` lives in
``tests/engine/strategy/test_signal_program_decision_clock.py``, alongside
#1733's own tests for the throttled log.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.engine.strategy.signal_program import StageQuarantine, StageStatus
from app.services.bot_decision_quarantine import (
    QUARANTINE_OUTCOME,
    QuarantineJournal,
    QuarantineReceiptSink,
    quarantine_bar_ref,
)
from tests.services.test_candidate_uncaptured_at_crash import _binding

_BUCKET_START_MS = 1_711_641_540_000
_BUCKET_END_MS = 1_711_641_600_000
_EXPECTED_TIMEFRAME_MS = 15 * 60_000


class _RecordingReceipts:
    """The ``SqliteDecisionReceipts.append`` surface, captured."""

    def __init__(self) -> None:
        self.appended: list[dict] = []

    def append(self, **kwargs: object) -> object:
        self.appended.append(dict(kwargs))
        return object()


def _quarantine(reason: str, *, offset_ms: int = 0) -> StageQuarantine:
    return StageQuarantine(
        status=StageStatus.REJECTED_BAR,
        reason=reason,
        bar_start_ms=_BUCKET_START_MS + offset_ms,
        bar_end_ms=_BUCKET_END_MS + offset_ms,
    )


def _record(journal: QuarantineJournal, quarantine: StageQuarantine) -> None:
    journal.record(
        quarantine,
        binding=_binding(run_id="run-1"),
        expected_timeframe_ms=_EXPECTED_TIMEFRAME_MS,
    )


def test_a_refused_decision_bar_becomes_a_durable_receipt() -> None:
    receipts = _RecordingReceipts()

    _record(QuarantineJournal(receipts), _quarantine("TIMEFRAME_MISMATCH"))

    assert len(receipts.appended) == 1
    row = receipts.appended[0]
    assert row["outcome"] == QUARANTINE_OUTCOME
    facts = row["facts"]
    assert facts["reason_code"] == "TIMEFRAME_MISMATCH"
    assert facts["run_id"] == "run-1"
    assert facts["bar_start_ms"] == _BUCKET_START_MS
    assert facts["bar_end_ms"] == _BUCKET_END_MS
    # Protected: this row is the explanation for a run that decided nothing,
    # so tail compaction must not be able to drop it.
    assert str(facts["retention_class"]).startswith("protected_")


def test_the_receipt_carries_no_evaluation_identity() -> None:
    """A quarantined bucket never became an evaluation, so it has none to claim.

    Inventing one would make the row look like a decision to every consumer
    keyed on ``evaluation_id`` -- above all ``run_replay_proof``, which would
    then hunt for a replayed bucket that cannot exist.
    """
    receipts = _RecordingReceipts()

    _record(QuarantineJournal(receipts), _quarantine("UNSETTLED_STAGE"))

    facts = receipts.appended[0]["facts"]
    assert "evaluation_id" not in facts
    assert "decision_id" not in facts
    assert receipts.appended[0].get("intent_id") is None


def test_a_systematically_refused_feed_writes_one_receipt_not_one_per_bar() -> None:
    """The bound that keeps this from evicting the decisions it sits beside.

    A mis-shaped decision clock refuses *every* bar. Decision receipts are
    pruned oldest-first against a per-instance cap, so a receipt per refused
    bar would push out the very rows FR-016 crash replay must still see --
    turning a diagnostic into data loss.
    """
    receipts = _RecordingReceipts()
    journal = QuarantineJournal(receipts)

    for bar in range(500):
        _record(journal, _quarantine("TIMEFRAME_MISMATCH", offset_ms=bar * 60_000))

    assert len(receipts.appended) == 1
    assert receipts.appended[0]["facts"]["first_of_reason"] is True


def test_each_distinct_reason_earns_its_own_receipt() -> None:
    receipts = _RecordingReceipts()
    journal = QuarantineJournal(receipts)

    _record(journal, _quarantine("TIMEFRAME_MISMATCH"))
    _record(journal, _quarantine("NON_MONOTONIC_DECISION_CLOCK"))
    _record(journal, _quarantine("TIMEFRAME_MISMATCH", offset_ms=60_000))

    assert [row["facts"]["reason_code"] for row in receipts.appended] == [
        "TIMEFRAME_MISMATCH",
        "NON_MONOTONIC_DECISION_CLOCK",
    ]


@pytest.mark.parametrize(
    ("reason", "expects_clock_pair"),
    [
        ("TIMEFRAME_MISMATCH", True),
        ("NON_MONOTONIC_DECISION_CLOCK", False),
        ("UNSETTLED_STAGE", False),
    ],
)
def test_only_a_width_refusal_reports_the_decision_clock_pair(
    reason: str, expects_clock_pair: bool
) -> None:
    """The other two refuse a bucket of exactly the right width."""
    receipts = _RecordingReceipts()

    _record(QuarantineJournal(receipts), _quarantine(reason))

    facts = receipts.appended[0]["facts"]
    assert ("expected_timeframe_ms" in facts) is expects_clock_pair
    assert ("observed_timeframe_ms" in facts) is expects_clock_pair
    if expects_clock_pair:
        assert facts["expected_timeframe_ms"] == _EXPECTED_TIMEFRAME_MS
        assert facts["observed_timeframe_ms"] == _BUCKET_END_MS - _BUCKET_START_MS


def test_a_journal_with_no_sink_still_counts_and_logs() -> None:
    """The read-only replay and qualification callers pass no sink.

    They re-drive bars a live run already judged, so a receipt from them
    would be a second record of one event. They must still not crash.
    """
    journal = QuarantineJournal()

    _record(journal, _quarantine("TIMEFRAME_MISMATCH"))
    _record(journal, _quarantine("TIMEFRAME_MISMATCH", offset_ms=60_000))


def test_the_bar_ref_names_the_refused_bucket() -> None:
    binding = _binding(run_id="run-1")

    ref = quarantine_bar_ref(binding, _quarantine("TIMEFRAME_MISMATCH"))

    assert str(_BUCKET_START_MS) in ref and str(_BUCKET_END_MS) in ref
    assert binding.symbol in ref


def test_the_sink_protocol_still_matches_the_journal_it_stands_for() -> None:
    """``QuarantineReceiptSink`` is structural, so nothing enforces it at runtime.

    The Protocol exists to keep this service module off the Clerk's concrete
    storage class, but a structural type that silently stops describing its
    one real implementer is worse than no type: the sink would be typed
    against a signature nothing satisfies, and only a live run would notice.
    """
    import inspect

    real = inspect.signature(SqliteDecisionReceipts.append)
    protocol = inspect.signature(QuarantineReceiptSink.append)

    assert list(real.parameters) == list(protocol.parameters)
    for name, expected in real.parameters.items():
        if name == "self":
            continue
        assert protocol.parameters[name].annotation == expected.annotation, (
            f"`{name}` drifted: SqliteDecisionReceipts.append declares "
            f"{expected.annotation!r}, the Protocol declares "
            f"{protocol.parameters[name].annotation!r}"
        )

