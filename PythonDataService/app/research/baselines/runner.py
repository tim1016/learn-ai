"""``run_baselines`` — generate N null-baseline specs, run each, aggregate.

Loads the parent run from disk, generates N alternative strategies
via the requested method (``buy_and_hold`` repeats the same single-trade
spec, ``random_ema_windows`` samples ``(fast, slow)`` pairs), runs
each through the canonical engine on the parent's symbol / window /
cost model, and aggregates target metrics across the successful
baseline runs into per-metric null distributions.

Each baseline run is persisted as a child ``RunLedger`` under
``artifacts/runs/<baseline_run_id>/`` with ``parent_run_id`` set to
the **baselines run id** (not the original parent run) so
``list_runs(parent_run_id=baseline_id)`` enumerates the baseline
children. The aggregated result is persisted separately under
``artifacts/baselines/<baseline_id>/``.

Failures match the Phase A/C/D contract: missing parent run,
malformed parent_run_id, generator failure, or zero successful
baselines → ``status='failed'`` with ``failure_reason`` populated.
Per-baseline failures don't fail the overall run; they appear in
the ``baselines`` list with ``status='failed'`` and are excluded
from null-distribution aggregation.

The work is mostly waiting on engine runs (one per baseline). For
``random_ema_windows`` with ``sample_count=50``, this can take
many seconds against real LEAN data. The router caps
``sample_count`` to keep request latency reasonable; the existing
job orchestration in ``app/routers/jobs.py`` is the natural escape
hatch when long-running baseline batches become a workflow.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from app.engine.strategy.spec import StrategySpec
from app.research.baselines.generators import (
    BaselineMethod,
    buy_and_hold_spec,
    random_ema_window_specs,
)
from app.research.baselines.result import (
    BaselineConfig,
    BaselineResult,
    BaselineRunRecord,
    NullDistribution,
)
from app.research.runs import (
    RunLedger,
    RunRequest,
    run_strategy_spec,
    save_run,
)
from app.research.runs.ledger import now_ms_utc
from app.research.runs.result import RunMetrics
from app.research.runs.storage import RunNotFoundError, load_run

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")

# Default null-distribution coverage. Architecture spec § Feature 7
# calls out Sharpe / max drawdown / profit factor / return as the
# headline comparisons; we add ``win_rate`` and ``expectancy_pct``
# because they're cheap and complete the workbench's metrics card.
_DEFAULT_TARGET_METRICS = (
    "sharpe_ratio",
    "total_return_pct",
    "max_drawdown_pct",
    "profit_factor",
    "win_rate",
    "expectancy_pct",
)


@dataclass(frozen=True)
class BaselineRequest:
    """Validated inputs for one baselines run."""

    parent_run_id: str
    method: BaselineMethod
    sample_count: int = 30
    random_seed: int = 0
    target_metrics: tuple[str, ...] = _DEFAULT_TARGET_METRICS
    # ``random_ema_windows`` reads these; ``buy_and_hold`` ignores them.
    fast_range: tuple[int, int] = (3, 12)
    slow_range: tuple[int, int] = (10, 30)


def run_baselines(
    request: BaselineRequest,
    *,
    data_source_factory: Any,
    artifacts_root: Any | None = None,
    data_root_revision: str | None = None,
    baseline_id: str | None = None,
) -> tuple[BaselineConfig, BaselineResult]:
    """Execute a null-baseline analysis and return ``(config, result)``."""
    bid = baseline_id or uuid.uuid4().hex
    created_at = now_ms_utc()

    # Load the parent run. Failures here surface as failed-status
    # records (Phase A/C/D contract).
    try:
        parent_ledger, _parent_result = load_run(
            request.parent_run_id, root=artifacts_root
        )
    except RunNotFoundError as exc:
        return _failed(bid, request, created_at, f"parent run not found: {exc}")
    except ValueError as exc:
        return _failed(bid, request, created_at, f"parent_run_id rejected: {exc}")

    # Refuse to derive a baseline from a failed parent. The parent's
    # metrics are placeholders in that case (engine never produced
    # them); comparing baselines against placeholders yields
    # meaningless percentiles and p-values. Surface as a failed
    # baseline record so the caller sees the cause.
    if parent_ledger.status == "failed":
        return _failed(
            bid,
            request,
            created_at,
            f"parent run is in status='failed' (run_id={request.parent_run_id})",
        )

    if request.sample_count <= 0:
        return _failed(bid, request, created_at, "sample_count must be >= 1")
    if request.random_seed < 0:
        # ``numpy.random.default_rng`` raises for negative seeds — same
        # belt-and-suspenders as ``run_monte_carlo``.
        return _failed(
            bid, request, created_at,
            f"random_seed must be >= 0 (got {request.random_seed})",
        )

    rng = np.random.default_rng(request.random_seed)

    # Generate the spec list. ``buy_and_hold`` is parameter-less, so
    # we just repeat the same spec sample_count times — every run
    # produces the same trade list, but the ``BaselineRunRecord`` /
    # null-distribution layer doesn't special-case zero-variance
    # methods. (The architecture spec calls B&H out as a *single*
    # baseline; sample_count=1 is the sensible default for it.)
    try:
        spec_records = _generate_specs(parent_ledger, request, rng)
    except ValueError as exc:
        return _failed(
            bid, request, created_at, f"baseline generator error: {exc}"
        )

    # Run each baseline. Use the parent's symbol/window/cost model;
    # only the strategy logic varies across baselines.
    parent_spec_hash = parent_ledger.strategy_spec_hash
    baselines: list[BaselineRunRecord] = []
    completed_metrics: list[RunMetrics] = []
    warnings: list[str] = []

    for spec, params in spec_records:
        run_request = RunRequest(
            spec=spec,
            start_date=_ms_to_date(parent_ledger.start_ms),
            end_date=_ms_to_date(parent_ledger.end_ms),
            initial_cash=parent_ledger.initial_cash,
            fill_mode=parent_ledger.fill_mode,
            commission_per_order=parent_ledger.commission_per_order,
            slippage_per_share=parent_ledger.slippage_per_share,
            random_seed=parent_ledger.random_seed,
            strategy_spec_id=f"baseline:{bid}:{spec.name}",
            parent_run_id=bid,
            parent_spec_hash=parent_spec_hash,
        )
        ledger, result = run_strategy_spec(
            run_request,
            data_source_factory=data_source_factory,
            data_root_revision=data_root_revision,
        )
        try:
            save_run(ledger, result, root=artifacts_root)
        except Exception as exc:
            logger.exception(
                "[BASELINES] failed to persist baseline_run_id=%s",
                ledger.run_id,
            )
            warnings.append(
                f"baseline run {ledger.run_id} could not be persisted: {exc}"
            )

        record = BaselineRunRecord(
            baseline_run_id=ledger.run_id,
            method=request.method,
            parameters=params,
            test_metrics=result.metrics,
            test_trade_count=result.metrics.total_trades,
            status="failed" if ledger.status == "failed" else "completed",
            failure_reason=ledger.failure_reason,
        )
        baselines.append(record)
        if record.status == "completed":
            completed_metrics.append(result.metrics)

    if not completed_metrics:
        warnings.append(
            "every baseline run failed — null distribution is empty"
        )

    # Compute null distributions per target metric.
    distributions = _build_null_distributions(
        parent_metrics=_load_parent_metrics(
            request.parent_run_id, artifacts_root
        ),
        completed_metrics=completed_metrics,
        target_metrics=request.target_metrics,
    )

    config = BaselineConfig(
        baseline_id=bid,
        parent_run_id=request.parent_run_id,
        parent_trade_log_hash=parent_ledger.trade_log_hash or "",
        method=request.method,
        sample_count=request.sample_count,
        random_seed=request.random_seed,
        method_params=_method_params(request),
        target_metrics=list(request.target_metrics),
        created_at_ms=created_at,
    )
    result_obj = BaselineResult(
        baseline_id=bid,
        parent_run_id=request.parent_run_id,
        method=request.method,
        sample_count=request.sample_count,
        baselines=baselines,
        null_distributions=distributions,
        warnings=warnings,
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status="completed",
    )
    return config, result_obj


# ---------------------------------------------------------------------------
# Generator dispatch.
# ---------------------------------------------------------------------------
def _generate_specs(
    parent: RunLedger,
    request: BaselineRequest,
    rng: np.random.Generator,
) -> list[tuple[StrategySpec, dict]]:
    """Dispatch on ``method`` to produce ``(spec, parameters)`` pairs."""
    if request.method == "buy_and_hold":
        # B&H is deterministic and has no sampled parameter — repeat
        # the same spec sample_count times. Most users will set
        # sample_count=1 for B&H; we don't enforce it because a user
        # might want N replicate persisted runs for sanity-checking
        # the engine's determinism.
        spec = buy_and_hold_spec(parent)
        return [(spec, {}) for _ in range(request.sample_count)]
    if request.method == "random_ema_windows":
        return random_ema_window_specs(
            parent,
            count=request.sample_count,
            fast_range=request.fast_range,
            slow_range=request.slow_range,
            rng=rng,
        )
    raise ValueError(f"unknown baseline method: {request.method!r}")


def _method_params(request: BaselineRequest) -> dict:
    """Method-specific config that's *not* per-baseline."""
    if request.method == "random_ema_windows":
        return {
            "fast_range": list(request.fast_range),
            "slow_range": list(request.slow_range),
        }
    return {}


# ---------------------------------------------------------------------------
# Null-distribution aggregation.
# ---------------------------------------------------------------------------
def _load_parent_metrics(
    parent_run_id: str, artifacts_root: Any | None
) -> RunMetrics:
    """Load the parent run's RunMetrics fresh from disk.

    Loaded a second time here (the runner already loaded the ledger
    above for spec / window / cost extraction) because the runner
    discarded the result body. Cheap to re-parse.
    """
    _, result = load_run(parent_run_id, root=artifacts_root)
    return result.metrics


def _build_null_distributions(
    *,
    parent_metrics: RunMetrics,
    completed_metrics: list[RunMetrics],
    target_metrics: tuple[str, ...],
) -> list[NullDistribution]:
    """For each target metric, gather the null values + parent's
    empirical position (percentile + small-sample p-value).

    Skips ``None`` values in the null sample (a baseline may produce
    ``win_rate=None`` on zero trades; that fold doesn't contribute).
    """
    out: list[NullDistribution] = []
    for metric_name in target_metrics:
        parent_value = _maybe_float(getattr(parent_metrics, metric_name, None))
        null_values: list[float] = []
        for m in completed_metrics:
            v = _maybe_float(getattr(m, metric_name, None))
            if v is not None:
                null_values.append(v)

        percentile, p_value = _empirical_position(parent_value, null_values)
        out.append(
            NullDistribution(
                metric_name=metric_name,
                parent_value=parent_value,
                null_values=null_values,
                empirical_percentile=percentile,
                empirical_p_value=p_value,
            )
        )
    return out


def _empirical_position(
    parent_value: float | None,
    null_values: list[float],
) -> tuple[float | None, float | None]:
    """Compute ``(percentile, one-sided p-value)`` of ``parent_value``
    in ``null_values``.

    * ``percentile`` = fraction of null values *strictly less than*
      parent (in [0, 1]). For higher-is-better metrics, higher
      percentile = parent outperformed the null.
    * ``p_value`` = ``(1 + count(null >= parent)) / (N + 1)`` —
      Phipson-Smyth small-sample p-value for "parent is anomalously
      high". Symmetric form for "anomalously low" is ``1 - p_value``,
      computed by the client.

    Returns ``(None, None)`` when parent_value is None or
    null_values is empty (can't position parent against an empty
    distribution).
    """
    if parent_value is None or not null_values:
        return None, None
    arr = np.asarray(null_values, dtype=float)
    strictly_less = float(np.mean(arr < parent_value))
    n = arr.size
    p_value = float((1 + np.sum(arr >= parent_value)) / (n + 1))
    return strictly_less, p_value


def _maybe_float(v: Any) -> float | None:
    """Coerce a metric reading to ``float`` if non-None and finite."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Window helpers.
# ---------------------------------------------------------------------------
# Phase A persists ``RunLedger.end_ms`` as the NY-midnight of the
# *inclusive* end date the parent ran with — same convention as the
# input ``end_date`` to ``RunRequest`` (see ``runs/runner.py``
# ``_date_to_ny_midnight_ms``). The engine's date filter is inclusive
# on both ends. So converting parent.end_ms back to its NY-local date
# directly reproduces the parent's window verbatim — no day
# subtraction. (Walk-forward's ``_ms_to_inclusive_end_date`` does
# subtract one day, but only because split policies emit half-open
# ``[start_ms, end_ms)`` fold boundaries. Different convention.)
def _ms_to_date(ms: int) -> Any:
    """Convert ``int64 ms UTC`` (NY-midnight) to ``date``."""
    return datetime.fromtimestamp(ms / 1000, tz=_NY).date()


# ---------------------------------------------------------------------------
# Failure path.
# ---------------------------------------------------------------------------
def _failed(
    bid: str,
    request: BaselineRequest,
    created_at: int,
    reason: str,
) -> tuple[BaselineConfig, BaselineResult]:
    """Build an empty result paired with a failed-status config.

    Same first-class-failure pattern as Phase A/C/D: persist failures
    so they're discoverable.
    """
    config = BaselineConfig(
        baseline_id=bid,
        parent_run_id=request.parent_run_id,
        parent_trade_log_hash="",
        method=request.method,
        sample_count=max(1, request.sample_count),
        random_seed=max(0, request.random_seed),
        method_params=_method_params(request),
        target_metrics=list(request.target_metrics),
        created_at_ms=created_at,
    )
    result = BaselineResult(
        baseline_id=bid,
        parent_run_id=request.parent_run_id,
        method=request.method,
        sample_count=max(1, request.sample_count),
        baselines=[],
        null_distributions=[],
        warnings=[reason],
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status="failed",
        failure_reason=reason,
    )
    return config, result


