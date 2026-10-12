"""Grid Search execution: every cell once, one at a time, persisted as it completes.

Mirrors the Recency Chart runner's injected-dependency shape so this module
has no dependency on the engine HTTP layer or the database: ``execute_cell``
turns one candidate into a :class:`CellResult`; ``persist`` durably writes a
finished cell. The per-cell isolation and the raise-only cancellation
contract (polled before every cell and once more after the last cell
completes, review F12) live in ``app.research.sweep.execution``.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Grid and
  workload", "Lifecycle and persistence".
Canonical implementation: this file.
Validated against: tests/research/grid_search/test_runner.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from app.research.grid_search.models import CellResult
from app.research.sweep.execution import run_each
from app.research.sweep.grid import RunSpec

#: What the runtime's dispatch delivers (#3049): each arrival as its
#: ``(candidate, result)`` pair, in arrival order — this loop persists and
#: counts as it consumes, so arrival order is free and logical order is the
#: ranking's. Defined here, beside the parameter it names; the dispatch
#: module cannot be imported at this module's top (it would cycle through
#: the engine adapter back into this package's service).
CellDispatch = Callable[[Iterable[RunSpec]], Iterator[tuple[RunSpec, CellResult]]]


@dataclass(frozen=True)
class GridRunSummary:
    expected_cells: int
    executed_cells: int
    completed_cells: int
    failed_cells: int


def run_grid(
    candidates: Iterable[RunSpec],
    *,
    expected_cells: int,
    persist: Callable[[CellResult], None],
    cancel_check: Callable[[], object] = lambda: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_cell_failed: Callable[[RunSpec, str], None] = lambda spec, message: None,
    skip_params_hashes: frozenset[str] = frozenset(),
    execute_cell: Callable[[RunSpec], CellResult] | None = None,
    dispatch: CellDispatch | None = None,
) -> GridRunSummary:
    """Execute every candidate not already persisted, persisting each cell as it completes.

    ``skip_params_hashes`` is how Finish re-runs only the missing cells: a
    cell with a row is simply not run again. Progress counts skipped cells
    as done so the bar reflects the whole search, not the remainder. A cell is
    counted and let go once persisted: each carries its daily equity, and a
    full search of them is not held in memory.

    Exactly one executor is given: ``execute_cell`` runs cells in this
    process, one at a time (the local mode and tests); ``dispatch`` (#3049)
    runs them through the research runtime's workers — cells overlap, each
    arrival is persisted as it is consumed, and the counts, skip set and
    cancellation contract are identical. Arrival order is free; the
    completion timestamps differ, the ranked results never do.
    """
    if (execute_cell is None) == (dispatch is None):
        raise ValueError("run_grid takes exactly one executor: execute_cell or dispatch")
    done = len(skip_params_hashes)
    executed = completed = 0
    on_progress(done, expected_cells)

    def _failed(spec: RunSpec, exc: Exception) -> CellResult:
        # The failure callback receives the candidate that was executed, not a reconstruction of it.
        on_cell_failed(spec, str(exc))
        return CellResult(params_hash=spec.params_hash, params=dict(spec.params), status="failed", error=str(exc))

    def _executed(spec: RunSpec) -> CellResult:
        result = execute_cell(spec)
        if result.status == "failed":
            on_cell_failed(spec, result.error or "cell failed")
        return result

    def _counted(result: CellResult) -> None:
        nonlocal done, executed, completed
        done += 1
        on_progress(done, expected_cells)
        persist(result)
        executed += 1
        completed += result.status == "completed"

    remaining = (spec for spec in candidates if spec.params_hash not in skip_params_hashes)
    if dispatch is not None:
        for spec, result in dispatch(remaining):
            if result.status == "failed":
                on_cell_failed(spec, result.error or "cell failed")
            _counted(result)
        # The serial loop's last poll (#1928 review F12): a cancel that lands
        # while the final cell executes is observed once more after it
        # persists, so the search records cancelled — never a silent
        # completed over an acknowledged cancellation.
        cancel_check()
        return GridRunSummary(
            expected_cells=expected_cells, executed_cells=executed, completed_cells=completed, failed_cells=executed - completed
        )
    for result in run_each(remaining, _executed, cancel_check=cancel_check, on_error=_failed):
        _counted(result)

    return GridRunSummary(expected_cells=expected_cells, executed_cells=executed, completed_cells=completed, failed_cells=executed - completed)
