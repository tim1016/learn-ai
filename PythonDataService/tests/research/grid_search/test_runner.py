"""Grid Search runner (PRD #1926 "Testing decisions — Runner")."""

from __future__ import annotations

import threading

import pytest

from app.research.grid_search.models import CellResult
from app.research.grid_search.runner import run_grid
from app.research.sweep.grid import StrategyGridConfig, ValueListRange, expand_grid, grid_size


def _grid(*values: float) -> tuple[list, int]:
    config = StrategyGridConfig(strategy_key="sma_crossover", param_ranges={"short_window": ValueListRange(values)})
    return list(expand_grid([config], ["SPY"])), grid_size([config], ["SPY"])


def _ok(spec) -> CellResult:
    return CellResult(params_hash=spec.params_hash, params=dict(spec.params), status="completed", total_trades=3, sharpe_ratio=spec.params["short_window"])


def test_every_combination_runs_exactly_once_and_is_persisted_as_batches_drain() -> None:
    candidates, expected = _grid(1, 2, 3, 4, 5)
    executed: list[str] = []
    persisted: list[list[CellResult]] = []
    lock = threading.Lock()

    def execute(spec) -> CellResult:
        with lock:
            executed.append(spec.params_hash)
        return _ok(spec)

    summary = run_grid(candidates, expected_cells=expected, execute_cell=execute, persist=persisted.append, max_workers=2)

    assert sorted(executed) == sorted(spec.params_hash for spec in candidates)
    assert len(executed) == len(set(executed)) == 5
    assert [len(chunk) for chunk in persisted] == [2, 2, 1]
    assert (summary.executed_cells, summary.completed_cells, summary.failed_cells) == (5, 5, 0)


def test_a_failing_cell_is_recorded_and_does_not_abort_the_batch() -> None:
    candidates, expected = _grid(1, 2, 3)
    failures: list[tuple[str, str]] = []

    def execute(spec) -> CellResult:
        if spec.params["short_window"] == 2:
            raise RuntimeError("engine exploded")
        return _ok(spec)

    summary = run_grid(
        candidates,
        expected_cells=expected,
        execute_cell=execute,
        persist=lambda chunk: None,
        on_cell_failed=lambda spec, message: failures.append((spec.params_hash, message)),
    )

    failed = [r for r in summary.results if r.status == "failed"]
    assert len(failed) == 1 and failed[0].error == "engine exploded"
    assert summary.completed_cells == 2 and summary.failed_cells == 1
    assert failures == [(failed[0].params_hash, "engine exploded")]


def test_finish_skips_cells_that_already_have_rows_and_counts_them_as_done() -> None:
    candidates, expected = _grid(1, 2, 3, 4)
    already = frozenset(spec.params_hash for spec in candidates[:2])
    progress: list[tuple[int, int]] = []

    summary = run_grid(
        candidates,
        expected_cells=expected,
        execute_cell=_ok,
        persist=lambda chunk: None,
        skip_params_hashes=already,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert summary.executed_cells == 2
    assert progress[0] == (2, 4) and progress[-1] == (4, 4)


def test_a_cancellation_during_the_final_batch_is_observed_after_it_drains() -> None:
    """Regression for the lost-cancel defect (issue #1928, review F12)."""

    class _Cancelled(Exception):
        pass

    candidates, expected = _grid(1)  # a single batch, so no batch-head poll can see the cancel
    cancelled = threading.Event()
    persisted: list[list[CellResult]] = []

    def execute(spec) -> CellResult:
        cancelled.set()  # the user cancels while the only cell executes
        return _ok(spec)

    def cancel_check() -> None:
        if cancelled.is_set():
            raise _Cancelled()

    with pytest.raises(_Cancelled):
        run_grid(candidates, expected_cells=expected, execute_cell=execute, persist=persisted.append, cancel_check=cancel_check)

    # The finished batch was persisted before the cancellation was acknowledged.
    assert len(persisted) == 1 and persisted[0][0].status == "completed"


def test_cancel_check_return_value_is_ignored() -> None:
    candidates, expected = _grid(1, 2)

    summary = run_grid(candidates, expected_cells=expected, execute_cell=_ok, persist=lambda chunk: None, cancel_check=lambda: True)

    assert summary.executed_cells == 2
