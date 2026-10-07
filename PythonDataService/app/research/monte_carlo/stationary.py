"""Stationary bootstrap index draws: resample a dependent series in blocks of random length.

Formula: for n observations and a mean block length L >= 1 (p = 1/L), one
  resample of length m is i_1..i_m with i_1 uniform on {0..n-1} and, for
  t > 1, with probability p a new block starts (i_t uniform on {0..n-1}),
  else the block continues, i_t = (i_{t-1} + 1) mod n, wrapping from the last
  observation to the first. L = 1 is the ordinary bootstrap. Every i_t is
  uniform, and Cov*(X*_t, X*_{t+k}) = (1 - p)^k C(k) with C the circular
  autocovariance of the observations, so a resampled mean of m has variance
  (1/m^2)[m C(0) + 2 sum_{k=1}^{m-1} (m - k)(1 - p)^k C(k)].
Reference: D. N. Politis & J. P. Romano, "The Stationary Bootstrap", Journal
  of the American Statistical Association 89(428) (1994) 1303-1313.
Canonical implementation: this file.
Validated against: tests/research/monte_carlo/test_stationary.py (the variance
  above; fixture tests/fixtures/golden/resampling/SB-001 from arch's
  StationaryBootstrap).

Draw order is part of the contract: each chunk of ``CHUNK_REPLICATIONS``
resamples draws its block starts (``rng.integers``) and then its new-block
uniforms (``rng.random``), chunks in order, so a seed fixes every index.
A stored result names a convention for that chunked order, so it draws
through ``stationary_index_chunks``. One ``stationary_indices`` call of more
than ``CHUNK_REPLICATIONS`` resamples is a different seeded draw.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np

#: Resamples drawn per chunk. Part of the draw order: changing it changes every seeded result.
CHUNK_REPLICATIONS = 1_000


def _check(n: int, size: int, mean_block: float, replications: int) -> None:
    if n < 1 or size < 1 or replications < 1:
        raise ValueError(f"n, size and replications must be at least 1 (got {n}, {size}, {replications})")
    if not (math.isfinite(mean_block) and mean_block >= 1):
        raise ValueError(f"mean_block must be a finite number of at least 1 (got {mean_block})")


def stationary_indices(n: int, *, size: int, mean_block: float, replications: int, rng: np.random.Generator) -> np.ndarray:
    """One chunk's draw: ``replications`` resamples of length ``size`` over ``n`` observations, as int64 row indices."""
    _check(n, size, mean_block, replications)
    starts = rng.integers(0, n, size=(replications, size))
    new_block = rng.random((replications, size)) < 1.0 / mean_block
    new_block[:, 0] = True
    position = np.arange(size)
    block_position = np.maximum.accumulate(np.where(new_block, position, 0), axis=1)
    return (np.take_along_axis(starts, block_position, axis=1) + (position - block_position)) % n


def stationary_index_chunks(n: int, *, size: int, mean_block: float, replications: int, rng: np.random.Generator) -> Iterator[np.ndarray]:
    """``replications`` resamples, one ``stationary_indices`` draw of ``CHUNK_REPLICATIONS`` rows at a time, so a long series never holds every index at once.

    Bad arguments are refused here, before any chunk is taken. Each chunk is drawn as it is taken, so
    nothing else may draw from ``rng`` until the last one has been.
    """
    _check(n, size, mean_block, replications)
    counts = [min(CHUNK_REPLICATIONS, replications - done) for done in range(0, replications, CHUNK_REPLICATIONS)]
    return (stationary_indices(n, size=size, mean_block=mean_block, replications=count, rng=rng) for count in counts)
