"""Walk-forward study orchestration (PRD #1925).

A study is a *procedure*: for each fold, sweep the grid over the training
window through Grid Search's callable interface, pick that fold's winner
with the ranking contract, sweep the same grid over the test window (the
winner's cell is the evidence; every other test cell is labelled
exploratory), and finally apply the frozen verdict over the fold winners.
Every window — training and test, winner and exploratory — is preceded by
the same uniform run-up, because Grid Search sizes one for every sweep it
runs. The per-fold sweeps are ordinary Grid Search records owned by the
study, so their cells, receipts, attempt fences and Finish semantics are
Grid Search's; the study adds folds, selection, the verdict, and its own
durable record.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1925 revision 7.
Canonical implementation: this file.
Validated against: tests/research/walk_forward_study/test_service.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.jobs.progress import JobCancelled
from app.research.grid_search import repository as sweep_repo
from app.research.grid_search import service as sweeps
from app.research.grid_search.models import CellResult, CellRow, SearchOwner, SearchRow
from app.research.persistence.db import connection, run_sync
from app.research.sweep.grid import RunSpec
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.research.sweep.ranking import leader
from app.research.sweep.snapshot import (
    DataSnapshot,
    DataSnapshotIncompleteError,
    capture_data_snapshot,
    verify_data_snapshot,
)
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study.folds import FoldPlan, FoldPlanError, plan_folds
from app.research.walk_forward_study.models import FoldRecord, NewStudy, StudyRow, StudySpec, StudyStatus
from app.research.walk_forward_study.verdict import FoldEvidence, Verdict, compute_verdict
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)

GridSearchRefusal = sweeps.GridSearchRefusal
CellExecutorFactory = Callable[[SearchRow, sweeps.GridSearchSpec], Callable[[RunSpec], CellResult]]
RECEIPT_SCHEMA_VERSION = 1


# ── Preflight ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyPreflight:
    spec: StudySpec
    folds: list[FoldPlan]
    combinations: int
    total_backtests: int
    estimated_seconds: float
    required_samples: int
    run_up_sessions: int
    roots: list[Path]


def preflight(spec: StudySpec, *, roots: Sequence[Path] | None = None) -> StudyPreflight:
    """Validate the study: whole folds, the grid, admission on combinations x folds x 2, data, run-up."""
    start, _ = sweeps.window_dates(spec.start_ms, spec.end_ms)
    try:
        folds = plan_folds(start=start, end_exclusive=et_date_at_ms(spec.end_ms), training_months=spec.training_months, test_months=spec.test_months)
    except FoldPlanError as exc:
        raise GridSearchRefusal(str(exc), code="FOLDS_INVALID") from exc
    # One sweep preflight over the whole range validates the grid, sizes the
    # run-up for the slowest candidate, and checks every session is present.
    pre = sweeps.preflight(spec.sweep_spec(spec.start_ms, spec.end_ms), backtests_per_combination=2 * len(folds), roots=roots)
    return StudyPreflight(
        spec=spec,
        folds=folds,
        combinations=pre.combinations,
        total_backtests=pre.total_backtests,
        estimated_seconds=pre.estimated_seconds,
        required_samples=pre.run_up.required_samples,
        run_up_sessions=pre.run_up.run_up_sessions,
        roots=pre.roots,
    )


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
    pre = preflight(spec, roots=roots)
    data_start, data_end = sweeps.window_dates(spec.start_ms, spec.end_ms)
    try:
        snapshot = capture_data_snapshot(roots=pre.roots, symbol=spec.symbol, resolution=spec.resolution, data_start=data_start, data_end=data_end)
    except DataSnapshotIncompleteError as exc:
        raise GridSearchRefusal(str(exc), code="DATA_MISSING") from exc
    identity = resolve_code_identity()
    folds = _fold_records(pre.folds)
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "training_months": spec.training_months,
        "test_months": spec.test_months,
        "step_months": spec.test_months,
        "fold_count": len(folds),
        "warmup_policy": "uniform_run_up",
        "required_samples": pre.required_samples,
        "run_up_sessions": pre.run_up_sessions,
        "code_identity": identity.as_dict(),
        "data_snapshot": snapshot.as_dict(),
        "data_snapshot_digest": snapshot.digest(),
        "estimated_seconds": pre.estimated_seconds,
    }
    return NewStudy(
        id=study_id or uuid.uuid4().hex,
        strategy_key=spec.strategy_key,
        symbol=spec.symbol,
        request=spec.as_request_dict(),
        receipt=receipt,
        folds=folds,
        expected_backtests=pre.total_backtests,
        job_id=job_id,
    )


async def create(study: NewStudy) -> StudyRow:
    async with connection() as conn:
        return await repo.create_study(conn, study)


# ── Execute ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyOutcome:
    study_id: str
    status: str
    verdict: Verdict | None


def load_study(study_id: str) -> tuple[StudyRow, StudySpec]:
    row = run_sync(_get(study_id))
    if row is None:
        raise GridSearchRefusal(f"study {study_id} not found", code="NOT_FOUND")
    return row, StudySpec.from_request_dict(row.request)


def _snapshot_agrees(study_snapshot: DataSnapshot, sweep: SearchRow) -> str | None:
    """Every artifact a fold sweep receipted must be the byte the study froze at launch."""
    sweep_artifacts: dict[str, str] = sweep.receipt["data_snapshot"]["artifacts"]
    for relative, digest in sweep_artifacts.items():
        expected = study_snapshot.artifacts.get(relative)
        if expected != digest:
            return f"{relative} differs from the study's frozen snapshot"
    return None


def _ensure_sweep(
    *,
    study: StudyRow,
    spec: StudySpec,
    fold: FoldRecord,
    phase: str,
    window: tuple[int, int],
    existing_search_id: str | None,
    job_id: str | None,
    roots: Sequence[Path],
) -> str:
    """Launch (or reuse) one owned sweep and check its snapshot against the study's."""
    if existing_search_id is None:
        record = sweeps.prepare_launch(
            spec.sweep_spec(*window),
            job_id=job_id,
            owner=SearchOwner(kind="walk_forward", owner_id=study.id, fold_index=fold.fold_index, phase=phase),
            roots=roots,
        )
        search_id = run_sync(sweeps.create(record)).id
    else:
        search_id = existing_search_id
    row, _ = sweeps.load_search(search_id)
    mismatch = _snapshot_agrees(DataSnapshot.from_dict(study.receipt["data_snapshot"]), row)
    if mismatch is not None:
        raise GridSearchRefusal(mismatch, code="DATA_SNAPSHOT_MISMATCH")
    return search_id


def _execute_sweep(
    search_id: str,
    *,
    fold: FoldRecord,
    phase: str,
    job_id: str | None,
    cell_executor: CellExecutorFactory,
    cancel_check: Callable[[], object],
    on_progress: Callable[[int], None],
    on_log: Callable[[str], None],
) -> sweeps.ExecutionOutcome:
    """Run one owned sweep to a terminal state (Finish semantics are Grid Search's)."""
    row, sweep_spec = sweeps.load_search(search_id)
    on_log(f"fold {fold.fold_index + 1} {phase}: sweep {search_id[:8]} ({row.expected_cells} cells)")
    return sweeps.execute(
        search_id,
        job_id=job_id,
        execute_cell=cell_executor(row, sweep_spec),
        cancel_check=cancel_check,
        on_progress=lambda done, total: on_progress(done),
        on_log=on_log,
    )


def _winner_cell(search_id: str, params_hash: str) -> CellRow | None:
    return next((cell for cell in run_sync(_cells(search_id)) if cell.params_hash == params_hash), None)


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
    """Run (or Finish) a launched study on the calling worker thread."""
    study, spec = load_study(study_id)
    attempt = run_sync(_claim(study_id, job_id))
    resolved_roots = list(roots) if roots is not None else sweeps.resolve_data_roots(source="polygon", adjusted=True)
    folds = list(study.folds)
    combinations = study.expected_backtests // max(1, 2 * len(folds))
    done_backtests = sum(2 * combinations for fold in folds if fold.status == "completed")

    def _persist() -> None:
        run_sync(_update(study_id, attempt, folds, done_backtests))

    on_phase("running")
    try:
        for index, fold in enumerate(folds):
            if fold.status == "completed":
                continue
            try:
                train_id = _ensure_sweep(
                    study=study, spec=spec, fold=fold, phase="train", window=(fold.train_start_ms, fold.train_end_ms),
                    existing_search_id=fold.train_search_id, job_id=job_id, roots=resolved_roots,
                )
                # The sweep id is durable before a single cell runs, so a Finish reuses it.
                folds[index] = fold = replace(fold, status="running", train_search_id=train_id)
                _persist()
                train_outcome = _execute_sweep(
                    train_id, fold=fold, phase="train", job_id=job_id, cell_executor=cell_executor, cancel_check=cancel_check,
                    on_progress=lambda done, base=done_backtests: on_progress(base + done, study.expected_backtests), on_log=on_log,
                )
                if train_outcome.leader_params_hash is None:
                    raise GridSearchRefusal("no candidate was eligible to win the training window", code="NO_ELIGIBLE_CANDIDATE")
                winner_train = _winner_cell(train_id, train_outcome.leader_params_hash)
                assert winner_train is not None

                test_id = _ensure_sweep(
                    study=study, spec=spec, fold=fold, phase="test", window=(fold.test_start_ms, fold.test_end_ms),
                    existing_search_id=fold.test_search_id, job_id=job_id, roots=resolved_roots,
                )
                folds[index] = fold = replace(fold, test_search_id=test_id)
                _persist()
                _execute_sweep(
                    test_id, fold=fold, phase="test", job_id=job_id, cell_executor=cell_executor, cancel_check=cancel_check,
                    on_progress=lambda done, base=done_backtests + combinations: on_progress(base + done, study.expected_backtests), on_log=on_log,
                )
                run_sync(_mark_exploratory(test_id, winner_train.params_hash))
                winner_test = _winner_cell(test_id, winner_train.params_hash)
                if winner_test is None or winner_test.status != "completed":
                    raise GridSearchRefusal(
                        f"the winner's test run failed: {winner_test.error if winner_test else 'no cell recorded'}",
                        code="WINNER_TEST_FAILED",
                    )
                evidence = FoldEvidence(
                    fold_index=fold.fold_index, status="completed",
                    train_sharpe=winner_train.sharpe_ratio, test_sharpe=winner_test.sharpe_ratio, test_trades=winner_test.total_trades,
                )
                folds[index] = fold = replace(
                    fold, status="completed", winner_params_hash=winner_train.params_hash,
                    winner_params=dict(winner_train.params), train_sharpe=winner_train.sharpe_ratio,
                    test_sharpe=winner_test.sharpe_ratio, test_trades=winner_test.total_trades, retention=evidence.retention,
                )
                done_backtests += 2 * combinations
                _persist()
                on_log(f"fold {fold.fold_index + 1}: winner {winner_train.params_hash[:8]} train Sharpe {winner_train.sharpe_ratio} → test Sharpe {winner_test.sharpe_ratio}")
            except GridSearchRefusal as exc:
                folds[index] = replace(fold, status="failed", failure_reason=f"{exc.code}: {exc}")
                done_backtests += 2 * combinations
                _persist()
                on_log(f"fold {fold.fold_index + 1} failed: {exc}")
    except JobCancelled:
        run_sync(_finish(study_id, attempt, "cancelled", None, True, None))
        raise
    except sweep_repo.StaleAttemptError:
        logger.warning("walk-forward study %s attempt %s superseded", study_id, attempt)
        raise
    except Exception as exc:
        run_sync(_finish(study_id, attempt, "failed", None, True, f"{type(exc).__name__}: {exc}"))
        raise

    on_phase("verdict")
    verdict = compute_verdict(
        [FoldEvidence(fold_index=f.fold_index, status="completed" if f.status == "completed" else "failed", train_sharpe=f.train_sharpe, test_sharpe=f.test_sharpe, test_trades=f.test_trades) for f in folds],
        min_trades=spec.min_trades,
    )
    all_failed = all(f.status == "failed" for f in folds)
    status: StudyStatus = "failed" if all_failed else "completed"
    reason = "every fold failed; see the folds for each reason" if all_failed else None
    run_sync(_finish(study_id, attempt, status, verdict.as_dict(), False, reason))
    on_phase("completed")
    return StudyOutcome(study_id=study_id, status=status, verdict=verdict)


async def _get(study_id: str) -> StudyRow | None:
    async with connection() as conn:
        return await repo.get_study(conn, study_id)


async def _claim(study_id: str, job_id: str | None) -> int:
    async with connection() as conn:
        return await repo.claim_attempt(conn, study_id, job_id=job_id)


async def _update(study_id: str, attempt: int, folds: Sequence[FoldRecord], completed_backtests: int) -> None:
    async with connection() as conn:
        await repo.update_folds(conn, study_id, attempt, folds, completed_backtests=completed_backtests)


async def _finish(study_id: str, attempt: int, status: StudyStatus, verdict: dict[str, Any] | None, incomplete: bool, reason: str | None) -> None:
    async with connection() as conn:
        await repo.finish_study(conn, study_id, attempt, status=status, verdict=verdict, incomplete=incomplete, failure_reason=reason)


async def _cells(search_id: str) -> list[CellRow]:
    async with connection() as conn:
        return await sweep_repo.list_all_cells(conn, search_id)


async def _mark_exploratory(search_id: str, winner_hash: str) -> None:
    async with connection() as conn:
        await sweep_repo.mark_exploratory(conn, search_id, evidence_params_hash=winner_hash)


# ── Presentation and Finish rules ────────────────────────────────────────

job_is_live = sweeps.job_is_live
request_cancel = sweeps.request_cancel


def presented_status(row: StudyRow, *, live: bool | None) -> str:
    if row.status in ("queued", "running") and live is False:
        return "interrupted"
    return row.status


def uncommitted_changes(row: StudyRow) -> bool:
    return row.receipt.get("code_identity", {}).get("tree_state") == "dirty"


def resume_refusal(row: StudyRow, *, live: bool | None, identity: CodeIdentity | None = None, verify_data: bool = False) -> str | None:
    if row.status == "completed":
        return "the study is complete"
    if row.status in ("queued", "running") and live is not False:
        return "the study is still running"
    if uncommitted_changes(row):
        return "the study was launched from a working tree with uncommitted changes and cannot be resumed; launch a fresh study"
    recorded = CodeIdentity(**row.receipt["code_identity"])
    if not recorded.matches(identity or resolve_code_identity()):
        return "the engine or strategy code changed since launch; launch a fresh study"
    if not verify_data:
        return None
    moved = verify_data_snapshot(DataSnapshot.from_dict(row.receipt["data_snapshot"]), sweeps.resolve_data_roots(source="polygon", adjusted=True))
    if moved:
        return f"{len(moved)} data artifact(s) changed since launch ({moved[0]}{', …' if len(moved) > 1 else ''}); launch a fresh study"
    return None


def winner_changes(folds: Sequence[FoldRecord]) -> int:
    """How often the chosen settings moved between consecutive successful folds."""
    hashes = [fold.winner_params_hash for fold in folds if fold.status == "completed" and fold.winner_params_hash]
    return sum(1 for previous, following in pairwise(hashes) if previous != following)


def leader_of(cells: Sequence[CellRow], measure: str, min_trades: int) -> CellRow | None:
    return leader(cells, measure, min_trades=min_trades)  # type: ignore[arg-type]
