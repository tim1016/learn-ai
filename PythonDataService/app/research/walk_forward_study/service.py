"""Walk-forward study orchestration (PRD #1925).

A study is a *procedure* over Grid Search: for each fold, sweep the grid over
the training window through Grid Search's callable interface, take the fold
winner from the ranking contract, sweep the same grid over the test window
(the winner's cell is the evidence; every other test cell is labelled
exploratory), and finally apply the frozen verdict over the fold winners.
The study freezes once — one data snapshot over its whole range, one code
identity — and every fold sweep binds its reads to that snapshot. The
per-fold sweeps are ordinary Grid Search records owned by the study, so
their cells, receipts, attempt fence and Finish semantics are Grid Search's;
the study adds folds, selection, exploratory labelling and the verdict.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1925 revision 7.
Canonical implementation: this file.
Validated against: tests/research/walk_forward_study/test_service.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import pairwise
from pathlib import Path

from app.jobs.progress import JobCancelled
from app.research.grid_search import repository as sweep_repo
from app.research.grid_search import service as sweeps
from app.research.grid_search.models import CellResult, CellRow, GridSearchSpec, SearchOwner, SearchRow
from app.research.grid_search.service import GridSearchRefusal
from app.research.persistence import lifecycle
from app.research.persistence.db import run_sync, with_connection
from app.research.persistence.fence import StaleAttemptError
from app.research.sweep.grid import RunSpec
from app.research.sweep.identity import resolve_code_identity
from app.research.sweep.snapshot import DataSnapshot, DataSnapshotIncompleteError, capture_data_snapshot
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study.folds import FoldPlan, FoldPlanError, plan_folds
from app.research.walk_forward_study.models import FoldRecord, NewStudy, StudyRow, StudySpec, StudyStatus
from app.research.walk_forward_study.verdict import FoldEvidence, Verdict, compute_verdict
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)

CellExecutorFactory = Callable[[SearchRow, GridSearchSpec], Callable[[RunSpec], CellResult]]
OWNER_KIND = "walk_forward"


# ── Preflight and launch ─────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyPreflight:
    spec: StudySpec
    folds: list[FoldPlan]
    # The whole-range sweep preflight: grid validation, run-up plan, data presence, estimate.
    sweep: sweeps.Preflight


def preflight(spec: StudySpec, *, roots: Sequence[Path] | None = None) -> StudyPreflight:
    """Whole folds first, then everything Grid Search checks, admitted on combinations x folds x 2."""
    try:
        folds = plan_folds(
            start=et_date_at_ms(spec.grid.start_ms),
            end_exclusive=et_date_at_ms(spec.grid.end_ms),
            training_months=spec.training_months,
            test_months=spec.test_months,
        )
    except FoldPlanError as exc:
        raise GridSearchRefusal(str(exc), code="FOLDS_INVALID") from exc
    sweep = sweeps.preflight(spec.grid, backtests_per_combination=2 * len(folds), roots=roots)
    return StudyPreflight(spec=spec, folds=folds, sweep=sweep)


def _fold_records(folds: Sequence[FoldPlan]) -> list[FoldRecord]:
    return [
        FoldRecord(
            fold_index=fold.fold_index,
            train_start_ms=et_midnight_ms(fold.train_start),
            train_end_ms=et_midnight_ms(fold.train_end),
            test_start_ms=et_midnight_ms(fold.test_start),
            test_end_ms=et_midnight_ms(fold.test_end),
        )
        for fold in folds
    ]


def prepare_launch(spec: StudySpec, *, job_id: str | None, roots: Sequence[Path] | None = None, study_id: str | None = None) -> NewStudy:
    """Preflight and freeze the receipt: the sweep receipt over the whole range (run-up included) plus the fold plan."""
    pre = preflight(spec, roots=roots)
    sweep = pre.sweep
    try:
        snapshot = capture_data_snapshot(
            roots=sweep.roots, symbol=spec.grid.symbol, resolution=spec.grid.resolution, data_start=sweep.data_start, data_end=sweep.evaluation_end
        )
    except DataSnapshotIncompleteError as exc:
        raise GridSearchRefusal(str(exc), code="DATA_MISSING") from exc
    folds = _fold_records(pre.folds)
    receipt = {
        **sweeps.build_receipt(sweep, snapshot, resolve_code_identity()),
        "walk_forward": {
            "training_months": spec.training_months,
            "test_months": spec.test_months,
            "step_months": spec.test_months,
            "fold_count": len(folds),
            "combinations": sweep.combinations,
        },
    }
    return NewStudy(
        id=study_id or uuid.uuid4().hex,
        strategy_key=spec.grid.strategy_key,
        symbol=spec.grid.symbol,
        request=spec.as_request_dict(),
        receipt=receipt,
        folds=folds,
        expected_backtests=sweep.total_backtests,
        job_id=job_id,
    )


async def create(study: NewStudy) -> StudyRow:
    return await with_connection(repo.create_study, study)


def load_study(study_id: str) -> tuple[StudyRow, StudySpec]:
    row = run_sync(with_connection(repo.get_study, study_id))
    if row is None:
        raise GridSearchRefusal(f"study {study_id} not found", code="NOT_FOUND")
    return row, StudySpec.from_request_dict(row.request)


# ── Execute ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyOutcome:
    study_id: str
    status: str
    verdict: Verdict | None


@dataclass(frozen=True)
class _StudyRun:
    """Everything a fold needs from the study, fixed for the whole attempt."""

    study: StudyRow
    spec: StudySpec
    job_id: str | None
    roots: list[Path]
    snapshot: DataSnapshot
    combinations: int
    cell_executor: CellExecutorFactory
    cancel_check: Callable[[], object]
    on_progress: Callable[[int], None]
    on_log: Callable[[str], None]


def _ensure_sweep(run: _StudyRun, fold: FoldRecord, phase: str, window: tuple[int, int], existing_id: str | None) -> SearchRow:
    """Launch (or reuse) one owned sweep, bound to the study's frozen snapshot."""
    if existing_id is not None:
        return sweeps.load_search(existing_id)[0]
    record = sweeps.prepare_launch(
        run.spec.sweep_spec(*window),
        job_id=run.job_id,
        owner=SearchOwner(kind=OWNER_KIND, owner_id=run.study.id, fold_index=fold.fold_index, phase=phase),
        roots=run.roots,
        snapshot=run.snapshot,
    )
    return run_sync(sweeps.create(record))


def _run_sweep(run: _StudyRun, sweep: SearchRow, fold: FoldRecord, phase: str, progress_base: int) -> str | None:
    """Run the sweep to a terminal state and return its leader; a sweep Finish finds already complete is read, not re-run."""
    if sweep.status == "completed":
        run.on_log(f"fold {fold.fold_index + 1} {phase}: sweep {sweep.id[:8]} already complete")
        run.on_progress(progress_base + sweep.expected_cells)
        return sweep.leader_params_hash
    run.on_log(f"fold {fold.fold_index + 1} {phase}: sweep {sweep.id[:8]} ({sweep.expected_cells} cells)")
    outcome = sweeps.execute(
        sweep.id,
        job_id=run.job_id,
        execute_cell=run.cell_executor(sweep, GridSearchSpec.from_request_dict(sweep.request)),
        cancel_check=run.cancel_check,
        on_progress=lambda done, total: run.on_progress(progress_base + done),
        on_log=run.on_log,
    )
    return outcome.leader_params_hash


def _cell(search_id: str, params_hash: str) -> CellRow | None:
    cells = run_sync(with_connection(sweep_repo.list_all_cells, search_id))
    return next((cell for cell in cells if cell.params_hash == params_hash), None)


def _recorded_cells(search_id: str | None) -> int:
    if search_id is None:
        return 0
    row = sweeps.load_search(search_id)[0]
    return row.completed_cells + row.failed_cells


def _run_fold(run: _StudyRun, fold: FoldRecord, persist: Callable[[FoldRecord], None]) -> FoldRecord:
    """Train, select, test. Returns the completed or failed record; ``persist`` sees every durable step."""
    base = 2 * run.combinations * fold.fold_index
    try:
        run.cancel_check()
        train = _ensure_sweep(run, fold, "train", (fold.train_start_ms, fold.train_end_ms), fold.train_search_id)
        fold = replace(fold, status="running", train_search_id=train.id)
        persist(fold)  # the sweep id is durable before a cell runs, so a Finish reuses it
        winner_hash = _run_sweep(run, train, fold, "train", base)
        if winner_hash is None:
            raise GridSearchRefusal("no candidate was eligible to win the training window", code="NO_ELIGIBLE_CANDIDATE")
        winner_train = _cell(train.id, winner_hash)
        assert winner_train is not None

        run.cancel_check()
        test = _ensure_sweep(run, fold, "test", (fold.test_start_ms, fold.test_end_ms), fold.test_search_id)
        fold = replace(fold, test_search_id=test.id)
        persist(fold)
        _run_sweep(run, test, fold, "test", base + run.combinations)
        # The test sweep's own ranking is hindsight; its evidence is the training winner.
        run_sync(with_connection(sweep_repo.record_evidence_winner, test.id, params_hash=winner_train.params_hash, params=dict(winner_train.params)))
        winner_test = _cell(test.id, winner_train.params_hash)
        if winner_test is None or winner_test.status != "completed":
            raise GridSearchRefusal(
                f"the winner's test run failed: {winner_test.error if winner_test else 'no cell recorded'}", code="WINNER_TEST_FAILED"
            )
        evidence = FoldEvidence(
            fold_index=fold.fold_index, status="completed", train_sharpe=winner_train.sharpe_ratio, test_sharpe=winner_test.sharpe_ratio, test_trades=winner_test.total_trades
        )
        run.on_log(f"fold {fold.fold_index + 1}: winner {winner_train.params_hash[:8]} train Sharpe {winner_train.sharpe_ratio} → test Sharpe {winner_test.sharpe_ratio}")
        return replace(
            fold,
            status="completed",
            winner_params_hash=winner_train.params_hash,
            winner_params=dict(winner_train.params),
            train_sharpe=winner_train.sharpe_ratio,
            test_sharpe=winner_test.sharpe_ratio,
            test_trades=winner_test.total_trades,
            retention=evidence.retention,
            recorded_backtests=_recorded_cells(train.id) + _recorded_cells(test.id),
        )
    except GridSearchRefusal as exc:
        run.on_log(f"fold {fold.fold_index + 1} failed: {exc}")
        return replace(
            fold,
            status="failed",
            failure_reason=f"{exc.code}: {exc}",
            recorded_backtests=_recorded_cells(fold.train_search_id) + _recorded_cells(fold.test_search_id),
        )


def execute(
    study_id: str,
    *,
    job_id: str | None,
    cell_executor: CellExecutorFactory,
    cancel_check: Callable[[], object] = lambda: None,
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_log: Callable[[str], None] = lambda message: None,
    roots: Sequence[Path] | None = None,
) -> StudyOutcome:
    """Run (or Finish) a launched study on the calling worker thread; completed folds are never re-run."""
    on_phase("preflight")
    study, spec = load_study(study_id)
    attempt = run_sync(with_connection(repo.claim_attempt, study_id, job_id=job_id))
    run = _StudyRun(
        study=study,
        spec=spec,
        job_id=job_id,
        roots=list(roots) if roots is not None else lifecycle.roots_for(study),
        snapshot=DataSnapshot.from_dict(study.receipt["data_snapshot"]),
        combinations=int(study.receipt["walk_forward"]["combinations"]),
        cell_executor=cell_executor,
        cancel_check=cancel_check,
        on_progress=lambda done: on_progress(done, study.expected_backtests),
        on_log=on_log,
    )
    folds = list(study.folds)

    def _persist(index: int, fold: FoldRecord) -> None:
        folds[index] = fold
        run_sync(with_connection(repo.update_folds, study_id, attempt, folds, completed_backtests=sum(f.recorded_backtests for f in folds)))

    def _persist_partial_progress() -> None:
        """An interrupted fold keeps the cells its sweeps recorded; the study's count must say so before it stops."""
        index = next((i for i, f in enumerate(folds) if f.status == "running"), None)
        if index is None:
            return
        fold = folds[index]
        _persist(index, replace(fold, recorded_backtests=_recorded_cells(fold.train_search_id) + _recorded_cells(fold.test_search_id)))

    def _finish(status: StudyStatus, verdict: Verdict | None, *, incomplete: bool, reason: str | None) -> None:
        run_sync(
            with_connection(
                repo.finish_study, study_id, attempt, status=status, verdict=verdict.as_dict() if verdict else None, incomplete=incomplete, failure_reason=reason
            )
        )

    on_phase("running")
    try:
        for index, fold in enumerate(folds):
            if fold.status != "completed":
                _persist(index, _run_fold(run, fold, partial(_persist, index)))
    except JobCancelled:
        _persist_partial_progress()
        _finish("cancelled", None, incomplete=True, reason=None)
        raise
    except StaleAttemptError:
        logger.warning("walk-forward study %s attempt %s superseded; leaving the newer attempt's record alone", study_id, attempt)
        raise
    except Exception as exc:
        _finish("failed", None, incomplete=True, reason=f"{type(exc).__name__}: {exc}")
        raise

    on_phase("verdict")
    verdict = compute_verdict(
        [
            FoldEvidence(fold_index=f.fold_index, status="completed" if f.status == "completed" else "failed", train_sharpe=f.train_sharpe, test_sharpe=f.test_sharpe, test_trades=f.test_trades)
            for f in folds
        ],
        min_trades=spec.grid.min_trades,
    )
    all_failed = all(f.status == "failed" for f in folds)
    status: StudyStatus = "failed" if all_failed else "completed"
    _finish(status, verdict, incomplete=False, reason="every fold failed; see the folds for each reason" if all_failed else None)
    on_phase("completed")
    return StudyOutcome(study_id=study_id, status=status, verdict=verdict)


def winner_changes(folds: Sequence[FoldRecord]) -> int:
    """How often the chosen settings moved between consecutive successful folds."""
    hashes = [fold.winner_params_hash for fold in folds if fold.status == "completed" and fold.winner_params_hash]
    return sum(1 for previous, following in pairwise(hashes) if previous != following)
