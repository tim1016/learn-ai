"""Durable experiment history must outlive Clerk retention and collector restarts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.schemas.paper_live_experiments import (
    ExperimentDecision,
    ExperimentEvidenceCapture,
    ExperimentLane,
    ExperimentSource,
    PaperLiveEvidencePair,
)
from app.services.paper_live_evidence_store import PaperLiveEvidenceStore

BAR = 1_790_171_100_000  # 2026-09-23 09:45 ET
DIGEST = "a" * 64


@pytest.fixture
def pair() -> PaperLiveEvidencePair:
    return PaperLiveEvidencePair(
        experiment_id="plx_" + "a" * 32,
        created_at_ms=BAR,
        paper=ExperimentSource(
            clerk_id="paper-clerk",
            account_id="paper-account",
            binding_generation=1,
            db_identity_token="paper-db",
            strategy_instance_id="paper-bot",
        ),
        live=ExperimentSource(
            clerk_id="live-clerk",
            account_id="live-account",
            binding_generation=2,
            db_identity_token="live-db",
            strategy_instance_id="live-bot",
        ),
    )


def receipt(seq: int = 1, *, lane: ExperimentLane = "paper", **changes: object) -> ExperimentDecision:
    fields = dict(
        seq=seq,
        run_id=f"{lane}-run",
        recorded_at_ms=BAR + seq,
        decision_bar_close_ms=BAR + (seq - 1) * 900_000,
        trace_digest=DIGEST,
        outcome="no_action",
        reason_code="NO_SIGNAL",
    )
    return ExperimentDecision.model_validate(fields | changes)


def capture(
    pair: PaperLiveEvidencePair,
    lane: ExperimentLane,
    *decisions: ExperimentDecision,
    at_ms: int = BAR + 100,
    highest_seq: int | None = None,
) -> ExperimentEvidenceCapture:
    return ExperimentEvidenceCapture(
        source=getattr(pair, lane),
        captured_at_ms=at_ms,
        decisions=decisions,
        highest_seq=highest_seq if highest_seq is not None else max((d.seq for d in decisions), default=0),
    )


def test_history_and_session_summary_survive_restart(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    store = PaperLiveEvidenceStore.open(control_dir=tmp_path)
    initial = store.create(pair)
    assert not initial.comparison.evidence_complete
    store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt()))
    expected = store.capture(pair.experiment_id, "live", capture(pair, "live", receipt(lane="live")))
    store.close()

    reopened = PaperLiveEvidenceStore.open(control_dir=tmp_path)
    try:
        actual = reopened.read(pair.experiment_id)
        assert actual == expected
        assert actual.comparison.all_decisions_match
        assert actual.comparison.sessions[0].paper_run_ids == ("paper-run",)
        assert actual.comparison.sessions[0].live_run_ids == ("live-run",)
    finally:
        reopened.close()


def test_collection_retains_rows_pruned_from_later_source_reads(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        store.create(pair)
        store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt()))
        report = store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt(2), at_ms=BAR + 200))
        assert [row.paper[0].seq for row in report.comparison.rows] == [1, 2]
        assert report.paper.missing_receipts == 0
        assert not report.comparison.evidence_complete  # Live has never been observed.


def test_missing_sequences_do_not_establish_equivalence(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        store.create(pair)
        for lane in ("paper", "live"):
            report = store.capture(pair.experiment_id, lane, capture(pair, lane, receipt(2, lane=lane)))
        assert report.paper.missing_receipts == report.live.missing_receipts == 1
        assert report.comparison.matching_decisions == 1
        assert not report.comparison.evidence_complete
        assert not report.comparison.all_decisions_match


def test_final_outcome_revisions_preserve_intent_and_refresh_summary(
    tmp_path: Path, pair: PaperLiveEvidencePair
) -> None:
    intent = receipt(outcome="enter_intent", reason_code="ENTER")
    entered = receipt(outcome="entered", reason_code="ENTERED", order_ref="order-1")
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        store.create(pair)
        store.capture(pair.experiment_id, "paper", capture(pair, "paper", intent))
        store.capture(pair.experiment_id, "live", capture(pair, "live", receipt(lane="live", outcome="entered")))
        report = store.capture(pair.experiment_id, "paper", capture(pair, "paper", entered, at_ms=BAR + 200))
        assert report.comparison.all_decisions_match
        assert report.comparison.execution_outcome_differences == 0
        assert [r.decision for r in store.revisions(pair.experiment_id, "paper", 1)] == [intent, entered]


def test_revised_trace_remains_a_conflict_even_if_later_restored(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        store.create(pair)
        store.capture(pair.experiment_id, "live", capture(pair, "live", receipt(lane="live")))
        for offset, digest in enumerate((DIGEST, "b" * 64, DIGEST), start=1):
            report = store.capture(
                pair.experiment_id,
                "paper",
                capture(
                    pair,
                    "paper",
                    receipt(trace_digest=digest),
                    at_ms=BAR + 100 * offset,
                ),
            )
        assert report.paper.conflicting_receipts == 1
        assert report.comparison.rows[0].status == "unverifiable"
        assert not report.comparison.all_decisions_match
        assert len(store.revisions(pair.experiment_id, "paper", 1)) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "different-account"),
        ("clerk_id", "different-clerk"),
        ("db_identity_token", "replacement-db"),
        ("binding_generation", 2),
        ("strategy_instance_id", "different-bot"),
    ],
)
def test_capture_rejects_changed_source_identity(
    tmp_path: Path,
    pair: PaperLiveEvidencePair,
    field: str,
    value: object,
) -> None:
    wrong = capture(pair, "paper", receipt())
    wrong = wrong.model_copy(update={"source": wrong.source.model_copy(update={field: value})})
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        original = store.create(pair)
        with pytest.raises(ValueError, match="source identity"):
            store.capture(pair.experiment_id, "paper", wrong)
        assert store.read(pair.experiment_id) == original


def test_idempotent_retry_cannot_overwrite_pair_or_capture(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        initial = store.create(pair)
        assert store.create(pair) == initial
        with pytest.raises(ValueError, match="already identifies"):
            store.create(pair.model_copy(update={"created_at_ms": BAR + 1}))
        observation = capture(pair, "paper", receipt())
        first = store.capture(pair.experiment_id, "paper", observation)
        assert store.capture(pair.experiment_id, "paper", observation) == first
        assert len(store.revisions(pair.experiment_id, "paper", 1)) == 1
        with pytest.raises(ValueError, match="same capture time"):
            store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt(outcome="blocked")))
        with pytest.raises(ValueError, match="older capture"):
            store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt(), at_ms=BAR + 99))
        with pytest.raises(ValueError, match="watermark moved backwards"):
            store.capture(pair.experiment_id, "paper", capture(pair, "paper", highest_seq=0, at_ms=BAR + 200))
        assert store.read(pair.experiment_id) == first


def test_concurrent_lane_collection_keeps_both_commits(tmp_path: Path, pair: PaperLiveEvidencePair) -> None:
    with (
        PaperLiveEvidenceStore.open(control_dir=tmp_path) as first,
        PaperLiveEvidenceStore.open(control_dir=tmp_path) as second,
    ):
        first.create(pair)
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [
                workers.submit(store.capture, pair.experiment_id, lane, capture(pair, lane, receipt(lane=lane)))
                for store, lane in ((first, "paper"), (second, "live"))
            ]
            for result in pending:
                result.result()
        report = first.read(pair.experiment_id)
        assert report.comparison.all_decisions_match
        assert report.paper.captured_at_ms == report.live.captured_at_ms == BAR + 100


def test_summary_failure_rolls_back_receipts_and_cursor(
    tmp_path: Path,
    pair: PaperLiveEvidencePair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.paper_live_evidence_store as module

    def fail_comparison(*args: object, **kwargs: object) -> None:
        raise RuntimeError("comparison failed")

    with PaperLiveEvidenceStore.open(control_dir=tmp_path) as store:
        initial = store.create(pair)
        monkeypatch.setattr(module, "compare_decisions", fail_comparison)
        with pytest.raises(RuntimeError, match="comparison failed"):
            store.capture(pair.experiment_id, "paper", capture(pair, "paper", receipt()))
        assert store.read(pair.experiment_id) == initial
        assert store.revisions(pair.experiment_id, "paper", 1) == ()
