"""Price-independent decay of an indicator's initialization error (#2857).

Formula: single stage w[n] = (1 - alpha)**n; chained stage
  v[n] = alpha_out * w[n] + (1 - alpha_out) * v[n-1].
Reference: the EMA and Wilder recurrences in the pinned LEAN indicators;
  platform convergence policy in issue #2857.
Canonical implementation: this file.
Validated against: tests/engine/indicators/test_convergence.py.

The weights bound the recursive state, not every nonlinear output on every
price path. RSI and ADX apply nonlinear ratios; Supertrend also latches a
direction. Their output convergence needs the separate twin-run evidence.
"""

from __future__ import annotations

import math
from functools import lru_cache

CONVERGENCE_TOLERANCE = 0.001


def require_tolerance(tolerance: float) -> None:
    if not 0 < tolerance < 1:
        raise ValueError("convergence tolerance must be strictly between zero and one")


@lru_cache(maxsize=4096)
def decay_steps(alpha: float, tolerance: float = CONVERGENCE_TOLERANCE) -> int:
    """Updates after seeding needed to reduce its weight to ``tolerance``."""
    require_tolerance(tolerance)
    if not 0 < alpha <= 1:
        raise ValueError("smoothing factor must be greater than zero and at most one")
    if alpha == 1:
        return 0
    return math.ceil(math.log(tolerance) / math.log1p(-alpha))


@lru_cache(maxsize=4096)
def cascade_steps(
    input_alpha: float, output_alpha: float, tolerance: float = CONVERGENCE_TOLERANCE,
) -> int:
    """Conservative two-stage decay, with both weights one at readiness.

    Using the slowest input stage bounds MACD's fast and slow EMA seed
    contributions separately. ADX uses two Wilder stages. Beginning both at
    one deliberately ignores decay already earned before full readiness.
    """
    require_tolerance(tolerance)
    if not 0 < input_alpha <= 1 or not 0 < output_alpha <= 1:
        raise ValueError("smoothing factors must be greater than zero and at most one")
    source = output = 1.0
    steps = 0
    while max(source, output) > tolerance:
        source *= 1 - input_alpha
        output = output_alpha * source + (1 - output_alpha) * output
        steps += 1
    return steps
