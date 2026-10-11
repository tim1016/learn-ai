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
fits its record names and the frozen incumbent; nothing before the exam may
read the final interval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from app.jobs.progress import JobCancelled
from app.research.golden_search import repository as repo
from app.research.golden_search.actions import authorize
from app.research.golden_search.activity import TradeFloors
from app.research.golden_search.budget import PROOF_EVALUATIONS
from app.research.golden_search.chance_approval import chance_weakness_items
from app.research.golden_search.chance_compare import (
    development_readings,
    failed_component,
    failed_readings,
    incumbent_readings,
)
from app.research.golden_search.chance_drawdown import drawdown_reading
from app.research.golden_search.chance_final import (
    SELECTION_NOTE,
    FinalChanceScope,
    carry_over_reading,
    fit_to_fit_reading,
    stored_final_chance,
)
from app.research.golden_search.chance_rehearsal import failed_rehearsal, rehearsal_reading
from app.research.golden_search.concentration import concentration
from app.research.golden_search.declarations import SearchDeclaration, declaration_for, knob_values, point_hash
from app.research.golden_search.evaluator import (
    EvaluationCapability,
    EvidenceIntegrityError,
    ExecuteBacktest,
    StudyEvaluator,
    Window,
    engine_executor,
    require_intact_detail,
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
from app.research.golden_search.final_test import fit_for, fit_label, pick_of, recorded_fits
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
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.research.sweep.snapshot import DataSnapshot
from app.research.walk_forward_study.verdict import Verdict, compute_verdict
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: The exam may not touch the proof's reservation.
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
    # The spawned-worker dispatch (#3044). None — every caller today — runs
    # the serial adapter; the research runtime passes a factory bound to its
    # lease gate, and execute_stage hands it this stage's sealed context.
    dispatch_factory: Callable[[Any], Any] | None = None
    _built_dispatch: Any = field(default=None, init=False, repr=False)

    @property
    def development(self) -> Window:
        return (self.protocol.development_start_ms, self.protocol.development_end_ms)

    @property
    def final(self) -> Window:
        return (self.protocol.final_start_ms, self.protocol.final_end_ms)

    @cached_property
    def floors(self) -> TradeFloors:
        # Resolved on first use, inside the stage run, so a refused receipt fails the stage with its reason.
        return TradeFloors(self.protocol, self.row.receipt)

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
            dispatch=self._dispatch,
        )

    @property
    def _dispatch(self):
        """The built worker dispatch, or None for the serial adapter (#3044)."""
        factory = self.dispatch_factory
        if factory is None:
            return None
        if self._built_dispatch is None:
            from app.research.golden_search.worker import WorkerDispatchContext

            execution = self.protocol.execution
            self._built_dispatch = factory(WorkerDispatchContext(
                strategy_key=self.row.strategy_key,
                execution={
                    "fill_mode": execution.fill_mode,
                    "commission_per_order": execution.commission_per_order,
                    "slippage_per_share": execution.slippage_per_share,
                    "initial_cash": execution.initial_cash,
                },
                manifest=self.row.receipt["data_snapshot"]["artifacts"],
                identity=resolve_code_identity().__dict__,
                job_id=str(self.row.job_id or ""),
                generation=self.attempt,
            ))
        return self._built_dispatch

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


def stage_refusal(row: StudyRow, *, identity: CodeIdentity | None = None) -> str | None:
    """Why no new stage may run for this study: the code or environment it was locked under has moved.

    Every stage scores under the execution context frozen at lock; a stage run
    by other code would record evaluations under an identity that did not
    compute them, and mix them with the ones that did (#2696).
    """
    moved = lifecycle.identity_refusal(row, noun="study", identity=identity)
    return None if moved is None else f"No new stage can run for this study: {moved}"


class StageRefusedError(RuntimeError):
    """A claimed stage did not run: this process cannot score it under the study's receipted identity."""


def stage_total(row: StudyRow, stage: StageName) -> int:
    """The stage's conservative bound, from the estimate frozen at lock."""
    bounds = {item["stage"]: int(item["max_evaluations"]) for item in row.receipt["estimate"]["stages"]}
    return sum(bounds.get(name, 0) for name in STAGE_ESTIMATES[stage])


def general_reserve(row: StudyRow) -> int:
    """The runs the lock set aside for the exam and the proof; development stages may never touch them."""
    return int(row.receipt["estimate"]["reserved_for_exam_and_proof"])


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
    result = run(declaration=ctx.declaration, protocol=ctx.protocol, seed=ctx.protocol.seed, evaluate=counting, policy=ctx.floors.policy(window))
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
        allowed=ctx.development, limit=ctx.row.budget_cap - general_reserve(ctx.row), total=stage_total(ctx.row, "search")
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
    eligible = result.winner_metrics is not None and ineligibility(result.winner_metrics, ctx.floors.policy(train)) is None
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
        {"fold_index": fold["fold_index"], "test_end_ms": fold["test_end_ms"], "linked_return": value, "fold_missing": own is None}
        for fold, own, value in zip(folds, returns, link_fold_returns(returns), strict=True)
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
    forward_window = (folds[0].test_start_ms, folds[-1].test_end_ms)
    verdict = compute_verdict(evidence, min_trades=ctx.floors.at(forward_window))
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


def _drawdown_for(ctx: StageContext, evidence: CandidateEvidence, detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """One run's own drawdown reading; the module owns its failures, so only cancellation escapes it (#3013)."""
    try:
        return drawdown_reading(
            window=ctx.development,
            ceiling=ctx.protocol.policy.max_drawdown_ceiling,
            metrics=evidence.metrics,
            detail=detail,
            point_hash=evidence.point_hash,
            cancel_check=ctx.cancel_check,
        )
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:  # a diagnostic failure the stage records; it never fails the evidence itself
        return failed_component(exc, "drawdown")


def _chance_records(ctx: StageContext, evaluator: StudyEvaluator, evidences: Sequence[CandidateEvidence]) -> list[dict[str, Any]]:
    """Each candidate's development chance readings, in candidate order (#3011, #3012, ADR 0074 decision 12).

    The readings resample detail runs the stage already evaluated, so they spend no engine budget. Candidates that
    chose the same settings share one computed record instead of drawing twice; the incumbent is the baseline and is
    never compared with itself. The rehearsal (#3012) reads the same development evidence and the frozen final
    interval's *scheduled* length — never any final-period detail, return or outcome. The drawdown reading (#3013)
    is each run's own — the incumbent's included — over the source window's own session count.
    """
    incumbent = next((evidence for evidence in evidences if evidence.key == "incumbent"), None)
    if incumbent is None:
        return [{} for _ in evidences]
    incumbent_detail = evaluator.detail_of(incumbent.point, window=ctx.development)
    rate = ctx.protocol.expected_trades_per_year
    by_hash: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for evidence in evidences:
        if evidence.key == "incumbent":
            try:
                record = incumbent_readings(objective=ctx.protocol.policy.objective, window=ctx.development)
            except JobCancelled:
                raise  # cancellation stops the stage; it is never recorded as a reading
            except Exception as exc:  # a diagnostic failure the stage records; it never fails the evidence itself
                record = failed_readings(exc)
            # The baseline draws its own depth: the incumbent has a drawdown chart of its own (#3013).
            record["drawdown"] = _drawdown_for(ctx, evidence, incumbent_detail)
            records.append(record)
            continue
        shared = by_hash.get(evidence.point_hash)
        if shared is None:
            try:
                shared = development_readings(
                    objective=ctx.protocol.policy.objective,
                    window=ctx.development,
                    candidate_metrics=evidence.metrics,
                    candidate_detail=evaluator.detail_of(evidence.point, window=ctx.development),
                    candidate_hash=evidence.point_hash,
                    incumbent_metrics=incumbent.metrics,
                    incumbent_detail=incumbent_detail,
                    incumbent_hash=incumbent.point_hash,
                    cancel_check=ctx.cancel_check,
                )
            except JobCancelled:
                raise  # cancellation stops the stage; it is never recorded as a reading
            except Exception as exc:  # a diagnostic failure the stage records; it never fails the evidence itself
                shared = failed_readings(exc)
            # The rehearsal is its own component: a missing or failed one never erases the comparison beside it.
            try:
                shared["rehearsal"] = rehearsal_reading(
                    objective=ctx.protocol.policy.objective,
                    window=ctx.development,
                    final_window=ctx.final,
                    floors=ctx.floors,
                    rate=rate,
                    final_years=ctx.floors.trading_years(ctx.final) if rate is not None else None,
                    ceiling=ctx.protocol.policy.max_drawdown_ceiling,
                    require_positive_net=ctx.protocol.policy.require_positive_net,
                    candidate_metrics=evidence.metrics,
                    candidate_detail=evaluator.detail_of(evidence.point, window=ctx.development),
                    candidate_hash=evidence.point_hash,
                    incumbent_metrics=incumbent.metrics,
                    incumbent_detail=incumbent_detail,
                    incumbent_hash=incumbent.point_hash,
                    cancel_check=ctx.cancel_check,
                )
            except JobCancelled:
                raise  # cancellation stops the stage; it is never recorded as a reading
            except Exception as exc:  # a diagnostic failure the stage records; it never fails the evidence itself
                shared["rehearsal"] = failed_rehearsal(exc)
            # The drawdown component is its own too: it needs this run alone, never the pairing (#3013).
            shared["drawdown"] = _drawdown_for(ctx, evidence, evaluator.detail_of(evidence.point, window=ctx.development))
            by_hash[evidence.point_hash] = shared
        records.append(shared)
    return records


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
                # Informs the decision summary only (ADR 0074 decision 11).
                "concentration": concentration(
                    metrics, evaluator.detail_of(point, window=ctx.development), commission_per_order=ctx.protocol.execution.commission_per_order
                ),
            }
        )
    for record, chance in zip(stored, _chance_records(ctx, evaluator, evidences), strict=True):
        record["chance"] = chance
    advice = recommendation(evidences, verdict, ctx.floors.policy(ctx.development))
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
        limit=ctx.row.budget_cap - general_reserve(ctx.row),
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


def _missing_single_fit() -> dict[str, Any]:
    """The optional fit-to-fit comparison when only one distinct fit ran: absent by design, not failed (#3014)."""
    return {
        "status": "missing",
        "code": "single_fit",
        "reason": "Only one distinct fit sat this final test, so there is no second fit to compare it with.",
    }


def _stored_rehearsal_for(ctx: StageContext, point_hash: str) -> dict[str, Any] | None:
    """The stored 1× development rehearsal of one point's evidence record, if this study recorded one (#3012, #3014)."""
    for candidate in ctx.results.get("evidence", {}).get("candidates", []):
        if candidate.get("point_hash") == point_hash:
            rehearsal = (candidate.get("chance") or {}).get("rehearsal")
            return rehearsal if isinstance(rehearsal, dict) else None
    return None


def _development_detail_for(ctx: StageContext, point: Mapping[str, Any]) -> tuple[Metrics | None, Mapping[str, Any] | None]:
    """The stored development detail run of one point, read back without an engine evaluation or budget (#3014).

    A completed row whose recorded payload is absent or misshapen raises
    :class:`EvidenceIntegrityError` instead of resurfacing as a missing
    carry-over reading (#3039); a failed run's absent payload is legitimate.
    """
    digest = point_hash(ctx.row.strategy_key, dict(point))
    record = run_sync(with_connection(repo.find_detail_evaluation, ctx.row.id, point_hash=digest, window_start_ms=ctx.development[0], window_end_ms=ctx.development[1]))
    if record is None:
        return None, None
    return repo.metrics_of(record), require_intact_detail(record.evaluation_key, record.status, record.detail_json)


def _final_chance_for(
    ctx: StageContext,
    evaluator: StudyEvaluator,
    fit: dict[str, Any],
    incumbent: Metrics,
    incumbent_detail: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One fit's final chance readings, computed after its frozen judgement from runs the stage already executed.

    No re-test, no extra engine evaluation: the comparison and profitability resample the exam's own detail runs, and
    the carry-over reproduces the stored rehearsal from its seed over the stored development input read back as-is.
    """
    readings: dict[str, Any] = {}
    try:
        # The final interval's own sessions, so every sentence names the window it resampled and carries the
        # two-selection caveat, never the development stage's selected-on-these-sessions note (#3014).
        readings = development_readings(
            objective=ctx.protocol.policy.objective,
            window=ctx.final,
            candidate_metrics=Metrics.from_dict(fit["metrics"]),
            candidate_detail=evaluator.detail_of(fit["point"], window=ctx.final),
            candidate_hash=fit["point_hash"],
            incumbent_metrics=incumbent,
            incumbent_detail=incumbent_detail,
            incumbent_hash=point_hash(ctx.row.strategy_key, dict(ctx.protocol.incumbent.params)),
            sessions_label="final",
            bias_note=SELECTION_NOTE,
            cancel_check=ctx.cancel_check,
        )
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except EvidenceIntegrityError:
        raise  # corrupted durable evidence fails the exam; a failed reading would conclude over it (#3039)
    except Exception as exc:  # a diagnostic failure the stage records; it never fails the exam itself
        readings = failed_readings(exc)
    try:
        development_metrics, development_detail = _development_detail_for(ctx, fit["point"])
        readings["carry_over"] = carry_over_reading(
            objective=ctx.protocol.policy.objective,
            development_window=ctx.development,
            final_window=ctx.final,
            development_detail=development_detail,
            development_metrics=development_metrics,
            final_metrics=Metrics.from_dict(fit["metrics"]),
            final_detail=evaluator.detail_of(fit["point"], window=ctx.final),
            point_hash=fit["point_hash"],
            stored_rehearsal=_stored_rehearsal_for(ctx, fit["point_hash"]),
            cancel_check=ctx.cancel_check,
        )
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except EvidenceIntegrityError:
        raise  # corrupted durable evidence fails the exam; a failed component would conclude over it (#3039)
    except Exception as exc:  # a diagnostic failure the stage records; it never fails the exam itself
        readings["carry_over"] = failed_component(exc, "carry-over")
    return readings


def run_exam(ctx: StageContext) -> tuple[str, dict[str, Any]]:
    """Score every fit the exam names and the frozen incumbent once on the final interval, and judge each fit."""
    exam = dict(ctx.results["exam"])
    fits = recorded_fits(exam)
    evaluator = ctx.evaluator(allowed=ctx.final, limit=ctx.row.budget_cap - EXAM_RESERVE, total=stage_total(ctx.row, "exam"))
    ctx.on_phase("exam")
    scored = evaluator.evaluate([fit["point"] for fit in fits], window=ctx.final, stage="exam", detail=True)
    (incumbent,) = evaluator.evaluate([ctx.protocol.incumbent.params], window=ctx.final, stage="exam", detail=True)
    incumbent_detail = evaluator.detail_of(ctx.protocol.incumbent.params, window=ctx.final)
    for fit, candidate in zip(fits, scored, strict=True):
        development = _development_metrics(ctx, fit["point_hash"])
        judgement = judge_exam(
            candidate,
            incumbent,
            policy=ctx.protocol.policy,
            exam_min_trades=ctx.floors.at(ctx.final, final=True),
            development_objective=None if development is None else objective_value(development, ctx.protocol.policy),
            frequency_policy=ctx.floors.frequency_based,
        )
        fit.update(
            outcome=judgement.outcome,
            checks=[check.as_dict() for check in judgement.checks],
            retention=judgement.retention,
            metrics=candidate.as_dict(),
        )
        # Diagnostics come after the frozen judgement and are stored on the fit (#3014); they gate nothing.
        fit["chance"] = _final_chance_for(ctx, evaluator, fit, incumbent, incumbent_detail)
    exam.update(fits=fits, incumbent_metrics=incumbent.as_dict())
    # Two distinct fits (the record's twin folding already guaranteed distinct settings): one shared paired
    # draw between them, stored beside the per-fit readings (#3014).
    try:
        if len(fits) >= 2:
            exam["chance"] = {
                "fit_to_fit": fit_to_fit_reading(
                    objective=ctx.protocol.policy.objective,
                    window=ctx.final,
                    first={
                        "metrics": Metrics.from_dict(fits[0]["metrics"]),
                        "detail": evaluator.detail_of(fits[0]["point"], window=ctx.final),
                        "point_hash": fits[0]["point_hash"],
                        "label": fit_label(fits[0]),
                    },
                    second={
                        "metrics": Metrics.from_dict(fits[1]["metrics"]),
                        "detail": evaluator.detail_of(fits[1]["point"], window=ctx.final),
                        "point_hash": fits[1]["point_hash"],
                        "label": fit_label(fits[1]),
                    },
                    cancel_check=ctx.cancel_check,
                )
            }
        else:
            exam["chance"] = {"fit_to_fit": _missing_single_fit()}
    except JobCancelled:
        raise  # cancellation stops the stage; it is never recorded as a reading
    except Exception as exc:  # a diagnostic failure the stage records; it never fails the exam itself
        exam["chance"] = {"fit_to_fit": failed_component(exc, "fit-to-fit")}
    return "awaiting_review", exam


def _exam_exposures(ctx: StageContext, exam: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each fit's outcome as its exposure-ledger row."""
    return [
        {
            "symbol": ctx.row.symbol,
            "start_ms": ctx.protocol.final_start_ms,
            "end_ms": ctx.protocol.final_end_ms,
            "strategy_key": ctx.row.strategy_key,
            "state_at_reservation": exam["exposure_state"],
            "claim": exam["claim"],
            "candidate_point_hash": fit["point_hash"],
            "payload": {
                "candidate_key": fit["key"],
                "same_as": fit["same_as"],
                "outcome": fit["outcome"],
                "checks": fit["checks"],
                "candidate_metrics": fit["metrics"],
                "incumbent_metrics": exam["incumbent_metrics"],
                "protocol_hash": ctx.row.protocol_hash,
            },
        }
        for fit in exam["fits"]
    ]


# ── Qualification ────────────────────────────────────────────────────────


def _validation_label(results: Mapping[str, Any]) -> str | None:
    verdict = results.get("validation", {}).get("verdict")
    return None if verdict is None else str(verdict["label"])


def run_qualification(ctx: StageContext, binding: ApprovalBinding, blob_store: Any | None) -> tuple[str, dict[str, Any] | None, str | None]:
    """Hand the exact candidate to the approval workflow; returns (state, qualification result, failure reason)."""
    decision = ctx.row.decision or {}
    exam = ctx.results["exam"]
    chosen = fit_for(exam, ctx.row.candidate_key)
    assert chosen is not None  # the approval command records the fit it names before it authorizes this stage
    evaluator = ctx.evaluator(allowed=ctx.final, limit=ctx.row.budget_cap, total=stage_total(ctx.row, "qualification"))
    # The same chance authority the approval intent and the page read (#3015): the stored components of the fit
    # the decision named, retained with the qualification so the acknowledgement's evidence is immutable. The
    # scope binds each record to this study's frozen protocol and the chosen fit's point identity (#3024).
    chance = stored_final_chance(chosen, FinalChanceScope.for_protocol(ctx.protocol, point_hash=chosen["point_hash"]))
    chance_items = chance_weakness_items(
        ctx.results.get("chance_approval"),
        chance,
        positive_net_required=bool(ctx.protocol.policy.require_positive_net),
    )
    weakness = research_weakness(chosen["outcome"], exam["claim"], exam["exposure_state"]) + [item["code"] for item in chance_items]
    request = binding.request_type(
        study_id=ctx.row.id,
        strategy_key=ctx.row.strategy_key,
        symbol=ctx.row.symbol,
        candidate_point=dict(chosen["point"]),
        proof_window=ProofWindow(
            start_ms=ctx.protocol.final_start_ms,
            end_ms=ctx.protocol.final_end_ms,
            warmup_from_ms=et_midnight_ms(evaluator.warmup_for(ctx.protocol.final_start_ms)),
        ),
        snapshot=DataSnapshot.from_dict(ctx.row.receipt["data_snapshot"]),
        roots=tuple(ctx.roots),
        execution=ctx.protocol.execution,
        research={
            "exam_outcome": chosen["outcome"],
            "checks": list(chosen["checks"]),
            "candidate_key": chosen["key"],
            "candidate_point_hash": chosen["point_hash"],
            "fits_tested": len(recorded_fits(exam)),
            "pick": pick_of(exam),
            "claim": exam["claim"],
            "exposure_state": exam["exposure_state"],
            "validation_verdict_label": _validation_label(ctx.results),
            "research_override": bool(weakness) and bool(decision.get("acknowledge_research_weakness")),
            "weakness": weakness,
            # The chance evidence behind any chance weakness code: each component's stored status, counts,
            # method and thresholds, and the enrollment policy version the study locked under (#3015).
            "chance": {
                "policy_schema": None if ctx.results.get("chance_approval") is None else ctx.results["chance_approval"].get("schema_version"),
                "components": chance,
                "weakness": chance_items,
            },
        },
        note=str(decision["note"]),
        expected_default_qualification_id=decision.get("expected_default_qualification_id"),
        actor=str(decision.get("actor", "owner")),
    )
    study_id, attempt = ctx.row.id, ctx.attempt

    def save_checkpoint(checkpoint: Any) -> None:
        run_sync(with_connection(repo.save_checkpoint, study_id, attempt, checkpoint.as_dict()))

    def consume_reserved(step: str, count: int) -> None:
        evaluator.consume(count, step="proof", once_key=f"approval:{step}")

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


def _close_dispatch(ctx: StageContext) -> None:
    """Kill and reap the stage's spawned worker, if it built one (#3044)."""
    if ctx._built_dispatch is not None:
        ctx._built_dispatch.close()


def execute_stage(
    study_id: str,
    *,
    stage_token: str,
    job_id: str,
    execute: ExecuteBacktest | None = None,
    dispatch_factory: Callable[[Any], Any] | None = None,
    approval: ApprovalBinding | None = None,
    blob_store: Any | None = None,
    roots: Sequence[Path] | None = None,
    cancel_check: Callable[[], object] = lambda: None,
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_log: Callable[[str], None] = lambda message: None,
    identity: CodeIdentity | None = None,
) -> StageOutcome:
    """Run the stage a guarded command authorized, on the calling worker thread.

    ``execute`` defaults to the engine (``evaluator.engine_executor``) and
    ``approval`` to ``approval.approve_study``; tests inject both. A
    cancelled stage keeps its results and reads back ``cancelled``; any other
    failure reads back ``failed`` with its reason; both may be finished. A
    stage claimed by a process whose code identity is not the study's
    receipted one runs nothing and reads back ``failed`` with that reason.
    """
    row, attempt = run_sync(with_connection(repo.claim_stage, study_id, stage_token=stage_token, job_id=job_id))
    stage = row.pending_stage
    assert stage is not None  # claim_stage refuses a study with no authorized stage
    refused = stage_refusal(row, identity=identity)
    if refused is not None:
        logger.warning(
            "golden search stage refused: the code identity moved since lock",
            extra={"action": "golden_search_stage_identity_refused", "study_id": study_id, "stage": stage, "attempt": attempt},
        )
        _end_run(study_id, attempt, status="failed", reason=refused)
        raise StageRefusedError(refused)
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
        dispatch_factory=dispatch_factory,
    )
    logger.info("golden search stage started", extra={"action": "golden_search_stage_started", "study_id": study_id, "stage": stage, "attempt": attempt})
    try:
        state, next_token = _run(ctx, stage, approval=approval, blob_store=blob_store)
    except JobCancelled:
        _close_dispatch(ctx)
        _end_run(study_id, attempt, status="cancelled", reason=None)
        raise
    except StaleAttemptError:
        _close_dispatch(ctx)
        logger.warning(
            "golden search stage superseded; leaving the newer attempt's record alone",
            extra={"action": "golden_search_stage_superseded", "study_id": study_id, "stage": stage, "attempt": attempt},
        )
        raise
    except Exception as exc:
        _close_dispatch(ctx)
        _end_run(study_id, attempt, status="failed", reason=f"{type(exc).__name__}: {exc}")
        raise
    except BaseException:
        # Anything else (a crash, a kill): the spawned worker dies with the
        # stage — its reservations stay pending for Finish, its process never leaks.
        _close_dispatch(ctx)
        raise
    logger.info("golden search stage finished", extra={"action": "golden_search_stage_finished", "study_id": study_id, "stage": stage, "state": state})
    # One worker per stage run: the next stage (if any) spawns its own.
    _close_dispatch(ctx)
    if next_token is not None:
        # Run research (#2811): the same job claims the stage its search authorized, under one cancel flag and lease.
        logger.info("golden search research advanced", extra={"action": "golden_search_research_advanced", "study_id": study_id, "from_stage": stage})
        return execute_stage(
            study_id,
            stage_token=next_token,
            job_id=job_id,
            execute=execute,
            dispatch_factory=dispatch_factory,
            approval=approval,
            blob_store=blob_store,
            roots=roots,
            cancel_check=cancel_check,
            on_phase=on_phase,
            on_progress=on_progress,
            on_log=on_log,
            identity=identity,
        )
    return StageOutcome(study_id=study_id, stage=stage, state=state)


def _run(ctx: StageContext, stage: StageName, *, approval: ApprovalBinding | None, blob_store: Any | None) -> tuple[str, str | None]:
    """Run ``stage`` and close it; the token of the stage it handed on to under Run research, else ``None``."""
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
        incomplete = _stage_incomplete(ctx.results, stage)
        changes = {**finished, "state": state, "incomplete": incomplete}
        if incomplete:
            # An unfinished search pauses Run research here, so the owner sees why before Test over time spends budget.
            _finish(ctx, changes, {})
            return state, None
        advance, token = authorize("validation")
        row, advanced = run_sync(
            with_connection(repo.finish_or_advance, ctx.row.id, ctx.attempt, changes=changes, advance={**advance, "job_id": ctx.row.job_id})
        )
        return row.state, token if advanced else None
    if stage == "validation":
        state = run_validation(ctx)
        _finish(ctx, {**finished, "state": state, "incomplete": _stage_incomplete(ctx.results, stage)}, {})
        return state, None
    if stage == "exam":
        state, exam = run_exam(ctx)
        run_sync(
            with_connection(
                repo.finish_exam,
                ctx.row.id,
                ctx.attempt,
                changes={**finished, "state": state, "incomplete": False},
                results_patch={"exam": exam},
                exposures=_exam_exposures(ctx, exam),
            )
        )
        return state, None
    state, qualification, reason = run_qualification(ctx, approval or default_approval(), blob_store)
    if state == "approved":
        current = run_sync(with_connection(repo.get_study, ctx.row.id))
        if current is None or current.state != "approved":
            # The publish committed without moving the study (an idempotent approval retry): move it now.
            _finish(ctx, _approved_changes(), {"qualification": qualification})
        return state, None
    _finish(ctx, {**finished, "state": state, "incomplete": False, "failure_reason": reason}, {"qualification": qualification})
    return state, None


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
