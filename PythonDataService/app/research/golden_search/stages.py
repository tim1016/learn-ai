"""Golden Search stage runners: search, validation (with evidence), exam and qualification (#2696, ADR 0074).

Each runner executes on a job worker thread under one claimed attempt and
writes only through the attempt fence, so a superseded worker can neither
record an evaluation nor move the study. Results are written after every
procedure and fold, so a cancelled or crashed stage keeps what it scored;
Finish re-runs the stage from scratch and every recorded evaluation comes
back from the cache, which makes the replay identical and free.

Leakage rules the runners enforce, beside the evaluator's capability: every
fold searches its own training window from the original ranges and the
protocol seed (never from the all-period winner); the exam scores only the
locked candidate and the frozen incumbent; nothing before the exam may
read the final interval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.jobs.progress import JobCancelled
from app.research.golden_search import repository as repo
from app.research.golden_search.budget import EXAM_EVALUATIONS, PROOF_EVALUATIONS
from app.research.golden_search.declarations import SearchDeclaration, declaration_for, knob_values, point_hash
from app.research.golden_search.evaluator import (
    EvaluationCapability,
    ExecuteBacktest,
    StudyEvaluator,
    Window,
    engine_executor,
)
from app.research.golden_search.evidence import (
    CandidateEvidence,
    CandidateKey,
    Neighborhood,
    StressResult,
    neighborhood,
    pair_map,
    recommendation,
)
from app.research.golden_search.exam_rules import judge_exam
from app.research.golden_search.grid_procedure import neighbor_probes, pair_grid, run_grid
from app.research.golden_search.guidance import research_weakness
from app.research.golden_search.models import STAGE_ESTIMATES, STAGE_STATES, StageName, StudyRow
from app.research.golden_search.procedure_history import (
    FoldWindow,
    fold_evidence,
    fold_return,
    fold_windows,
    link_fold_returns,
)
from app.research.golden_search.proof import ProofWindow
from app.research.golden_search.protocol import GoldenSearchProtocol, recent_window_ms
from app.research.golden_search.selection import Metrics, ineligibility, objective_value
from app.research.golden_search.zoom import BudgetExhausted, ProcedureResult, run_zoom
from app.research.persistence import lifecycle
from app.research.persistence.db import run_sync, with_connection
from app.research.persistence.fence import StaleAttemptError, lock_current_attempt
from app.research.sweep.snapshot import DataSnapshot
from app.research.walk_forward_study.verdict import Verdict, compute_verdict
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: Development stages may never touch the exam and proof reservations; the exam may not touch the proof's.
GENERAL_RESERVE = EXAM_EVALUATIONS + PROOF_EVALUATIONS
EXAM_RESERVE = PROOF_EVALUATIONS
NO_ELIGIBLE_CANDIDATE = "No setting met your rules in this fold's training window."
FOLD_BUDGET_REACHED = "The evaluation budget ran out before this fold finished."


@dataclass(frozen=True)
class ApprovalBinding:
    """The approval workflow's interface (``approval.approve_study`` and its two types), injectable for tests."""

    request_type: Callable[..., Any]
    checkpoint_from_dict: Callable[[Mapping[str, Any] | None], Any]
    approve: Callable[..., Any]


def default_approval() -> ApprovalBinding:
    # Imported when a qualification runs: the approval workflow (#2696) loads the proof and Golden Validation stacks.
    from app.research.golden_search import approval

    return ApprovalBinding(
        request_type=approval.ApprovalRequest,
        checkpoint_from_dict=approval.ApprovalCheckpoint.from_dict,
        approve=approval.approve_study,
    )


@dataclass(frozen=True)
class StageOutcome:
    study_id: str
    stage: StageName
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {"study_id": self.study_id, "stage": self.stage, "state": self.state}


@dataclass
class StageContext:
    row: StudyRow
    attempt: int
    protocol: GoldenSearchProtocol
    declaration: SearchDeclaration
    execute: ExecuteBacktest
    roots: list[Path]
    cancel_check: Callable[[], object]
    on_phase: Callable[[str], None]
    on_progress: Callable[[int, int], None]
    on_log: Callable[[str], None]
    results: dict[str, Any] = field(default_factory=dict)

    @property
    def development(self) -> Window:
        return (self.protocol.development_start_ms, self.protocol.development_end_ms)

    @property
    def final(self) -> Window:
        return (self.protocol.final_start_ms, self.protocol.final_end_ms)

    def evaluator(self, *, allowed: Window, limit: int, total: int) -> StudyEvaluator:
        return StudyEvaluator(
            study_id=self.row.id,
            attempt=self.attempt,
            strategy_key=self.row.strategy_key,
            context_digest=self.row.receipt["context_digest"],
            run_up_sessions=int(self.row.receipt["run_up"]["run_up_sessions"]),
            capability=EvaluationCapability(allowed=(allowed,)),
            budget_limit=limit,
            execute=self.execute,
            cancel_check=self.cancel_check,
            on_evaluated=lambda done: self.on_progress(done, total),
        )

    def write(self, patch: Mapping[str, Any]) -> None:
        """Persist stage output under the fence; the study's invalid-point total follows from every stored procedure."""
        self.results.update(patch)
        run_sync(
            with_connection(
                repo.update_study_fenced,
                self.row.id,
                self.attempt,
                changes={"invalid_points": _invalid_total(self.results)},
                results_patch=dict(patch),
            )
        )

    def trials(self, trials: Sequence[Mapping[str, Any]]) -> None:
        if trials:
            run_sync(with_connection(repo.insert_trials_fenced, self.row.id, self.attempt, list(trials)))


def _invalid_total(results: Mapping[str, Any]) -> int:
    total = 0
    for key in ("search", "recent"):
        if key in results:
            total += int(results[key]["counts"]["invalid"])
    for fold in results.get("validation", {}).get("folds", []):
        total += int((fold.get("counts") or {}).get("invalid", 0))
    return total


def stage_total(row: StudyRow, stage: StageName) -> int:
    """The stage's conservative bound, from the estimate frozen at lock."""
    bounds = {item["stage"]: int(item["max_evaluations"]) for item in row.receipt["estimate"]["stages"]}
    return sum(bounds.get(name, 0) for name in STAGE_ESTIMATES[stage])


# ── Procedures ───────────────────────────────────────────────────────────


class _Counting:
    """Counts the points a procedure re-submits, to tell distinct evaluations from re-used ones."""

    def __init__(self, evaluate: Callable[[Sequence[dict[str, Any]]], list[Metrics]]) -> None:
        self._evaluate = evaluate
        self.submitted = 0

    def __call__(self, points: Sequence[dict[str, Any]]) -> list[Metrics]:
        results = self._evaluate(points)
        self.submitted += len(points)
        return results


def run_procedure(
    ctx: StageContext,
    evaluator: StudyEvaluator,
    *,
    window: Window,
    step: str,
    fold_index: int | None = None,
) -> tuple[ProcedureResult, dict[str, int]]:
    """The plan's method over one window from the protocol seed; records its path as trials."""
    counting = _Counting(lambda points: evaluator.evaluate(points, window=window, stage=step, fold_index=fold_index))
    run = run_zoom if ctx.protocol.method == "zoom" else run_grid
    result = run(declaration=ctx.declaration, protocol=ctx.protocol, seed=ctx.protocol.seed, evaluate=counting)
    scored = run_sync(
        with_connection(
            repo.count_procedure_evaluations,
            ctx.row.id,
            step=step,
            fold_index=fold_index,
            window_start_ms=window[0],
            window_end_ms=window[1],
        )
    )
    # Evaluated: distinct points the engine scored for this procedure (replay-stable, a cut-short batch included).
    # Cached: requests for a point this procedure had already scored.
    counts = {"evaluated": scored, "cached": counting.submitted - len(result.evaluated_hashes), "invalid": result.invalid_points}
    trials: list[dict[str, Any]] = [
        {"stage": step, "fold_index": fold_index, "kind": "round", "payload": round_.as_dict()} for round_ in result.rounds
    ]
    if result.invalid_points:
        trials.append({"stage": step, "fold_index": fold_index, "kind": "invalid", "payload": {"invalid_points": result.invalid_points}})
    trials.append(
        {
            "stage": step,
            "fold_index": fold_index,
            "kind": "stop",
            "payload": {"stop_reason": result.stop_reason, "winner_hash": result.winner_hash, "window": list(window), **counts},
        }
    )
    ctx.trials(trials)
    return result, counts


def _procedure_record(result: ProcedureResult, counts: Mapping[str, int], window: Window) -> dict[str, Any]:
    return {
        "window": {"start_ms": window[0], "end_ms": window[1]},
        "procedure": result.as_dict(),
        "counts": dict(counts),
        "incomplete": result.stop_reason == "budget",
    }


def _evaluate_one(evaluator: StudyEvaluator, point: Mapping[str, Any], **kwargs: Any) -> Metrics | None:
    """One point's metrics, or ``None`` when the budget cannot admit it."""
    try:
        (metrics,) = evaluator.evaluate([point], **kwargs)
    except BudgetExhausted:
        return None
    return metrics


# ── Search ───────────────────────────────────────────────────────────────


def _pair_maps(ctx: StageContext, evaluator: StudyEvaluator, center: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Each predeclared pair's landscape through the all-period winner, cell by cell so an empty budget keeps the rest."""
    maps: list[dict[str, Any]] = []
    exhausted = False
    for a, b in ctx.protocol.pair_audits:
        grid = pair_grid(ctx.declaration, ctx.protocol, center, a, b)
        metrics_by_hash: dict[str, Metrics] = {}
        for cell in grid.testable:
            if exhausted:
                break
            assert cell.point is not None and cell.point_hash is not None
            metrics = _evaluate_one(evaluator, cell.point, window=ctx.development, stage="pair_audit")
            if metrics is None:
                exhausted = True
            else:
                metrics_by_hash[cell.point_hash] = metrics
        maps.append(pair_map(grid, metrics_by_hash))
    return maps, exhausted


def run_search(ctx: StageContext) -> str:
    evaluator = ctx.evaluator(
        allowed=ctx.development, limit=ctx.row.budget_cap - GENERAL_RESERVE, total=stage_total(ctx.row, "search")
    )
    ctx.on_phase("search")
    result, counts = run_procedure(ctx, evaluator, window=ctx.development, step="search")
    search = _procedure_record(result, counts, ctx.development)
    ctx.write({"search": {**search, "pair_maps": [], "pair_maps_incomplete": False}})
    ctx.on_phase("pair_audits")
    maps, maps_incomplete = _pair_maps(ctx, evaluator, result.winner)
    ctx.write({"search": {**search, "pair_maps": maps, "pair_maps_incomplete": maps_incomplete}})
    if ctx.protocol.recent_window:
        ctx.on_phase("recent")
        recent_window = recent_window_ms(ctx.protocol)
        recent, recent_counts = run_procedure(ctx, evaluator, window=recent_window, step="recent")
        ctx.write({"recent": _procedure_record(recent, recent_counts, recent_window)})
    return "awaiting_validation"


# ── Validation and evidence ──────────────────────────────────────────────


def _fold_record(fold: FoldWindow) -> dict[str, Any]:
    return {
        **fold.as_dict(),
        "status": "pending",
        "winner": None,
        "winner_hash": None,
        "train_metrics": None,
        "test_metrics": None,
        "incumbent_test_metrics": None,
        "failure_reason": None,
        "failure_code": None,
        "counts": None,
        "stop_reason": None,
    }


def _metrics_dict(metrics: Metrics | None) -> dict[str, Any] | None:
    return None if metrics is None else metrics.as_dict()


def _run_fold(ctx: StageContext, evaluator: StudyEvaluator, fold: FoldWindow) -> tuple[dict[str, Any], Metrics | None, Metrics | None]:
    """One fold: search its training window from the seed, test the winner and the incumbent once on its test window."""
    train: Window = (fold.train_start_ms, fold.train_end_ms)
    test: Window = (fold.test_start_ms, fold.test_end_ms)
    result, counts = run_procedure(ctx, evaluator, window=train, step="validation", fold_index=fold.fold_index)
    record = {**_fold_record(fold), "counts": counts, "stop_reason": result.stop_reason}
    eligible = result.winner_metrics is not None and ineligibility(result.winner_metrics, ctx.protocol.policy) is None
    train_metrics: Metrics | None = None
    test_metrics: Metrics | None = None
    if result.stop_reason == "budget":
        record.update(status="failed", failure_code="BUDGET", failure_reason=FOLD_BUDGET_REACHED)
    elif not eligible:
        record.update(status="failed", failure_code="NO_ELIGIBLE_CANDIDATE", failure_reason=NO_ELIGIBLE_CANDIDATE)
    else:
        train_metrics = result.winner_metrics
        record.update(winner=result.winner, winner_hash=result.winner_hash, train_metrics=_metrics_dict(train_metrics))
        test_metrics = _evaluate_one(evaluator, result.winner, window=test, stage="validation", fold_index=fold.fold_index)
        if test_metrics is None:
            record.update(status="failed", failure_code="BUDGET", failure_reason=FOLD_BUDGET_REACHED)
        elif test_metrics.status != "completed":
            record.update(
                status="failed",
                failure_code="TEST_RUN_FAILED",
                failure_reason=f"The winner's test run failed: {test_metrics.error or 'no reason recorded'}.",
                test_metrics=_metrics_dict(test_metrics),
            )
        else:
            record.update(status="completed", test_metrics=_metrics_dict(test_metrics))
    incumbent = _evaluate_one(evaluator, ctx.protocol.incumbent.params, window=test, stage="validation", fold_index=fold.fold_index)
    record["incumbent_test_metrics"] = _metrics_dict(incumbent)
    ctx.trials(
        [
            {
                "stage": "validation",
                "fold_index": fold.fold_index,
                "kind": "selection",
                "payload": {"winner_hash": record["winner_hash"], "status": record["status"], "failure_code": record["failure_code"]},
            }
        ]
    )
    return record, (train_metrics if record["status"] == "completed" else None), (test_metrics if record["status"] == "completed" else None)


def _linked(folds: Sequence[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    returns = [fold_return(Metrics.from_dict(fold[key]) if fold[key] is not None else None) for fold in folds]
    return [
        {"fold_index": fold["fold_index"], "test_end_ms": fold["test_end_ms"], "linked_return": value}
        for fold, value in zip(folds, link_fold_returns(returns), strict=True)
    ]


def _validation(ctx: StageContext, evaluator: StudyEvaluator) -> Verdict:
    folds = fold_windows(ctx.protocol)
    records = [_fold_record(fold) for fold in folds]
    ctx.write({"validation": {"folds": records, "verdict": None, "linked": [], "incumbent_linked": [], "incomplete": False}})
    evidence = []
    for index, fold in enumerate(folds):
        records[index], train, test = _run_fold(ctx, evaluator, fold)
        evidence.append(fold_evidence(fold.fold_index, train, test))
        ctx.write({"validation": {"folds": records, "verdict": None, "linked": [], "incumbent_linked": [], "incomplete": False}})
    verdict = compute_verdict(evidence, min_trades=ctx.protocol.policy.min_trades)
    ctx.write(
        {
            "validation": {
                "folds": records,
                "verdict": verdict.as_dict(),
                "linked": _linked(records, "test_metrics"),
                "incumbent_linked": _linked(records, "incumbent_test_metrics"),
                "incomplete": any(record["failure_code"] == "BUDGET" for record in records),
            }
        }
    )
    return verdict


def _candidates(ctx: StageContext) -> list[tuple[CandidateKey, dict[str, Any], tuple[str, ...]]]:
    """(key, point, edge hits) for the incumbent and each fitted candidate."""
    candidates: list[tuple[CandidateKey, dict[str, Any], tuple[str, ...]]] = [("incumbent", dict(ctx.protocol.incumbent.params), ())]
    for step, key in (("search", "all_period"), ("recent", "recent")):
        if step in ctx.results:
            procedure = ctx.results[step]["procedure"]
            candidates.append((key, dict(procedure["winner"]), tuple(procedure["edge_hits"])))
    return candidates


@dataclass
class _EvidenceRun:
    """One evidence pass: the evaluator, the window, and whether the budget ran out."""

    ctx: StageContext
    evaluator: StudyEvaluator
    exhausted: bool = False

    def one(self, point: Mapping[str, Any], **kwargs: Any) -> Metrics | None:
        if self.exhausted:
            return None
        metrics = _evaluate_one(self.evaluator, point, window=self.ctx.development, stage="evidence", **kwargs)
        self.exhausted = metrics is None
        return metrics

    def neighborhoods(self, point: Mapping[str, Any], center: Metrics | None) -> tuple[Neighborhood, ...]:
        """Every searched knob one neighbor step either way from ``point``; untested once the budget is gone."""
        values = knob_values(self.ctx.declaration, point)
        hoods = []
        for plan in self.ctx.protocol.search_knobs:
            probes = neighbor_probes(self.ctx.declaration, point, plan.name)
            by_hash: dict[str, Metrics] = {}
            for probe in probes:
                if probe.status != "testable":
                    continue
                assert probe.point is not None and probe.point_hash is not None
                metrics = self.one(probe.point)
                if metrics is not None:
                    by_hash[probe.point_hash] = metrics
            hoods.append(neighborhood(plan.name, float(values[plan.name]), center, probes, by_hash))
        return tuple(hoods)


def _evidence(ctx: StageContext, evaluator: StudyEvaluator, verdict: Verdict) -> None:
    run = _EvidenceRun(ctx=ctx, evaluator=evaluator)
    stored: list[dict[str, Any]] = []
    evidences: list[CandidateEvidence] = []
    audited: dict[str, tuple[Neighborhood, ...]] = {}
    for key, point, edges in _candidates(ctx):
        digest = point_hash(ctx.row.strategy_key, point)
        metrics = run.one(point, detail=True)
        audit = key != "incumbent" and ctx.protocol.neighbor_audit
        if audit and digest not in audited:
            audited[digest] = run.neighborhoods(point, metrics)
        hoods = audited.get(digest, ()) if audit else ()
        stress = tuple(
            StressResult(scenario=scenario.key, label=scenario.label, metrics=run.one(point, scenario=scenario))
            for scenario in ctx.protocol.stress
        )
        evidences.append(
            CandidateEvidence(key=key, point=point, point_hash=digest, metrics=metrics, neighborhoods=hoods, stress=stress, edge_hits=edges)
        )
        stored.append(
            {
                "key": key,
                "point": point,
                "point_hash": digest,
                "development_metrics": _metrics_dict(metrics),
                "neighbors": [hood.as_dict() for hood in hoods],
                "neighbors_audited": audit,
                "stress": [{"scenario": item.scenario, "label": item.label, "metrics": _metrics_dict(item.metrics)} for item in stress],
                "edge_hits": list(edges),
            }
        )
    advice = recommendation(evidences, verdict, ctx.protocol.policy)
    window = ctx.development
    ctx.write(
        {
            "evidence": {
                "window": {"start_ms": window[0], "end_ms": window[1]},
                "candidates": stored,
                "recommendation": advice.as_dict(),
                "findings": [{"code": item.code, "text": item.text, "candidate": item.candidate} for item in advice.findings],
                "incomplete": run.exhausted,
            }
        }
    )


def run_validation(ctx: StageContext) -> str:
    evaluator = ctx.evaluator(
        allowed=ctx.development,
        limit=ctx.row.budget_cap - GENERAL_RESERVE,
        total=stage_total(ctx.row, "validation"),
    )
    ctx.on_phase("validation")
    verdict = _validation(ctx, evaluator)
    ctx.on_phase("evidence")
    _evidence(ctx, evaluator, verdict)
    return "awaiting_candidate"


# ── Exam ─────────────────────────────────────────────────────────────────


def _development_metrics(ctx: StageContext, point_digest: str) -> Metrics | None:
    for candidate in ctx.results.get("evidence", {}).get("candidates", []):
        if candidate["point_hash"] == point_digest and candidate["development_metrics"] is not None:
            return Metrics.from_dict(candidate["development_metrics"])
    return None


def run_exam(ctx: StageContext) -> tuple[str, dict[str, Any]]:
    """Score the locked candidate and the frozen incumbent once on the final interval, and judge."""
    exam = dict(ctx.results["exam"])
    evaluator = ctx.evaluator(allowed=ctx.final, limit=ctx.row.budget_cap - EXAM_RESERVE, total=stage_total(ctx.row, "exam"))
    ctx.on_phase("exam")
    (candidate,) = evaluator.evaluate([exam["candidate_point"]], window=ctx.final, stage="exam", detail=True)
    (incumbent,) = evaluator.evaluate([ctx.protocol.incumbent.params], window=ctx.final, stage="exam", detail=True)
    development = _development_metrics(ctx, exam["candidate_point_hash"])
    judgement = judge_exam(
        candidate,
        incumbent,
        policy=ctx.protocol.policy,
        exam_min_trades=ctx.protocol.exam_min_trades,
        development_objective=None if development is None else objective_value(development, ctx.protocol.policy),
    )
    exam.update(
        outcome=judgement.outcome,
        checks=[check.as_dict() for check in judgement.checks],
        retention=judgement.retention,
        candidate_metrics=candidate.as_dict(),
        incumbent_metrics=incumbent.as_dict(),
    )
    return "awaiting_review", exam


# ── Qualification ────────────────────────────────────────────────────────


def _validation_label(results: Mapping[str, Any]) -> str | None:
    verdict = results.get("validation", {}).get("verdict")
    return None if verdict is None else str(verdict["label"])


def run_qualification(ctx: StageContext, binding: ApprovalBinding, blob_store: Any | None) -> tuple[str, dict[str, Any] | None, str | None]:
    """Hand the exact candidate to the approval workflow; returns (state, qualification result, failure reason)."""
    decision = ctx.row.decision or {}
    exam = ctx.results["exam"]
    evaluator = ctx.evaluator(allowed=ctx.final, limit=ctx.row.budget_cap, total=stage_total(ctx.row, "qualification"))
    weakness = research_weakness(exam["outcome"], exam["claim"], exam["exposure_state"])
    request = binding.request_type(
        study_id=ctx.row.id,
        strategy_key=ctx.row.strategy_key,
        symbol=ctx.row.symbol,
        candidate_point=dict(exam["candidate_point"]),
        proof_window=ProofWindow(
            start_ms=ctx.protocol.final_start_ms,
            end_ms=ctx.protocol.final_end_ms,
            warmup_from_ms=et_midnight_ms(evaluator.warmup_for(ctx.protocol.final_start_ms)),
        ),
        snapshot=DataSnapshot.from_dict(ctx.row.receipt["data_snapshot"]),
        roots=tuple(ctx.roots),
        execution=ctx.protocol.execution,
        research={
            "exam_outcome": exam["outcome"],
            "checks": list(exam["checks"]),
            "claim": exam["claim"],
            "exposure_state": exam["exposure_state"],
            "validation_verdict_label": _validation_label(ctx.results),
            "research_override": bool(weakness) and bool(decision.get("acknowledge_research_weakness")),
            "weakness": weakness,
        },
        note=str(decision["note"]),
        expected_default_qualification_id=decision.get("expected_default_qualification_id"),
        actor=str(decision.get("actor", "owner")),
    )
    study_id, attempt = ctx.row.id, ctx.attempt

    def save_checkpoint(checkpoint: Any) -> None:
        run_sync(with_connection(repo.save_checkpoint, study_id, attempt, checkpoint.as_dict()))

    def consume_reserved(count: int) -> None:
        evaluator.consume(count, step="proof")

    async def on_commit(conn: Any, qualification_id: str) -> None:
        # Runs inside the approval's publish transaction: the study turns approved with it or not at all.
        await lock_current_attempt(conn, table=repo.STUDIES, record_id=study_id, attempt=attempt)
        await repo.update_study(conn, study_id, changes=_approved_changes(), results_patch={"qualification": _ready(qualification_id)})

    kwargs: dict[str, Any] = {
        "checkpoint": binding.checkpoint_from_dict(decision.get("checkpoint")),
        "save_checkpoint": save_checkpoint,
        "consume_reserved": consume_reserved,
        "on_commit": on_commit,
        "cancel_check": ctx.cancel_check,
    }
    if blob_store is not None:
        kwargs["blob_store"] = blob_store
    ctx.on_phase("proof")
    outcome = binding.approve(request, **kwargs)
    if outcome.status == "approved":
        return "approved", _ready(outcome.qualification_id), None
    failed = {
        "status": "failed",
        "qualification_id": None,
        "failure_code": outcome.failure_code,
        "failure_reason": outcome.failure_reason,
    }
    return "qualification_failed", failed, outcome.failure_reason


def _ready(qualification_id: str | None) -> dict[str, Any]:
    return {"status": "ready", "qualification_id": qualification_id, "failure_code": None, "failure_reason": None}


def _approved_changes() -> dict[str, Any]:
    return {
        "state": "approved",
        "status": "completed",
        "pending_stage": None,
        "stage_token": None,
        "finished_at_ms": now_ms_utc(),
        "failure_reason": None,
        "incomplete": False,
    }


# ── The entry point ──────────────────────────────────────────────────────


def _stage_incomplete(results: Mapping[str, Any], stage: StageName) -> bool:
    keys = {"search": ("search", "recent"), "validation": ("validation", "evidence")}.get(stage, ())
    return any(bool(results.get(key, {}).get("incomplete")) or bool(results.get(key, {}).get("pair_maps_incomplete")) for key in keys)


def execute_stage(
    study_id: str,
    *,
    stage_token: str,
    job_id: str,
    execute: ExecuteBacktest | None = None,
    approval: ApprovalBinding | None = None,
    blob_store: Any | None = None,
    roots: Sequence[Path] | None = None,
    cancel_check: Callable[[], object] = lambda: None,
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_log: Callable[[str], None] = lambda message: None,
) -> StageOutcome:
    """Run the stage a guarded command authorized, on the calling worker thread.

    ``execute`` defaults to the engine (``evaluator.engine_executor``) and
    ``approval`` to ``approval.approve_study``; tests inject both. A
    cancelled stage keeps its results and reads back ``cancelled``; any other
    failure reads back ``failed`` with its reason; both may be finished.
    """
    row, attempt = run_sync(with_connection(repo.claim_stage, study_id, stage_token=stage_token, job_id=job_id))
    stage = row.pending_stage
    assert stage is not None  # claim_stage refuses a study with no authorized stage
    protocol = GoldenSearchProtocol.from_dict(row.protocol)
    declaration = declaration_for(row.strategy_key)
    if declaration is None:
        raise RuntimeError(f"no Golden Search declaration for {row.strategy_key!r}")
    resolved_roots = list(roots) if roots is not None else lifecycle.roots_for(row)
    ctx = StageContext(
        row=row,
        attempt=attempt,
        protocol=protocol,
        declaration=declaration,
        execute=execute
        or engine_executor(
            strategy_key=row.strategy_key,
            execution=protocol.execution,
            manifest=row.receipt["data_snapshot"]["artifacts"],
            cancel_check=cancel_check,
        ),
        roots=resolved_roots,
        cancel_check=cancel_check,
        on_phase=on_phase,
        on_progress=on_progress,
        on_log=on_log,
        results=dict(row.results),
    )
    logger.info("golden search stage started", extra={"action": "golden_search_stage_started", "study_id": study_id, "stage": stage, "attempt": attempt})
    try:
        state = _run(ctx, stage, approval=approval, blob_store=blob_store)
    except JobCancelled:
        _end_run(study_id, attempt, status="cancelled", reason=None)
        raise
    except StaleAttemptError:
        logger.warning(
            "golden search stage superseded; leaving the newer attempt's record alone",
            extra={"action": "golden_search_stage_superseded", "study_id": study_id, "stage": stage, "attempt": attempt},
        )
        raise
    except Exception as exc:
        _end_run(study_id, attempt, status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    logger.info("golden search stage finished", extra={"action": "golden_search_stage_finished", "study_id": study_id, "stage": stage, "state": state})
    return StageOutcome(study_id=study_id, stage=stage, state=state)


def _run(ctx: StageContext, stage: StageName, *, approval: ApprovalBinding | None, blob_store: Any | None) -> str:
    if ctx.row.state != STAGE_STATES[stage]:
        raise RuntimeError(f"study {ctx.row.id} is {ctx.row.state}, not in the {stage} stage")
    finished: dict[str, Any] = {
        "status": "completed",
        "pending_stage": None,
        "stage_token": None,
        "finished_at_ms": now_ms_utc(),
        "failure_reason": None,
    }
    if stage == "search":
        state = run_search(ctx)
        _finish(ctx, {**finished, "state": state, "incomplete": _stage_incomplete(ctx.results, stage)}, {})
        return state
    if stage == "validation":
        state = run_validation(ctx)
        _finish(ctx, {**finished, "state": state, "incomplete": _stage_incomplete(ctx.results, stage)}, {})
        return state
    if stage == "exam":
        state, exam = run_exam(ctx)
        exposure = {
            "symbol": ctx.row.symbol,
            "start_ms": ctx.protocol.final_start_ms,
            "end_ms": ctx.protocol.final_end_ms,
            "strategy_key": ctx.row.strategy_key,
            "state_at_reservation": exam["exposure_state"],
            "claim": exam["claim"],
            "candidate_point_hash": exam["candidate_point_hash"],
            "payload": {
                "outcome": exam["outcome"],
                "checks": exam["checks"],
                "candidate_metrics": exam["candidate_metrics"],
                "incumbent_metrics": exam["incumbent_metrics"],
                "protocol_hash": ctx.row.protocol_hash,
            },
        }
        run_sync(
            with_connection(
                repo.finish_exam,
                ctx.row.id,
                ctx.attempt,
                changes={**finished, "state": state, "incomplete": False},
                results_patch={"exam": exam},
                exposure=exposure,
            )
        )
        return state
    state, qualification, reason = run_qualification(ctx, approval or default_approval(), blob_store)
    if state == "approved":
        current = run_sync(with_connection(repo.get_study, ctx.row.id))
        if current is None or current.state != "approved":
            # The publish committed without moving the study (an idempotent approval retry): move it now.
            _finish(ctx, _approved_changes(), {"qualification": qualification})
        return state
    _finish(ctx, {**finished, "state": state, "incomplete": False, "failure_reason": reason}, {"qualification": qualification})
    return state


def _finish(ctx: StageContext, changes: Mapping[str, Any], results_patch: Mapping[str, Any]) -> None:
    run_sync(
        with_connection(
            repo.update_study_fenced,
            ctx.row.id,
            ctx.attempt,
            changes=changes,
            results_patch=results_patch,
            bump_revision=True,
        )
    )


def _end_run(study_id: str, attempt: int, *, status: str, reason: str | None) -> None:
    run_sync(
        with_connection(
            repo.update_study_fenced,
            study_id,
            attempt,
            # The stopped stage keeps its name for Finish; its token is spent, so only Finish can re-dispatch it.
            changes={"status": status, "stage_token": None, "incomplete": True, "failure_reason": reason, "finished_at_ms": now_ms_utc()},
            bump_revision=True,
        )
    )
