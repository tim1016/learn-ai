"""Grid Search execution: every cell once, bounded concurrency, chunked persistence.

Mirrors the Recency Chart runner's injected-dependency shape so this module
has no dependency on the engine HTTP layer or the database: ``execute_cell``
turns one candidate into a :class:`CellResult`; ``persist`` durably writes a
finished batch. The batching, per-cell isolation and the raise-only
cancellation contract (polled before every batch and once more after the
final batch drains, review F12) live in ``app.research.sweep.concurrency``.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Grid and
  workload", "Lifecycle and persistence".
Canonical implementation: this file.
Validated against: tests/research/grid_search/test_runner.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.research.grid_search.models import CellResult
from app.research.sweep.concurrency import MAX_CONCURRENT_RUNS, run_batched
from app.research.sweep.grid import RunSpec


@dataclass(frozen=True)
class GridRunSummary:
    expected_cells: int
    executed_cells: int
    completed_cells: int
    failed_cells: int
    results: list[CellResult] = field(default_factory=list)


def run_grid(
    candidates: Iterable[RunSpec],
    *,
    expected_cells: int,
    execute_cell: Callable[[RunSpec], CellResult],
    persist: Callable[[list[CellResult]], None],
    cancel_check: Callable[[], object] = lambda: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_cell_failed: Callable[[RunSpec, str], None] = lambda spec, message: None,
    skip_params_hashes: frozenset[str] = frozenset(),
    max_workers: int = MAX_CONCURRENT_RUNS,
) -> GridRunSummary:
    """Execute every candidate not already persisted, persisting each batch as it drains.

    ``skip_params_hashes`` is how Finish re-runs only the missing cells: a
    cell with a row is simply not run again. Progress counts skipped cells
    as done so the bar reflects the whole search, not the remainder.
    """
    done = len(skip_params_hashes)
    results: list[CellResult] = []
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

    remaining = (spec for spec in candidates if spec.params_hash not in skip_params_hashes)
    for batch in run_batched(remaining, _executed, max_workers=max_workers, cancel_check=cancel_check, on_error=_failed):
        done += len(batch)
        on_progress(done, expected_cells)
        persist(batch)
        results.extend(batch)

    return GridRunSummary(
        expected_cells=expected_cells,
        executed_cells=len(results),
        completed_cells=sum(1 for r in results if r.status == "completed"),
        failed_cells=sum(1 for r in results if r.status == "failed"),
        results=results,
    )
