"""Sequential execution shared by every sweep runner.

``run_each`` is the one loop that carries the cancellation contract — poll
before every item and once more after the last item completes, so a
cancellation that arrives while the last item executes is never lost
(issue #1928, review F12) — and per-item isolation, so a failing item is
returned as its own result rather than aborting the run. The Recency Chart
runner established the contract (PRD #1577, design spec D11); Grid Search
(PRD #1926) and Walk-Forward (PRD #1925) share this module rather than each
transcribing the loop.

One backtest runs at a time, by measurement rather than by default. On the
data-service container (2 CPUs, 2 GiB) with SPY minute bars over two years
(~194k bars per cell), one engine run peaks near 480 MB of resident memory
above the idle service, and the engine's per-bar loop holds the GIL: a second
thread added ~415 MB and no throughput (four cells took 24.5 s on one thread,
27.9 s on two, 38 s on four), and at eight the kernel's memory cgroup killed
the service mid-search and left its record reading ``running`` for a day
(2026-09-05). A thread count cannot lift the GIL, so this module keeps no
pool; more throughput would take a process pool, which re-loads the bars in
every process and so costs memory first.

That limit is no longer this module's to enforce. Running cells one at a time
only ever counted the cells of one sweep; ``app.engine.run_gate`` is the
canonical enforcer across sweeps, Strategy Lab tabs and the sync endpoint
alike (#1957). This loop stays sequential because a pool would buy nothing
here, not because it is what keeps concurrent runs apart.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator


def run_each[T, R](
    items: Iterable[T],
    execute: Callable[[T], R],
    *,
    cancel_check: Callable[[], object] = lambda: None,
    on_error: Callable[[T, Exception], R],
) -> Iterator[R]:
    """Execute ``items`` one at a time, yielding each result as it completes.

    ``cancel_check`` is raise-only: its return value is ignored and a raise
    propagates out of this generator. It is polled before every item and once
    more after the last item has been yielded, so the caller persists a
    finished item before a cancellation that arrived during it is acknowledged.
    ``on_error`` turns one item's exception into that item's result — never
    dropped, never fatal to the run.
    """
    for item in items:
        cancel_check()
        try:
            result = execute(item)
        except Exception as exc:  # per-item isolation
            result = on_error(item, exc)
        yield result
    cancel_check()
