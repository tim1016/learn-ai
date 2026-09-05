"""Walk-forward study orchestration against the ephemeral database with a fake engine.

Each fold's training and test sweeps are real Grid Search records owned by
the study; the fake engine answers by (phase, fold, short_window) so the
tests can pin selection, exploratory labelling, fold failure, cancellation,
Finish, and the verdict without the engine.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.grid_search import repository as sweep_repo
from app.research.grid_search.models import CellResult, GridSearchSpec, SearchRow
from app.research.persistence import lifecycle
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.grid import RunSpec, ValueListRange
from app.research.sweep.identity import CodeIdentity
from app.research.walk_forward_study import repository as repo
from app.research.walk_forward_study import service
from app.research.walk_forward_study.models import StudySpec
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.lean_store import seed_store_day

# Month arithmetic is anchored on the requested start (the 1st), and each boundary snaps to a session.
START, END_EXCLUSIVE = date(2025, 1, 1), date(2025, 4, 1)
SESSIONS = expected_sessions(START, date(2025, 3, 31))
# The lake also holds history before the study's start, as a real lake does: the run-up is primed
# from it, so the study's frozen snapshot must reach back before START for fold 0 to run.
PRIOR_SESSIONS = expected_sessions(date(2024, 12, 2), date(2024, 12, 31))


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("lake")
    for day in (*PRIOR_SESSIONS, *SESSIONS):
        seed_store_day(root, "SPY", day, count=120)
    return root


def _spec(*, training_months: int = 1, test_months: int = 1, **grid_overrides) -> StudySpec:
    grid = dict(
        strategy_key="sma_crossover",
        symbol="SPY",
        param_ranges={"short_window": ValueListRange((2.0, 3.0)), "long_window": ValueListRange((5.0,)), "resolution_minutes": ValueListRange((60.0,))},
        start_ms=et_midnight_ms(START),
        end_ms=et_midnight_ms(END_EXCLUSIVE),
        measure="sharpe_ratio",
        min_trades=1,
    )
    grid.update(grid_overrides)
    return StudySpec(grid=GridSearchSpec(**grid), training_months=training_months, test_months=test_months)


# Sharpe by (phase, fold_index, short_window). Fold 0: short=3 wins training and keeps 2/3 of it;
# fold 1: short=2 wins training and keeps a quarter. Median retention 0.458 → "got worse".
SHARPES: dict[tuple[str, int, float], float] = {
    ("train", 0, 2.0): 0.5, ("train", 0, 3.0): 1.5, ("test", 0, 2.0): 3.0, ("test", 0, 3.0): 1.0,
    ("train", 1, 2.0): 2.0, ("train", 1, 3.0): 1.0, ("test", 1, 2.0): 0.5, ("test", 1, 3.0): 2.5,
}


def _fake_factory(sharpes: dict = SHARPES, *, failing: set[tuple[str, int, float]] = frozenset(), seen: list | None = None) -> Callable:
    def factory(row: SearchRow, spec: GridSearchSpec) -> Callable[[RunSpec], CellResult]:
        assert row.owner.kind == "walk_forward" and row.owner.fold_index is not None and row.owner.phase in ("train", "test")
        key_prefix = (row.owner.phase, row.owner.fold_index)

        def execute(candidate: RunSpec) -> CellResult:
            key = (*key_prefix, candidate.params["short_window"])
            if seen is not None:
                seen.append(key)
            if key in failing:
                raise RuntimeError(f"boom {key}")
            return CellResult(params_hash=candidate.params_hash, params=dict(candidate.params), status="completed", total_trades=4, sharpe_ratio=sharpes[key], total_return_pct=1.0, net_profit=10.0)

        return execute

    return factory


async def _launch(lake: Path, **overrides) -> str:
    record = service.prepare_launch(_spec(**overrides), job_id=f"job-{uuid.uuid4().hex[:8]}", roots=[lake])
    return (await service.create(record)).id


# ── Preflight ────────────────────────────────────────────────────────────


def test_preflight_plans_whole_folds_and_admits_on_combinations_times_folds_times_two(lake: Path) -> None:
    pre = service.preflight(_spec(), roots=[lake])

    assert [(f.train_start, f.train_end, f.test_start, f.test_end) for f in pre.folds] == [
        (date(2025, 1, 2), date(2025, 2, 3), date(2025, 2, 3), date(2025, 3, 3)),
        (date(2025, 2, 3), date(2025, 3, 3), date(2025, 3, 3), date(2025, 4, 1)),
    ]
    assert pre.sweep.combinations == 2 and pre.sweep.total_backtests == 8
    # Primed from the lake's history before START rather than carved from the study range.
    assert pre.sweep.run_up.carved_from_range is False and pre.sweep.data_start < START
    assert pre.sweep.estimated_seconds > 0


def test_preflight_refuses_a_range_that_does_not_make_whole_folds_and_names_the_nearest_ends(lake: Path) -> None:
    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.preflight(_spec(end_ms=et_midnight_ms(date(2025, 3, 15))), roots=[lake])

    assert excinfo.value.code == "FOLDS_INVALID"
    assert "2025-03-01" in str(excinfo.value) and "2025-04-01" in str(excinfo.value)


def test_preflight_applies_the_total_backtest_limit_across_folds(lake: Path) -> None:
    ranges = {"short_window": ValueListRange(tuple(float(v) for v in range(2, 52))), "long_window": ValueListRange(tuple(float(v) for v in range(60, 86)))}
    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.preflight(_spec(param_ranges=ranges), roots=[lake])  # 1300 combos × 2 folds × 2 = 5200
    assert excinfo.value.code == "WORKLOAD_LIMIT"


# ── Execute ──────────────────────────────────────────────────────────────


async def test_a_study_sweeps_each_fold_selects_per_fold_winners_and_reaches_a_verdict(conn, lake: Path) -> None:
    study_id = await _launch(lake)
    created = await repo.get_study(conn, study_id)
    assert created is not None and created.status == "queued" and created.expected_backtests == 8
    assert created.receipt["walk_forward"] == {"training_months": 1, "test_months": 1, "step_months": 1, "fold_count": 2, "combinations": 2}
    assert created.receipt["interval_table"]["warmup_policy"] == "uniform_run_up"
    # The frozen snapshot reaches back over the run-up sessions before the study start (review finding).
    assert created.receipt["data_snapshot"]["sessions_ms"][0] < et_midnight_ms(START)
    logs: list[str] = []

    outcome = await asyncio.to_thread(service.execute, study_id, job_id=created.job_id, cell_executor=_fake_factory(), roots=[lake], on_log=logs.append)

    row = await repo.get_study(conn, study_id)
    assert row is not None and row.status == "completed" and outcome.status == "completed"
    assert [fold.status for fold in row.folds] == ["completed", "completed"]
    assert [fold.winner_params["short_window"] for fold in row.folds] == [3.0, 2.0]
    assert [fold.train_sharpe for fold in row.folds] == [1.5, 2.0]
    assert [fold.test_sharpe for fold in row.folds] == [1.0, 0.5]
    assert [round(fold.retention, 4) for fold in row.folds] == [0.6667, 0.25]
    assert row.completed_backtests == 8 and not row.incomplete
    assert row.verdict is not None and row.verdict["label"] == "got worse" and row.verdict["based_on"] == "based on 2 of 2 folds"
    assert service.winner_changes(row.folds) == 1

    # The fold sweeps are owned by the study, invisible in the user history, and the test
    # sweep's non-winner cells are exploratory while the winner's cell is the evidence.
    owned = await sweep_repo.list_searches(conn, owner_kind="walk_forward", owner_id=study_id)
    assert sorted((s.owner.fold_index, s.owner.phase) for s in owned) == [(0, "test"), (0, "train"), (1, "test"), (1, "train")]
    assert all(s.status == "completed" for s in owned)
    # Every fold sweep carries the study's frozen snapshot verbatim: its reads were bound to the study's bytes.
    assert {s.receipt["data_snapshot_digest"] for s in owned} == {row.receipt["data_snapshot_digest"]}
    assert not any(s.id in {o.id for o in owned} for s in await sweep_repo.list_searches(conn, job_id=created.job_id))
    test_cells = {cell.params["short_window"]: cell for cell in await sweep_repo.list_all_cells(conn, row.folds[0].test_search_id)}
    assert test_cells[3.0].exploratory is False and test_cells[2.0].exploratory is True
    # The test sweep's own best cell (short=2, Sharpe 3.0) is hindsight; its leader is the training winner.
    test_sweep = await sweep_repo.get_search(conn, row.folds[0].test_search_id)
    assert test_sweep is not None and test_sweep.leader_params_hash == test_cells[3.0].params_hash
    assert test_sweep.leader_params == test_cells[3.0].params
    train_cells = await sweep_repo.list_all_cells(conn, row.folds[0].train_search_id)
    assert all(cell.exploratory is False for cell in train_cells)
    # Each fold sweep's window is the fold's window.
    windows = {(s.owner.fold_index, s.owner.phase): (s.request["start_ms"], s.request["end_ms"]) for s in owned}
    assert windows[(0, "train")] == (row.folds[0].train_start_ms, row.folds[0].train_end_ms)
    assert windows[(1, "test")] == (row.folds[1].test_start_ms, row.folds[1].test_end_ms)
    assert any("winner" in line for line in logs)

    # Deleting the study takes its sweeps with it.
    assert await repo.delete_study(conn, study_id) is True
    assert await sweep_repo.list_searches(conn, owner_kind="walk_forward", owner_id=study_id) == []


async def test_a_failed_winner_test_run_fails_the_fold_and_the_verdict_cannot_be_judged(conn, lake: Path) -> None:
    study_id = await _launch(lake)

    outcome = await asyncio.to_thread(
        service.execute, study_id, job_id=None, cell_executor=_fake_factory(failing={("test", 0, 3.0)}), roots=[lake]
    )

    row = await repo.get_study(conn, study_id)
    assert row is not None and outcome.status == "completed"
    assert row.folds[0].status == "failed" and "WINNER_TEST_FAILED" in (row.folds[0].failure_reason or "")
    assert row.folds[1].status == "completed"
    assert row.verdict is not None and row.verdict["label"] == "could not be judged"
    assert "1 of 2 folds failed" in row.verdict["reason"]


async def test_a_training_window_with_no_eligible_candidate_fails_the_fold(conn, lake: Path) -> None:
    study_id = await _launch(lake)

    outcome = await asyncio.to_thread(
        service.execute, study_id, job_id=None, cell_executor=_fake_factory(failing={("train", 1, 2.0), ("train", 1, 3.0)}), roots=[lake]
    )

    row = await repo.get_study(conn, study_id)
    assert row is not None and outcome.status == "completed"
    assert row.folds[1].status == "failed" and "NO_ELIGIBLE_CANDIDATE" in (row.folds[1].failure_reason or "")
    assert row.folds[1].test_search_id is None  # the test window is never swept without a winner


async def test_every_fold_failing_fails_the_study(conn, lake: Path) -> None:
    study_id = await _launch(lake)
    failing = {(phase, fold, short) for phase in ("train",) for fold in (0, 1) for short in (2.0, 3.0)}

    outcome = await asyncio.to_thread(service.execute, study_id, job_id=None, cell_executor=_fake_factory(failing=failing), roots=[lake])

    row = await repo.get_study(conn, study_id)
    assert row is not None and outcome.status == "failed" and row.status == "failed"
    assert "every fold failed" in (row.failure_reason or "")


async def test_cancellation_keeps_completed_folds_and_finish_runs_only_the_rest(conn, lake: Path) -> None:
    study_id = await _launch(lake)
    seen: list[tuple] = []

    def cancel_once_fold_zero_is_done() -> None:
        if any(key[:2] == ("test", 0) for key in seen) and any(key[:2] == ("train", 1) for key in seen):
            raise JobCancelled("cancelled")

    with pytest.raises(JobCancelled):
        await asyncio.to_thread(
            service.execute, study_id, job_id="job-a", cell_executor=_fake_factory(seen=seen), cancel_check=cancel_once_fold_zero_is_done, roots=[lake]
        )

    row = await repo.get_study(conn, study_id)
    assert row is not None and row.status == "cancelled" and row.incomplete and row.verdict is None
    assert row.folds[0].status == "completed" and row.folds[1].status == "running"
    assert row.folds[1].train_search_id is not None and row.folds[1].test_search_id is None
    # The interrupted fold's training cells were recorded and the study's count says so.
    assert row.folds[1].recorded_backtests == 2 and row.completed_backtests == 6

    resumed_seen: list[tuple] = []
    outcome = await asyncio.to_thread(service.execute, study_id, job_id="job-b", cell_executor=_fake_factory(seen=resumed_seen), roots=[lake])

    # Fold 0 is not re-run; fold 1's training sweep is Finished (its cells were recorded) and its test window swept.
    assert not any(key[1] == 0 for key in resumed_seen)
    assert outcome.status == "completed"
    finished = await repo.get_study(conn, study_id)
    assert finished is not None and finished.status == "completed" and finished.attempt == 2
    assert [fold.status for fold in finished.folds] == ["completed", "completed"]
    assert finished.verdict is not None and finished.verdict["label"] == "got worse"


async def test_finish_continues_a_fold_whose_training_sweep_completed_before_the_cancel(conn, lake: Path) -> None:
    """Cancelled between a completed training sweep and its test sweep: Finish reads the winner, it does not re-run the sweep."""
    study_id = await _launch(lake)

    def cancel_once_fold_zero_training_is_complete() -> None:
        owned = run_sync(with_connection(sweep_repo.list_searches, owner_kind="walk_forward", owner_id=study_id))
        if any(s.owner.phase == "train" and s.status == "completed" for s in owned) and not any(s.owner.phase == "test" for s in owned):
            raise JobCancelled("cancelled")

    with pytest.raises(JobCancelled):
        await asyncio.to_thread(service.execute, study_id, job_id="job-a", cell_executor=_fake_factory(), cancel_check=cancel_once_fold_zero_training_is_complete, roots=[lake])

    row = await repo.get_study(conn, study_id)
    assert row is not None and row.folds[0].status == "running" and row.folds[0].train_search_id and row.folds[0].test_search_id is None

    resumed_seen: list[tuple] = []
    outcome = await asyncio.to_thread(service.execute, study_id, job_id="job-b", cell_executor=_fake_factory(seen=resumed_seen), roots=[lake])

    assert outcome.status == "completed"
    assert not any(key[:2] == ("train", 0) for key in resumed_seen)  # the completed training sweep was read, not re-run
    finished = await repo.get_study(conn, study_id)
    assert finished is not None and [f.status for f in finished.folds] == ["completed", "completed"]
    assert finished.folds[0].winner_params["short_window"] == 3.0


async def test_a_fold_sweep_is_refused_when_the_study_snapshot_does_not_cover_its_window(conn, lake: Path) -> None:
    """The study's snapshot is the only bytes a fold may read; a window outside it is a refusal, not a re-hash."""
    study_id = await _launch(lake)
    row = await repo.get_study(conn, study_id)
    assert row is not None
    spec = StudySpec.from_request_dict(row.request)
    narrow = service.DataSnapshot.from_dict(row.receipt["data_snapshot"])
    narrow = service.DataSnapshot(**{**narrow.__dict__, "sessions": narrow.sessions[:5]})

    with pytest.raises(service.GridSearchRefusal) as excinfo:
        service.sweeps.prepare_launch(spec.sweep_spec(row.folds[1].test_start_ms, row.folds[1].test_end_ms), job_id=None, roots=[lake], snapshot=narrow)
    assert excinfo.value.code == "SNAPSHOT_COVERAGE"


async def test_progress_counts_the_cells_a_failed_fold_actually_recorded(conn, lake: Path) -> None:
    study_id = await _launch(lake)

    await asyncio.to_thread(service.execute, study_id, job_id=None, cell_executor=_fake_factory(failing={("train", 1, 2.0), ("train", 1, 3.0)}), roots=[lake])

    row = await repo.get_study(conn, study_id)
    assert row is not None
    # Fold 0 ran both sweeps (4 cells); fold 1 recorded its two failed training cells and never swept the test window.
    assert [fold.recorded_backtests for fold in row.folds] == [4, 2] and row.completed_backtests == 6


# ── Presentation ─────────────────────────────────────────────────────────


async def test_status_presentation_and_resume_refusals(conn, lake: Path, monkeypatch) -> None:
    clean = CodeIdentity(git_revision="h", tree_state="clean", source_digest="s" * 64, environment_digest="e" * 64)
    monkeypatch.setattr(service, "resolve_code_identity", lambda: clean)  # what the receipt records
    monkeypatch.setattr(lifecycle, "resolve_code_identity", lambda: clean)  # what Finish compares against
    study_id = await _launch(lake)
    await repo.claim_attempt(conn, study_id, job_id="job-x")
    row = await repo.get_study(conn, study_id)
    assert row is not None

    refusal = lambda **kw: lifecycle.resume_refusal(row, noun="study", unit="fold", **kw)  # noqa: E731
    assert lifecycle.presented_status(row, live=False) == "interrupted"
    assert lifecycle.presented_status(row, live=True) == "running"
    assert lifecycle.presented_status(row, live=None) == "running"
    assert refusal(live=True) == "the study is still running"
    identity = CodeIdentity(**row.receipt["code_identity"])
    assert refusal(live=False, identity=identity) is None
    moved = CodeIdentity(git_revision=identity.git_revision, tree_state=identity.tree_state, source_digest="0" * 64, environment_digest=identity.environment_digest)
    assert "code changed" in (refusal(live=False, identity=moved) or "")
