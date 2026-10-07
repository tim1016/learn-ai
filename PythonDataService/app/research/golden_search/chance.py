"""How much of a Golden Search result could be chance: the session series and path statistics every reading resamples (#2932).

Formula: a detail run's sessions s = 1..n carry its daily return r_s
  (r_1 = E_1 / capital - 1, r_s = E_s / E_{s-1} - 1) and x_s, the trades that
  exited in session s. A session the run has no equity point for is not in
  the series: its move is in the next session's return, as in the engine's
  own daily returns. A resample is a stationary-bootstrap draw of m session
  indices (``monte_carlo/stationary.py``, mean block MEAN_BLOCK_SESSIONS); two
  runs over the same sessions take the same indices, so they stay paired. On
  a resampled path:
  * net return = prod(1 + r) - 1, and net profit = capital x net return;
  * Sharpe = mean(r) / sd(r) x sqrt(252), sd over m - 1; undefined on fewer
    than two sessions or when every r is equal;
  * worst fall = max_t (peak_t - e_t) / peak_t over e_0 = 1,
    e_t = prod_{u<=t}(1 + r_u): the starting capital and the session closes
    only, so a fall inside a day can be deeper;
  * trades = sum of x.
  An undefined statistic is NaN, and so is one that overflows: never an
  infinity. NaN loses every comparison, so it never counts as beating
  anything, as money made, or as inside a ceiling.
  The seed is the first eight bytes of the SHA-256 of the canonical JSON of
  the parts a reading names (the frozen plan's hash, the points, the window,
  the reading), so no one chooses it.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2932 "What a
  resampled path is scored on"; Politis & Romano (1994), as
  ``monte_carlo/stationary.py``.
Canonical implementation: app/engine/results/statistics.py for one path's
  daily returns, Sharpe and drawdown. This file is their row-wise form over
  thousands of resampled paths at once, which the engine's per-value loops
  would take seconds a reading to score.
Validated against: tests/research/golden_search/test_chance.py (the engine's
  ``_daily_returns``, ``_sharpe`` and ``_max_drawdown``, ``atol=1e-12``; and
  hand-computed paths).

Two differences from the engine's own figures:
  * its worst fall reads every bar and starts its peak at the first bar, not
    at the starting capital. It is usually the deeper of the two, but a run
    already below its starting capital on the first bar has that first loss
    counted here and not there;
  * its Sharpe is undefined when the sd it computes is exactly 0, which
    rounding can miss on a constant non-zero series. Here "every return
    equal" is tested directly. The two agree on a flat run and whenever the
    returns differ.

Only a detail run can be read. Every run keeps its daily curve, but the trades
that exited in each session come from the trade list, which a detail run
alone keeps.

The readings inform. Selection, eligibility, the recommendation and each
final-test outcome do not read them.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from app.engine.results.statistics import TRADING_DAYS_PER_YEAR
from app.research.golden_search.protocol import canonical_json
from app.research.sweep.ranking import RankingMeasure
from app.utils.session_anchors import et_date_at_ms

# The owner's decisions on #2932. They fix every stored number, so each reading carries them (``reading_basis``).
CONVENTION = "stationary-bootstrap/v1"
REPLICATIONS = 10_000
MEAN_BLOCK_SESSIONS = 5.0
#: A share at or above this reads as clear; at or below one minus it, as clear the other way.
CLEAR = 0.95
#: The pass rate a final test should give a fit as good as its development result.
REHEARSAL_TARGET = 0.80
#: Below this many sessions a resample says nothing worth reading.
MIN_SESSIONS = 20

NO_TRADE_LIST = "The run kept no trade list, so the trades that exited in each session cannot be counted. A daily curve alone does not record them."
NOT_POSITIVE_CAPITAL = "The run's starting capital is not a positive amount, so returns cannot be measured against it."
NOT_FINITE = "The run's daily equity holds a value that is not a finite number."
REACHED_ZERO = "The account fell to zero or below, so its returns cannot be resampled."
OUT_OF_ORDER = "The run's daily equity has two points on one day, or days out of order."
UNPLACED_EXIT = "A trade exited on a day with no equity point, so trades cannot be counted by session."


def reading_basis() -> dict[str, Any]:
    """What every stored reading carries beside its numbers: the method that drew them and the thresholds they are judged by.

    A later change of either then cannot pass for a change in the evidence.
    """
    return {
        "convention": CONVENTION,
        "replications": REPLICATIONS,
        "mean_block_sessions": MEAN_BLOCK_SESSIONS,
        "clear": CLEAR,
        "rehearsal_target": REHEARSAL_TARGET,
    }


@dataclass(frozen=True, eq=False)
class SessionSeries:
    """One detail run, session by session. Its arrays are read-only."""

    # Each session's close, int64 ms UTC. Two runs are paired when these are equal.
    session_ms: tuple[int, ...]
    returns: np.ndarray
    # Trades that exited in each session.
    exits: np.ndarray
    capital: float


def session_series(detail: Mapping[str, Any] | None) -> SessionSeries | str:
    """A detail run's sessions, or why they cannot be resampled.

    ``None`` is a run with no detail payload: one that was not a detail run, that failed, or that was never
    evaluated. A caller that knows which should say so before asking here.
    """
    if detail is None:
        return NO_TRADE_LIST
    capital = float(detail["initial_cash"])
    if not (math.isfinite(capital) and capital > 0):
        return NOT_POSITIVE_CAPITAL
    daily = [(int(ms), float(equity)) for ms, equity in detail["daily_equity"]]
    if len(daily) < MIN_SESSIONS:
        return f"The run covers {len(daily)} session{'' if len(daily) == 1 else 's'}; resampling needs at least {MIN_SESSIONS}."
    equity = np.array([capital, *(value for _, value in daily)], dtype=np.float64)
    if not np.isfinite(equity).all():
        return NOT_FINITE
    # Refused at the last session too: a resample can put that session anywhere on a path.
    if (equity <= 0).any():
        return REACHED_ZERO
    days = [et_date_at_ms(ms) for ms, _ in daily]
    if any(later <= earlier for earlier, later in pairwise(days)):
        return OUT_OF_ORDER
    position = {day: index for index, day in enumerate(days)}
    exits = np.zeros(len(daily), dtype=np.int64)
    for trade in detail["trades"]:
        index = position.get(et_date_at_ms(int(trade["exit_ms"])))
        if index is None:
            return UNPLACED_EXIT
        exits[index] += 1
    returns = equity[1:] / equity[:-1] - 1.0
    returns.flags.writeable = exits.flags.writeable = False
    return SessionSeries(session_ms=tuple(ms for ms, _ in daily), returns=returns, exits=exits, capital=capital)


def _defined(values: np.ndarray) -> np.ndarray:
    """``values`` with every infinity turned to NaN, the one marker for undefined."""
    return np.where(np.isfinite(values), values, np.nan)


def path_net_return(returns: np.ndarray) -> np.ndarray:
    """Each row's compounded return."""
    with np.errstate(over="ignore", invalid="ignore"):
        return _defined(np.prod(1.0 + returns, axis=1) - 1.0)


def path_sharpe(returns: np.ndarray) -> np.ndarray:
    """Each row's annualized Sharpe; NaN on fewer than two sessions or where every return in the row is equal."""
    if returns.shape[1] < 2:
        return np.full(returns.shape[0], np.nan)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        deviation = returns.std(axis=1, ddof=1)
        sharpe = returns.mean(axis=1) / deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
    spread = returns.max(axis=1) > returns.min(axis=1)
    return np.where(spread & np.isfinite(deviation) & np.isfinite(sharpe), sharpe, np.nan)


def path_worst_fall(returns: np.ndarray) -> np.ndarray:
    """Each row's largest fall from a running peak, as a fraction of that peak, from a start of 1."""
    with np.errstate(over="ignore", invalid="ignore"):
        equity = np.concatenate([np.ones((returns.shape[0], 1)), np.cumprod(1.0 + returns, axis=1)], axis=1)
        peak = np.maximum.accumulate(equity, axis=1)
        return _defined(((peak - equity) / peak).max(axis=1))


def path_objective(returns: np.ndarray, objective: RankingMeasure, capital: float) -> np.ndarray:
    """Each row's value of the plan's objective; NaN where it is undefined."""
    if objective == "sharpe_ratio":
        return path_sharpe(returns)
    net = path_net_return(returns)
    if objective == "total_return_pct":
        return net
    if objective == "net_profit":
        return capital * net
    raise ValueError(f"the objective {objective!r} has no resampled form")


def seed_for(*parts: object) -> int:
    """The seed a reading draws with: fixed by what the reading is of, never supplied."""
    return int.from_bytes(hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).digest()[:8], "big")
