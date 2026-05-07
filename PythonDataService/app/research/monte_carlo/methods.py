"""Monte Carlo path-generation primitives over a sequence of trade returns.

Two methods, both deterministic given a seed:

  * **Reshuffle** — permutation of the input array. The output is a
    *rearrangement* of the same multiset of returns; mean and variance
    of the realised distribution are unchanged. Tests path dependence
    only (does the *order* matter?).
  * **Resample** — draw with replacement. The output may contain
    duplicates of any input return. Tests sensitivity to the specific
    realisation of the strategy's return distribution; useful for
    forward projection (``size > len(returns)``) where the question
    is "what could the next N trades look like, drawn from the same
    distribution?".

Both functions return a ``numpy.ndarray`` of the requested size. The
caller is responsible for compounding into an equity curve — this
module is the random-draw layer, not the equity-math layer.

Determinism contract: same ``rng.bit_generator.state`` (i.e. same
seed) → same output. Tests pin this by reseeding between calls.
"""

from __future__ import annotations

import numpy as np


def reshuffle_trades(returns: np.ndarray, *, rng: np.random.Generator) -> np.ndarray:
    """Return a permutation of ``returns`` — same multiset, different order.

    ``rng.permutation`` returns a copy; the input array is not mutated.
    The output length equals the input length (reshuffle is by
    definition a rearrangement, not a sample).

    Use the per-simulation rng substream so each simulation in a
    Monte Carlo batch gets a distinct order even though the batch
    shares a top-level seed.
    """
    if returns.size == 0:
        return returns.copy()
    return rng.permutation(returns)


def resample_trades(
    returns: np.ndarray,
    *,
    size: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample ``size`` returns with replacement from ``returns``.

    ``size`` may exceed ``len(returns)`` to project forward beyond the
    historical trade count — the architecture spec calls this
    "forward N-trade projection" (Feature 5). When ``size <= len(returns)``,
    this is the standard bootstrap; duplicates are still permitted.
    """
    if returns.size == 0:
        if size == 0:
            return returns.copy()
        raise ValueError(
            "Cannot resample from an empty returns array — caller should "
            "have rejected this earlier (no trades on the parent run)."
        )
    if size < 0:
        raise ValueError(f"size must be non-negative (got {size})")
    indices = rng.integers(low=0, high=returns.size, size=size)
    return returns[indices]


def equity_curve(
    initial_equity: float,
    returns: np.ndarray,
) -> np.ndarray:
    """Compound a return sequence into an equity curve, ``initial_equity``-anchored.

    Output length = ``len(returns) + 1``: the first element is
    ``initial_equity`` (before any trade) and each subsequent element
    is ``previous * (1 + return_i)``. Matches the per-trade compounding
    used by ``app/engine/results/statistics.py`` so the simulated
    curves are directly comparable to the parent run's reported
    equity progression.
    """
    if returns.size == 0:
        return np.array([initial_equity], dtype=float)
    multipliers = np.cumprod(1.0 + returns.astype(float))
    return np.concatenate(([initial_equity], initial_equity * multipliers))


def max_drawdown(equity: np.ndarray) -> float:
    """Peak-to-trough drawdown of an equity curve, as a positive fraction.

    Identical formula to ``app/engine/results/statistics.py::_max_drawdown``
    so simulated drawdowns are on the same scale as the parent run's
    ``max_drawdown_pct``. Returns 0.0 for an empty or single-point curve.
    """
    if equity.size < 2:
        return 0.0
    running_peak = np.maximum.accumulate(equity)
    # Avoid division-by-zero for the (degenerate) all-zero curve;
    # the engine's analogue does the same.
    safe_peak = np.where(running_peak > 0, running_peak, 1.0)
    drawdowns = (running_peak - equity) / safe_peak
    return float(drawdowns.max())


def max_losing_streak(returns: np.ndarray) -> int:
    """Longest run of consecutive losing trades (``return < 0``).

    Wins and break-even trades both terminate a streak. Returns 0
    when there are no losing trades.
    """
    if returns.size == 0:
        return 0
    losing = returns < 0
    longest = 0
    current = 0
    for x in losing:
        if x:
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return int(longest)
