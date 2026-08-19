"""Shared percentile utility retained by Alpaca SQLite qualification evidence."""

from __future__ import annotations

import math
from collections.abc import Sequence


def nist_r6_percentile(values: Sequence[int], percentile: int) -> float:
    """Return the NIST R6 latency percentile.

    Formula: ``r = p(n + 1) = k + d`` and
    ``Q_p = x_[k] + d(x_[k+1] - x_[k])``.
    Reference: NIST/SEMATECH e-Handbook §7.2.6.2, “Percentiles”,
    https://itl.nist.gov/div898/handbook/prc/section2/prc262.htm.
    """

    if not values:
        raise ValueError("a percentile requires at least one latency sample")
    if not 1 <= percentile <= 100:
        raise ValueError("percentile must be within 1..100")
    if any(value < 0 for value in values):
        raise ValueError("latency samples must be nonnegative")
    ordered = sorted(values)
    rank = percentile * (len(ordered) + 1) / 100
    if rank <= 1:
        return float(ordered[0])
    if rank >= len(ordered):
        return float(ordered[-1])
    lower_rank = math.floor(rank)
    fractional = rank - lower_rank
    lower = ordered[lower_rank - 1]
    upper = ordered[lower_rank]
    return lower + fractional * (upper - lower)


__all__ = ["nist_r6_percentile"]
