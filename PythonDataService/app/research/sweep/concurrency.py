"""Bounded-concurrency vocabulary shared by every sweep runner.

``MAX_CONCURRENT_RUNS`` is the number of backtests in flight at once and
``batches`` is how a lazily expanded grid is fed to a pool without ever
materializing the whole grid: at most one batch of specs is pulled from the
iterator at a time. The Recency Chart runner established both (PRD #1577,
design spec D11); Grid Search (PRD #1926) and Walk-Forward (PRD #1925)
share the figure and the batching rather than choosing their own, so the
machine is shared politely by exactly one policy. Raise the figure from a
measurement, not a guess.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice
from typing import TypeVar

MAX_CONCURRENT_RUNS = 8

T = TypeVar("T")


def batches(items: Iterator[T], size: int) -> Iterator[list[T]]:
    """Yield ``items`` in consecutive lists of at most ``size``."""
    it = iter(items)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch
