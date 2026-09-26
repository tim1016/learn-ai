"""Read-only comparison of Paper/Live decision receipts (#2371).

Formula: exact trace-digest equality on a full outer join by bar-close ms;
  each uniquely joined bar belongs to one disjoint comparison category.
Reference: https://github.com/tim1016/learn-ai/issues/2371, requirements 5–7.
Canonical implementation: this module. No order, custody, or arming authority.
Validated against: tests/services/test_paper_live_comparison.py (exact identities
  and integer counts; no floating-point tolerance applies).

Receipt outcomes are execution evidence. Different fills or an arming refusal
can follow identical decisions, so outcome differences remain explicit alongside
the trace comparison and do not overwrite its verdict. Missing digests, missing
clocks, duplicate bars, and missing history can never establish equivalence.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.paper_live_experiments import (
    DecisionComparisonCounts,
    DecisionComparisonRow,
    ExperimentDecision,
    ExperimentLane,
    ExperimentSessionComparison,
    PaperLiveComparison,
)
from app.utils.session_anchors import et_date_at_ms


def _compare_bar(
    key: str,
    bar_ms: int | None,
    paper: list[ExperimentDecision],
    live: list[ExperimentDecision],
    *,
    conflicting_receipts: frozenset[tuple[ExperimentLane, int]],
) -> DecisionComparisonRow:
    outcomes_match = reasons_match = None
    if bar_ms is None:
        status, detail = "unverifiable", "The source receipt has no decision-bar close time."
    elif _session_anchor(bar_ms) is None:
        status, detail = "unverifiable", "The decision-bar close is outside a scheduled regular session."
    elif any(
        (lane, receipt.seq) in conflicting_receipts
        for lane, receipts in (("paper", paper), ("live", live))
        for receipt in receipts
    ):
        status, detail = "unverifiable", "A source revised the identity or trace of an archived decision."
    elif len(paper) > 1 or len(live) > 1:
        status, detail = "unverifiable", "More than one decision receipt names this bar on a lane."
    elif not paper:
        status, detail = "live_only", "This decision bar is present only on Live."
    elif not live:
        status, detail = "paper_only", "This decision bar is present only on Paper."
    else:
        left, right = paper[0], live[0]
        outcomes_match = left.outcome == right.outcome
        reasons_match = left.reason_code == right.reason_code
        if not left.run_id or not right.run_id:
            status, detail = "unverifiable", "A source receipt has no run identity."
        elif left.trace_digest is None or right.trace_digest is None:
            status, detail = "unverifiable", "A source receipt has no decision trace digest."
        elif left.trace_digest == right.trace_digest:
            status, detail = "same", "The decision trace digests match exactly."
        else:
            status, detail = "different", "The decision trace digests differ."
    return DecisionComparisonRow(
        key=key,
        decision_bar_close_ms=bar_ms,
        status=status,
        detail=detail,
        paper=tuple(paper),
        live=tuple(live),
        outcomes_match=outcomes_match,
        reasons_match=reasons_match,
    )


def _session_anchor(bar_ms: int) -> int | None:
    try:
        window = session_window_for_date(et_date_at_ms(bar_ms))
    except (LookupError, ValueError, OverflowError):
        # Keep an out-of-calendar source clock as unverifiable evidence,
        # including instants outside the calendar library's supported range.
        return None
    return window.open_ms_utc if window.open_ms_utc < bar_ms <= window.close_ms_utc else None


def _counts(rows: Sequence[DecisionComparisonRow]) -> DecisionComparisonCounts:
    return DecisionComparisonCounts(
        matching_decisions=sum(row.status == "same" for row in rows),
        divergent_decisions=sum(row.status == "different" for row in rows),
        unmatched_decisions=sum(row.status in {"paper_only", "live_only"} for row in rows),
        unverifiable_decisions=sum(row.status == "unverifiable" for row in rows),
        execution_outcome_differences=sum(row.outcomes_match is False for row in rows),
    )


def compare_decisions(
    paper: Sequence[ExperimentDecision],
    live: Sequence[ExperimentDecision],
    *,
    evidence_complete: bool,
    conflicting_receipts: frozenset[tuple[ExperimentLane, int]] = frozenset(),
) -> PaperLiveComparison:
    """Compare captured evidence, keeping every unmatched or ambiguous receipt."""
    buckets: dict[str, tuple[list[ExperimentDecision], list[ExperimentDecision]]] = {}
    for lane, observations in enumerate((paper, live)):
        for observation in observations:
            clock = observation.decision_bar_close_ms
            # A missing clock has no join key. In particular, two missing
            # clocks must not match just because their receipt times do.
            key = str(clock) if clock is not None else f"unclocked:{lane}:{observation.seq}"
            buckets.setdefault(key, ([], []))[lane].append(observation)
    rows = [
        _compare_bar(
            key, (left or right)[0].decision_bar_close_ms, left, right, conflicting_receipts=conflicting_receipts
        )
        for key, (left, right) in buckets.items()
    ]
    rows.sort(
        key=lambda row: (
            row.decision_bar_close_ms
            if row.decision_bar_close_ms is not None
            else (row.paper or row.live)[0].recorded_at_ms,
            row.key,
        )
    )
    sessions: dict[int, list[DecisionComparisonRow]] = defaultdict(list)
    for row in rows:
        if row.decision_bar_close_ms is not None:
            anchor = _session_anchor(row.decision_bar_close_ms)
            if anchor is not None:
                sessions[anchor].append(row)
    counts = _counts(rows)
    return PaperLiveComparison(
        **counts.model_dump(),
        rows=tuple(rows),
        evidence_complete=evidence_complete,
        all_decisions_match=bool(rows) and evidence_complete and all(row.status == "same" for row in rows),
        first_difference_at_ms=next(
            (
                row.decision_bar_close_ms
                for row in rows
                if row.status != "same" and row.decision_bar_close_ms is not None
            ),
            None,
        ),
        sessions=tuple(
            ExperimentSessionComparison(
                session_open_ms=anchor,
                **_counts(day).model_dump(),
                paper_run_ids=tuple(sorted({item.run_id for row in day for item in row.paper if item.run_id})),
                live_run_ids=tuple(sorted({item.run_id for row in day for item in row.live if item.run_id})),
            )
            for anchor, day in sorted(sessions.items())
        ),
    )
