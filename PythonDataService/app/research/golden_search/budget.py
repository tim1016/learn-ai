"""The conservative per-stage upper bound on a study's engine evaluations, and its serial time estimate.

Formula: with ``B`` one procedure's bound (``zoom.max_evaluations`` or
``grid_procedure.max_grid_evaluations``), ``S`` the searched-knob count,
``F`` the fold count, ``P`` the pair-audit count and ``R`` the stress
scenario count:
  search      ``B + P·5²`` — the all-period procedure, then each 5×5 pair
              audit around its winner
  recent      ``B`` when the recent window is fitted, else no stage
  validation  ``F · (B + 2)`` — each fold's procedure, its winner's test
              evaluation and the frozen incumbent's benchmark on that test window
  evidence    ``3 + 2·2·S`` (when the neighbor audit runs) ``+ 3·R``
              — a detail run per candidate (incumbent, all-period, recent),
              both neighbors of every searched knob for the two fitted
              candidates, every stress scenario per candidate
  exam        ``2`` — candidate and incumbent, RESERVED at lock
  proof       ``3`` — persisted run, lake replay, restored replay, RESERVED at lock
``total_max`` is the sum; a plan whose ``total_max`` exceeds its budget cap is
refused (``WORKLOAD_LIMIT``). Every engine evaluation in a study, proof
included, counts against the cap; cache hits and invalid points never do,
so the bound only overstates. Time: evaluations run one at a time
(``app.research.sweep.execution``), each costing Grid Search's measured
``ESTIMATE_FIXED_SECONDS + ESTIMATE_SECONDS_PER_MONTH · months`` for its
window (at least one month); ``high`` is the bound, ``low`` assumes 60% of
it runs (a cache-friendly guess). Both are estimates, labelled so.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Workload and progress".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_budget.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.research.golden_search.declarations import SearchDeclaration
from app.research.golden_search.grid_procedure import PAIR_GRID_SIZE, max_grid_evaluations
from app.research.golden_search.procedure_history import FoldWindow, fold_windows
from app.research.golden_search.protocol import (
    MAX_BUDGET_CAP,
    GoldenSearchProtocol,
    ProtocolRefusal,
    grid_size,
    recent_window_ms,
    validate_protocol,
)
from app.research.golden_search.zoom import max_evaluations
from app.research.grid_search.service import ESTIMATE_FIXED_SECONDS, ESTIMATE_SECONDS_PER_MONTH
from app.utils.session_anchors import et_date_at_ms

EXAM_EVALUATIONS = 2
PROOF_EVALUATIONS = 3
EVIDENCE_CANDIDATES = 3
NEIGHBOR_CANDIDATES = 2
# Per fold: the winner on the test window and the frozen incumbent benchmarked on it (#2696).
FOLD_TEST_EVALUATIONS = 2
CACHE_FRIENDLY_SHARE = 0.6
_DAYS_PER_MONTH = 30.4


@dataclass(frozen=True)
class StageEstimate:
    stage: str
    label: str
    max_evaluations: int

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "label": self.label, "max_evaluations": self.max_evaluations}


@dataclass(frozen=True)
class StudyEstimate:
    stages: tuple[StageEstimate, ...]
    total_max: int
    reserved_for_exam_and_proof: int
    serial_seconds_low: float
    serial_seconds_high: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "stages": [stage.as_dict() for stage in self.stages],
            "total_max": self.total_max,
            "reserved_for_exam_and_proof": self.reserved_for_exam_and_proof,
            "serial_seconds_low": self.serial_seconds_low,
            "serial_seconds_high": self.serial_seconds_high,
        }


def procedure_bound(protocol: GoldenSearchProtocol) -> int:
    """Upper bound on one search procedure's evaluations under the plan's method."""
    return max_evaluations(protocol) if protocol.method == "zoom" else max_grid_evaluations(protocol)


def _seconds_per_evaluation(months: float) -> float:
    return ESTIMATE_FIXED_SECONDS + ESTIMATE_SECONDS_PER_MONTH * max(1.0, months)


def _months(start_ms: int, end_ms: int) -> float:
    """Calendar months between two ET midnights, counted in ET dates so a DST hour never leaks in."""
    return (et_date_at_ms(end_ms) - et_date_at_ms(start_ms)).days / _DAYS_PER_MONTH


def estimate(protocol: GoldenSearchProtocol, *, folds: int) -> StudyEstimate:
    """Every stage's upper bound and the serial time range for a plan with ``folds`` validation folds."""
    bound = procedure_bound(protocol)
    searched = len(protocol.search_knobs)
    development_months = _months(protocol.development_start_ms, protocol.development_end_ms)
    final_months = _months(protocol.final_start_ms, protocol.final_end_ms)
    search = bound + len(protocol.pair_audits) * PAIR_GRID_SIZE**2
    evidence = (
        EVIDENCE_CANDIDATES
        + (NEIGHBOR_CANDIDATES * 2 * searched if protocol.neighbor_audit else 0)
        + EVIDENCE_CANDIDATES * len(protocol.stress)
    )
    # (stage, label, bound, seconds for the stage at its windows' lengths)
    rows: list[tuple[str, str, int, float]] = [
        ("search", "Search the development period", search, search * _seconds_per_evaluation(development_months)),
    ]
    if protocol.recent_window:
        recent_start, recent_end = recent_window_ms(protocol)
        rows.append(("recent", "Fit the recent window", bound, bound * _seconds_per_evaluation(_months(recent_start, recent_end))))
    rows.extend(
        [
            (
                "validation",
                "Test the procedure over time",
                folds * (bound + FOLD_TEST_EVALUATIONS),
                folds
                * (
                    bound * _seconds_per_evaluation(protocol.training_months)
                    + FOLD_TEST_EVALUATIONS * _seconds_per_evaluation(protocol.test_months)
                ),
            ),
            ("evidence", "Gather candidate evidence", evidence, evidence * _seconds_per_evaluation(development_months)),
            ("exam", "Final test (reserved)", EXAM_EVALUATIONS, EXAM_EVALUATIONS * _seconds_per_evaluation(final_months)),
            ("proof", "Qualification proof (reserved)", PROOF_EVALUATIONS, PROOF_EVALUATIONS * _seconds_per_evaluation(final_months)),
        ]
    )
    high = sum(seconds for *_, seconds in rows)
    return StudyEstimate(
        stages=tuple(StageEstimate(stage=stage, label=label, max_evaluations=count) for stage, label, count, _ in rows),
        total_max=sum(count for _, _, count, _ in rows),
        reserved_for_exam_and_proof=EXAM_EVALUATIONS + PROOF_EVALUATIONS,
        serial_seconds_low=round(high * CACHE_FRIENDLY_SHARE, 1),
        serial_seconds_high=round(high, 1),
    )


def workload_refusal(protocol: GoldenSearchProtocol, study_estimate: StudyEstimate) -> ProtocolRefusal | None:
    """``WORKLOAD_LIMIT`` when the plan's upper bound exceeds its budget cap."""
    if study_estimate.total_max <= protocol.budget_cap:
        return None
    return ProtocolRefusal(
        code="WORKLOAD_LIMIT",
        field="budget_cap",
        message=(
            f"This plan could need up to {study_estimate.total_max} evaluations, more than its budget of "
            f"{protocol.budget_cap}. Search fewer knobs, use fewer Zoom points or passes, or raise the budget "
            f"(at most {MAX_BUDGET_CAP})."
        ),
    )


@dataclass(frozen=True)
class ProtocolReview:
    """Everything a preflight shows: refusals (data, never an error), the folds and the estimate when sizable."""

    refusals: tuple[ProtocolRefusal, ...]
    folds: tuple[FoldWindow, ...]
    estimate: StudyEstimate | None

    @property
    def lockable(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict[str, Any]:
        return {
            "refusals": [refusal.as_dict() for refusal in self.refusals],
            "folds": [fold.as_dict() for fold in self.folds],
            "estimate": None if self.estimate is None else self.estimate.as_dict(),
        }


def review_protocol(protocol: GoldenSearchProtocol, declaration: SearchDeclaration) -> ProtocolReview:
    """Validate the plan, plan its folds and size it; the workload ceiling is checked last."""
    refusals = list(validate_protocol(protocol, declaration))
    codes = {refusal.code for refusal in refusals}
    folds: tuple[FoldWindow, ...] = ()
    if not codes & {"INTERVALS_INVALID", "FOLDS_INVALID"}:
        folds = tuple(fold_windows(protocol))
    sizable = bool(folds) and (
        protocol.method == "zoom" or (protocol.method == "grid" and grid_size(protocol) is not None)
    )
    study_estimate = estimate(protocol, folds=len(folds)) if sizable else None
    if study_estimate is not None and "BUDGET_CAP_INVALID" not in codes:
        refusal = workload_refusal(protocol, study_estimate)
        if refusal is not None:
            refusals.append(refusal)
    return ProtocolReview(refusals=tuple(refusals), folds=folds, estimate=study_estimate)
