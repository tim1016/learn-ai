"""``run_monte_carlo`` — load parent run, simulate N paths, aggregate.

Loads the parent ``RunLedger`` + ``BacktestRunResult`` from disk,
extracts the per-trade ``pnl_pct`` array, runs ``simulation_count``
paths via the requested method (``reshuffle`` or ``resample``),
compounds each path into an equity curve anchored at the parent run's
``initial_cash``, and aggregates across simulations.

Three families of aggregations:

  * **Equity bands** — at each trade index, compute the 5th / 50th /
    95th percentile of equity across simulations. The output series
    is a fan that the UI can render as a shaded band.
  * **Scalar quantile dicts** — max drawdown, terminal PnL, max
    losing streak: each simulation produces one scalar; we report the
    P5 / P50 / P95 of that scalar across simulations.
  * **Breach probabilities** — for each client-supplied drawdown
    threshold, the fraction of simulations whose realised
    max-drawdown ≥ the threshold.

Failed runs (parent has no trades, parent doesn't exist, etc.)
produce ``status='failed'`` results with a reason — same first-class
failure-record pattern as Phase A/C.

The simulation work is CPU-bound NumPy; for typical sim_count ≤ 5000
and trade counts in the dozens-to-hundreds, this finishes in well
under a second. ``run_monte_carlo`` is synchronous; the FastAPI
threadpool is the right execution path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.research.monte_carlo.methods import (
    equity_curve,
    max_drawdown,
    max_losing_streak,
    resample_trades,
    reshuffle_trades,
)
from app.research.monte_carlo.result import (
    BreachProbability,
    EquityBandPoint,
    MonteCarloConfig,
    MonteCarloMethod,
    MonteCarloResult,
)
from app.research.runs.result import BacktestRunResult
from app.research.runs.storage import RunNotFoundError, load_run
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonteCarloRequest:
    """Validated inputs for one Monte Carlo execution."""

    parent_run_id: str
    method: MonteCarloMethod
    simulation_count: int = 1000
    projection_trade_count: int = 0  # 0 → use parent's trade count
    random_seed: int = 0
    breach_thresholds: list[float] = field(default_factory=list)


def run_monte_carlo(
    request: MonteCarloRequest,
    *,
    artifacts_root: Any | None = None,
    monte_carlo_id: str | None = None,
) -> tuple[MonteCarloConfig, MonteCarloResult]:
    """Execute a Monte Carlo analysis and return ``(config, result)``.

    The parent run must exist on disk under ``artifacts_root`` (Phase A
    storage) and have at least one trade. Reshuffle additionally
    requires the parent to have trades because permuting an empty
    array isn't meaningful; resample requires it because we'd have
    nothing to draw from.

    ``projection_trade_count = 0`` resolves to the parent's trade
    count (standard reshuffle / fixed-length bootstrap). For
    reshuffle, ``projection_trade_count`` MUST be 0 or equal to the
    parent's trade count — reshuffle is a permutation, not a sample,
    so other lengths are nonsensical.
    """
    mc_id = monte_carlo_id or uuid.uuid4().hex
    created_at = now_ms_utc()

    # Load the parent run. Failures here are 4xx-shape (caller passed
    # a bad parent_run_id); surface as failed-status MC.
    try:
        parent_ledger, parent_result = load_run(
            request.parent_run_id, root=artifacts_root
        )
    except RunNotFoundError as exc:
        return _failed(
            mc_id, request, created_at, f"parent run not found: {exc}"
        )
    except ValueError as exc:
        return _failed(
            mc_id, request, created_at, f"parent_run_id rejected: {exc}"
        )

    # Validate request shape. The router's Pydantic layer catches most
    # of these for HTTP callers, but ``run_monte_carlo`` is also called
    # by internal code paths (parity tests, future programmatic
    # consumers) that bypass the router. Belt-and-suspenders.
    if request.simulation_count <= 0:
        return _failed(
            mc_id, request, created_at, "simulation_count must be >= 1"
        )
    if request.projection_trade_count < 0:
        return _failed(
            mc_id,
            request,
            created_at,
            f"projection_trade_count must be >= 0 (got {request.projection_trade_count})",
        )
    if request.random_seed < 0:
        # ``numpy.random.default_rng`` raises ValueError for negative
        # seeds; catch here so non-HTTP callers get a failed-status
        # record instead of an unhandled exception.
        return _failed(
            mc_id,
            request,
            created_at,
            f"random_seed must be >= 0 (got {request.random_seed})",
        )
    for t in request.breach_thresholds:
        if not 0.0 <= t <= 1.0:
            return _failed(
                mc_id,
                request,
                created_at,
                f"breach_thresholds must be in [0, 1] (got {t})",
            )

    if not parent_result.trades:
        return _failed(
            mc_id,
            request,
            created_at,
            "parent run has no trades; Monte Carlo requires a non-empty trade list",
        )

    returns = np.array(
        [t.pnl_pct for t in parent_result.trades], dtype=float
    )
    parent_trade_count = returns.size

    realised_trade_count = (
        request.projection_trade_count
        if request.projection_trade_count > 0
        else parent_trade_count
    )

    if request.method == "reshuffle" and realised_trade_count != parent_trade_count:
        return _failed(
            mc_id,
            request,
            created_at,
            (
                f"reshuffle requires projection_trade_count == 0 or "
                f"== parent trade count ({parent_trade_count}); got "
                f"{request.projection_trade_count}"
            ),
        )

    initial_equity = float(parent_result.initial_cash)
    rng = np.random.default_rng(request.random_seed)

    # 2D matrix: [simulation, trade_index] of equity values. Pre-allocate
    # to avoid Python-loop append cost; numpy fills in-place.
    equity_matrix = np.empty(
        (request.simulation_count, realised_trade_count + 1),
        dtype=float,
    )
    drawdowns = np.empty(request.simulation_count, dtype=float)
    streaks = np.empty(request.simulation_count, dtype=int)
    terminal_pnls = np.empty(request.simulation_count, dtype=float)

    for i in range(request.simulation_count):
        if request.method == "reshuffle":
            sim_returns = reshuffle_trades(returns, rng=rng)
        else:  # resample
            sim_returns = resample_trades(
                returns, size=realised_trade_count, rng=rng
            )
        curve = equity_curve(initial_equity, sim_returns)
        equity_matrix[i] = curve
        drawdowns[i] = max_drawdown(curve)
        streaks[i] = max_losing_streak(sim_returns)
        terminal_pnls[i] = curve[-1] - initial_equity

    # Aggregate across simulations.
    bands = _build_equity_bands(equity_matrix)
    dd_quantiles = _quantile_dict(drawdowns)
    pnl_quantiles = _quantile_dict(terminal_pnls)
    # Streak quantiles use ``method='nearest'`` so values are actual
    # observations from the streak distribution, not interpolated
    # fractions. ``int(np.percentile(values, 95))`` previously *floored*
    # interpolated values like 4.95 to 4 — under-reporting the tail.
    # ``method='nearest'`` returns the nearest actual streak length, an
    # integer-valued float we cast cleanly. (PR #112 review.)
    streak_quantiles = _streak_quantile_dict(streaks)
    breach_probs = [
        BreachProbability(
            threshold=t,
            probability=float(np.mean(drawdowns >= t)),
        )
        for t in request.breach_thresholds
    ]

    warnings = _emit_warnings(parent_result, request, parent_trade_count)

    config = MonteCarloConfig(
        monte_carlo_id=mc_id,
        parent_run_id=request.parent_run_id,
        parent_trade_log_hash=parent_ledger.trade_log_hash or "",
        method=request.method,
        simulation_count=request.simulation_count,
        projection_trade_count=request.projection_trade_count,
        initial_equity=initial_equity,
        random_seed=request.random_seed,
        breach_thresholds=list(request.breach_thresholds),
        created_at_ms=created_at,
    )
    result = MonteCarloResult(
        monte_carlo_id=mc_id,
        parent_run_id=request.parent_run_id,
        method=request.method,
        simulation_count=request.simulation_count,
        realised_trade_count=realised_trade_count,
        equity_bands=bands,
        drawdown_quantiles=dd_quantiles,
        terminal_pnl_quantiles=pnl_quantiles,
        max_losing_streak_quantiles=streak_quantiles,
        breach_probabilities=breach_probs,
        warnings=warnings,
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status="completed",
    )
    return config, result


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _build_equity_bands(equity_matrix: np.ndarray) -> list[EquityBandPoint]:
    """Per-trade-index 5/50/95 percentile across simulations.

    NumPy's ``percentile`` with ``axis=0`` returns the percentile at
    each column position — exactly the per-trade-index summary the UI
    needs for the fan chart.
    """
    p5 = np.percentile(equity_matrix, 5, axis=0)
    p50 = np.percentile(equity_matrix, 50, axis=0)
    p95 = np.percentile(equity_matrix, 95, axis=0)
    return [
        EquityBandPoint(
            trade_index=i,
            p5=float(p5[i]),
            p50=float(p50[i]),
            p95=float(p95[i]),
        )
        for i in range(equity_matrix.shape[1])
    ]


def _quantile_dict(values: np.ndarray) -> dict[str, float]:
    """Return ``{p5, p50, p95}`` quantiles of a 1-D array via
    NumPy's default linear interpolation.

    Used for continuous quantities (drawdown fractions, terminal PnL).
    For streak counts (integer-valued), use ``_streak_quantile_dict``
    instead — ``method='nearest'`` keeps values discrete.
    """
    if values.size == 0:
        return {"p5": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "p5": float(np.percentile(values, 5)),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
    }


def _streak_quantile_dict(streaks: np.ndarray) -> dict[str, int]:
    """Return ``{p5, p50, p95}`` losing-streak quantiles as integers.

    Streaks are by definition non-negative integer counts. NumPy's
    default linear interpolation can produce fractional percentiles
    (e.g. P95 of ``[0, 1]`` is ``0.95``); flooring those to ``int(v)``
    under-reports the tail (PR #112 review). Switch to
    ``method='nearest'`` so the percentile picks the closest *actual*
    streak observation — always an integer, no rounding ambiguity.
    """
    if streaks.size == 0:
        return {"p5": 0, "p50": 0, "p95": 0}
    return {
        "p5": int(np.percentile(streaks, 5, method="nearest")),
        "p50": int(np.percentile(streaks, 50, method="nearest")),
        "p95": int(np.percentile(streaks, 95, method="nearest")),
    }


def _emit_warnings(
    parent_result: BacktestRunResult,
    request: MonteCarloRequest,
    parent_trade_count: int,
) -> list[str]:
    """Surface degenerate-input warnings without failing the run.

    The architecture spec calls out "low-trade-count" warnings as a
    requirement — a Monte Carlo over 5 trades has wide quantiles
    that aren't statistically meaningful. We don't *block* the run
    (the user may genuinely want to look) but we surface the warning
    in the result so the UI can flag it.
    """
    warnings: list[str] = []
    if parent_trade_count < 30:
        warnings.append(
            f"parent run has only {parent_trade_count} trades — Monte Carlo "
            f"quantiles will be wide; treat the bands as illustrative rather "
            f"than statistically definitive"
        )
    if (
        request.method == "resample"
        and request.projection_trade_count > 4 * parent_trade_count
    ):
        warnings.append(
            f"forward projection of {request.projection_trade_count} trades "
            f"is >4× the historical {parent_trade_count}; the resampled "
            f"distribution assumes IID returns over a much longer horizon "
            f"than the data supports"
        )
    if request.simulation_count < 100:
        warnings.append(
            f"simulation_count={request.simulation_count} is low; tail "
            f"quantiles (P5 / P95) are noisy. Consider 1000+."
        )
    return warnings


def _failed(
    mc_id: str,
    request: MonteCarloRequest,
    created_at: int,
    reason: str,
) -> tuple[MonteCarloConfig, MonteCarloResult]:
    """Build an empty result paired with a failed-status config.

    Same first-class-failure pattern as Phase A/C: persist failures
    so they're discoverable, don't raise from the runner.
    """
    config = MonteCarloConfig(
        monte_carlo_id=mc_id,
        parent_run_id=request.parent_run_id,
        parent_trade_log_hash="",
        method=request.method,
        simulation_count=max(1, request.simulation_count),
        projection_trade_count=max(0, request.projection_trade_count),
        initial_equity=1.0,  # placeholder; failed runs don't reflect real values
        random_seed=request.random_seed,
        breach_thresholds=list(request.breach_thresholds),
        created_at_ms=created_at,
    )
    result = MonteCarloResult(
        monte_carlo_id=mc_id,
        parent_run_id=request.parent_run_id,
        method=request.method,
        simulation_count=max(1, request.simulation_count),
        realised_trade_count=0,
        warnings=[reason],
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status="failed",
        failure_reason=reason,
    )
    return config, result
