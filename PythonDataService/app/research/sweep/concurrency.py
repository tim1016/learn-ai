"""Bounded-concurrency vocabulary shared by every sweep runner.

``MAX_CONCURRENT_RUNS`` is the number of backtests in flight at once;
``batches`` feeds a lazily expanded grid to a pool without materializing it;
``run_batched`` is the one loop that carries the cancellation contract —
poll before every batch and once more after the final batch drains, so a
cancellation that arrives while the last batch executes is never lost
(issue #1928, review F12) — and per-item isolation, so a failing item is
returned as its own result rather than aborting its batch. The Recency
Chart runner established the figure and the batching (PRD #1577, design
spec D11); Grid Search (PRD #1926) and Walk-Forward (PRD #1925) share this
module rather than each transcribing the loop. Raise the figure from a
measurement, not a guess.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice

MAX_CONCURRENT_RUNS = 8


def batches[T](items: Iterator[T], size: int) -> Iterator[list[T]]:
    """Yield ``items`` in consecutive lists of at most ``size``."""
    it = iter(items)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


def run_batched[T, R](
    items: Iterable[T],
    execute: Callable[[T], R],
    *,
    max_workers: int = MAX_CONCURRENT_RUNS,
    cancel_check: Callable[[], object] = lambda: None,
    on_error: Callable[[T, Exception], R],
) -> Iterator[list[R]]:
    """Execute ``items`` ``max_workers`` at a time, yielding each drained batch.

    ``cancel_check`` is raise-only: its return value is ignored and a raise
    propagates out of this generator (after the current batch has drained
    and been yielded, so the caller can persist it first). ``on_error``
    turns one item's exception into that item's result — never dropped,
    never fatal to the batch.
    """

    def _guarded(item: T) -> R:
        try:
            return execute(item)
        except Exception as exc:  # per-item isolation
            return on_error(item, exc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for batch in batches(iter(items), max_workers):
            cancel_check()
            futures = [pool.submit(_guarded, item) for item in batch]
            yield [future.result() for future in as_completed(futures)]

    # The batch-head poll cannot observe a cancellation that arrived while the
    # final batch executed; that batch was drained and yielded, so nothing is
    # lost by acknowledging the cancellation now.
    cancel_check()
