"""Grid Search execution: every cell once, bounded concurrency, chunked persistence.

Mirrors the Recency Chart runner's injected-dependency shape so this module
has no dependency on the engine HTTP layer or the database: ``execute_cell``
turns one candidate into a :class:`CellResult`; ``persist`` durably writes a
finished batch; ``cancel_check`` is raise-only and polled before every batch
and once more after the final batch drains, so a cancellation that arrives
while the last batch executes is never lost (review F12). A failing cell is
recorded as a failed cell — never dropped, never fatal to the batch.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1926 "Grid and
  workload", "Lifecycle and persistence"; runner precedent
  app/research/recency/runner.py.
Canonical implementation: this file.
Validated against: tests/research/grid_search/test_runner.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from app.research.grid_search.models import CellResult
from app.research.sweep.concurrency import MAX_CONCURRENT_RUNS, batches
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

    def _guarded(spec: RunSpec) -> CellResult:
        try:
            return execute_cell(spec)
        except Exception as exc:  # per-cell isolation: recorded, never dropped
            return CellResult(params_hash=spec.params_hash, params=dict(spec.params), status="failed", error=str(exc))

    remaining = (spec for spec in candidates if spec.params_hash not in skip_params_hashes)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for batch in batches(remaining, max_workers):
            cancel_check()
            futures = {pool.submit(_guarded, spec): spec for spec in batch}
            finished: list[CellResult] = []
            for future in as_completed(futures):
                result = future.result()
                if result.status == "failed":
                    on_cell_failed(futures[future], result.error or "cell failed")
                finished.append(result)
                done += 1
                on_progress(done, expected_cells)
            persist(finished)
            results.extend(finished)

    # The batch-head poll cannot observe a cancellation that arrived while the
    # final batch executed; the batch is drained and durable, so acknowledging
    # the cancellation now loses nothing (issue #1928 / review F12).
    cancel_check()

    return GridRunSummary(
        expected_cells=expected_cells,
        executed_cells=len(results),
        completed_cells=sum(1 for r in results if r.status == "completed"),
        failed_cells=sum(1 for r in results if r.status == "failed"),
        results=results,
    )
