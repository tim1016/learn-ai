"""``run_walk_forward`` — orchestrates fold execution and aggregation.

Takes a validated ``StrategySpec`` plus a window plus a split policy,
generates folds, runs each fold's TEST window through the canonical
engine via ``run_strategy_spec``, persists every fold as a child
``RunLedger`` linked back to this walk-forward by ``parent_run_id``,
and aggregates fold-level metrics into a ``WalkForwardResult``.

**Phase 4A only.** The train side of each fold is recorded but not
executed — there's no parameter selection happening on train, so
running the train window would just produce a redundant in-sample
fit. Phase 4B (parameter selection on train, frozen on test) plugs
into the same fold list with a non-empty ``selected_parameters``
field per fold.

Failures are persisted, not raised. A fold that hits an unsupported
spec feature, infrastructure error, or engine crash gets a
``status='failed'`` ledger via the underlying ``run_strategy_spec``
contract — the WF runner records the failure in the fold list,
skips it from aggregation, and continues with the remaining folds.
The WF result itself only flips to ``status='failed'`` when *every*
fold fails (or the split policy emits zero folds).
"""

from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.strategy.spec import StrategySpec
from app.research.runs import RunLedger, RunRequest, run_strategy_spec, save_run
from app.research.runs.hashing import hash_payload
from app.research.runs.ledger import now_ms_utc
from app.research.runs.result import BacktestRunResult, EquityCurvePoint
from app.research.walk_forward.result import (
    FoldResult,
    SplitPolicySpec,
    WalkForwardConfig,
    WalkForwardResult,
)
from app.research.walk_forward.splits import SplitPolicy, date_str_to_ms

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class WalkForwardRequest:
    """Validated inputs for one walk-forward execution.

    Mirrors ``RunRequest`` for cost / fill semantics; adds the
    split policy and an optional ``parent_run_id`` linking this WF to
    a baseline full-window run (typically the run that established
    the strategy was worth folding in the first place).
    """

    spec: StrategySpec
    start_date: str  # YYYY-MM-DD
    end_date: str
    split_policy: SplitPolicy
    initial_cash: float = 100_000.0
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 0.0
    slippage_per_share: float = 0.0
    random_seed: int = 0
    parent_run_id: str | None = None


def run_walk_forward(
    request: WalkForwardRequest,
    *,
    data_source_factory: Any,
    artifacts_root: Any | None = None,
    data_root_revision: str | None = None,
    walk_forward_id: str | None = None,
) -> tuple[WalkForwardConfig, WalkForwardResult]:
    """Execute a walk-forward analysis and return ``(config, result)``.

    ``data_source_factory`` is the same callable Phase A uses — tests
    inject a synthetic reader; the FastAPI router injects the real
    LEAN one. ``artifacts_root`` is forwarded to the per-fold
    ``save_run`` so test runs land under ``tmp_path``.

    Each fold's run is persisted before the next fold starts. The
    walk-forward result is *not* persisted by this function — that's
    the storage layer's job; ``run_walk_forward`` returns the
    ``(config, result)`` pair and the router (or test) decides where
    to write.
    """
    wf_id = walk_forward_id or uuid.uuid4().hex
    created_at = now_ms_utc()

    spec_dump = request.spec.model_dump(mode="json")
    spec_hash = hash_payload(spec_dump)
    symbol = request.spec.symbols[0]
    resolution = request.spec.resolution.period_minutes

    start_ms = date_str_to_ms(request.start_date)
    end_ms = date_str_to_ms(request.end_date)

    config = WalkForwardConfig(
        walk_forward_id=wf_id,
        parent_run_id=request.parent_run_id,
        strategy_spec_hash=spec_hash,
        strategy_spec_json=spec_dump,
        symbol=symbol,
        resolution_minutes=resolution,
        start_ms=start_ms,
        end_ms=end_ms,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        split_policy=SplitPolicySpec.model_validate(request.split_policy.describe()),
        created_at_ms=created_at,
    )

    # 1. Generate folds. A failure here (e.g., window too short for the
    #    requested split) is a 4xx-shape error — surface as a failed WF
    #    result so the caller can persist it uniformly.
    try:
        windows = request.split_policy.folds(start_ms, end_ms)
    except ValueError as exc:
        return config, _failed_wf_result(
            wf_id, spec_hash, request, created_at, f"split-policy error: {exc}"
        )

    if not windows:
        return config, _failed_wf_result(
            wf_id, spec_hash, request, created_at, "split policy produced zero folds"
        )

    # 2. Run each fold's test window. Persist every result — failed
    #    folds are first-class research records (same contract as
    #    Phase A) so the listing endpoint surfaces them.
    folds: list[FoldResult] = []
    fold_curves: list[list[EquityCurvePoint]] = []
    warnings: list[str] = []

    for window in windows:
        fold_request = RunRequest(
            spec=request.spec,
            start_date=_ms_to_date(window.test_start_ms),
            end_date=_ms_to_inclusive_end_date(window.test_end_ms),
            initial_cash=request.initial_cash,
            fill_mode=request.fill_mode,
            commission_per_order=request.commission_per_order,
            slippage_per_share=request.slippage_per_share,
            random_seed=request.random_seed,
            strategy_spec_id=f"wf:{wf_id}:fold-{window.fold_index}",
            parent_run_id=wf_id,
            parent_spec_hash=spec_hash,
        )
        ledger, result = run_strategy_spec(
            fold_request,
            data_source_factory=data_source_factory,
            data_root_revision=data_root_revision,
        )
        try:
            save_run(ledger, result, root=artifacts_root)
        except Exception as exc:
            # Persistence failure for one fold doesn't stop the WF —
            # log and continue. The fold result is still recorded
            # in-memory but the on-disk record is missing; surface
            # via warnings so the client sees it.
            logger.exception(
                "[WF] failed to persist fold %s of walk_forward=%s",
                window.fold_index,
                wf_id,
            )
            warnings.append(
                f"fold {window.fold_index} could not be persisted: {exc}"
            )

        folds.append(_fold_to_result(window, ledger, result))
        if ledger.status == "completed" and result.equity_curve:
            fold_curves.append(result.equity_curve)

    # 3. Aggregate.
    # ``pct_profitable_folds`` denominator counts only successful folds —
    # a fold that crashed at the engine boundary is an infrastructure
    # story, not a strategy story, and shouldn't dilute the OOS
    # scoreboard. Same rule for ``mean/median_oos_sharpe`` and
    # ``alpha_decay`` — they all key off ``FoldResult.status`` to
    # exclude failed folds.
    successful_folds = [f for f in folds if f.status == "completed"]
    sharpes = [
        f.test_metrics.sharpe_ratio
        for f in successful_folds
        if f.test_metrics.sharpe_ratio is not None
    ]
    profitable_count = sum(
        1 for f in successful_folds if f.test_metrics.total_return_pct > 0
    )
    pct_profitable = (
        profitable_count / len(successful_folds) if successful_folds else None
    )

    combined_curve = _compound_oos_curve(fold_curves)
    alpha_decay = _alpha_decay(successful_folds)

    if not successful_folds:
        warnings.append("every fold failed — aggregate metrics are degenerate")

    completed_at = now_ms_utc()

    result = WalkForwardResult(
        walk_forward_id=wf_id,
        parent_run_id=request.parent_run_id,
        strategy_spec_hash=spec_hash,
        split_policy=config.split_policy,
        folds=folds,
        combined_oos_equity_curve=combined_curve,
        mean_oos_sharpe=statistics.fmean(sharpes) if sharpes else None,
        median_oos_sharpe=statistics.median(sharpes) if sharpes else None,
        pct_profitable_folds=pct_profitable,
        # ``oos_retention`` requires a parent-run sharpe to compare against;
        # leave None until the caller wires that in (router-level concern).
        oos_retention=None,
        alpha_decay=alpha_decay,
        warnings=warnings,
        created_at_ms=created_at,
        completed_at_ms=completed_at,
        status="completed",
    )
    return config, result


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _ms_to_date(ms: int) -> Date:
    """Convert ``int64 ms UTC`` (NY-midnight) to ``date`` for ``RunRequest``.

    ``RunRequest`` declares ``start_date: Date``; the runner internally
    re-anchors via NY-midnight, so producing a NY-local date here
    keeps the round-trip correct.
    """
    return datetime.fromtimestamp(ms / 1000, tz=_NY).date()


def _ms_to_inclusive_end_date(ms: int) -> Date:
    """Convert a half-open fold boundary to the *last day* of the fold.

    Split policies emit fold boundaries as half-open ms intervals
    ``[test_start_ms, test_end_ms)`` — fold N+1's test_start_ms equals
    fold N's test_end_ms (no gap, no overlap). But the engine's data
    filter is *inclusive* on both ends (``start <= bar.date() <= end``),
    so passing ``test_end_ms``'s NY date directly would include the
    boundary day in *both* fold N and fold N+1, producing duplicate
    bars in the combined OOS curve.

    Subtract one calendar day in NY-local time (DST-safe — uses
    ``timedelta(days=1)`` on a tz-aware datetime so spring-forward /
    fall-back doesn't shift the resulting date).
    """
    return (datetime.fromtimestamp(ms / 1000, tz=_NY) - timedelta(days=1)).date()


def _fold_to_result(window, ledger: RunLedger, result: BacktestRunResult) -> FoldResult:
    # Mirror the underlying ledger's lifecycle status onto the fold so
    # aggregation can exclude failed folds without re-loading ledgers.
    # Phase A's RunLedger.status is one of {"running", "completed",
    # "failed"}; "running" is a transient state the runner never
    # surfaces synchronously, so collapse to a 2-value field here.
    fold_status = "failed" if ledger.status == "failed" else "completed"
    return FoldResult(
        fold_index=window.fold_index,
        train_start_ms=window.train_start_ms,
        train_end_ms=window.train_end_ms,
        test_start_ms=window.test_start_ms,
        test_end_ms=window.test_end_ms,
        test_run_id=ledger.run_id,
        test_metrics=result.metrics,
        test_trade_count=result.metrics.total_trades,
        status=fold_status,
        failure_reason=ledger.failure_reason,
    )


def _alpha_decay(folds: list[FoldResult]) -> float | None:
    """Slope of ``test_metrics.sharpe_ratio`` vs ``fold_index`` via OLS.

    Returns ``None`` when fewer than 2 folds have non-None sharpe — a
    single point can't establish a slope. Negative slopes indicate
    decay; positive slopes indicate the strategy is still working
    (or improving). The metric is meant to be *directional*, not a
    pass/fail gate.
    """
    points = [
        (f.fold_index, f.test_metrics.sharpe_ratio)
        for f in folds
        if f.test_metrics.sharpe_ratio is not None
    ]
    if len(points) < 2:
        return None
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    return (n * sum_xy - sum_x * sum_y) / denom


def _compound_oos_curve(
    fold_curves: list[list[EquityCurvePoint]],
) -> list[EquityCurvePoint]:
    """Concatenate fold equity curves with compounding.

    Each fold's curve normally starts at the same configured
    ``initial_cash`` (because every fold runs through the same
    ``RunRequest.initial_cash`` value) and ends at some terminal
    equity. To produce the "investor experience" curve across all
    folds, fold N+1's curve is *scaled* so its first point equals
    fold N's last point. The scale factor is
    ``fold_N_last / fold_N+1_first``.

    Fold 0's curve is emitted as-is — its first point is already at
    ``initial_cash``, which is the correct starting equity for the
    combined view. (An earlier draft accepted ``initial_cash`` as a
    parameter but never used it — caller relied on per-fold inputs
    being consistent. Dropped per PR #110 review.)

    Returns an empty list when there are no fold curves to combine.
    """
    if not fold_curves:
        return []
    out: list[EquityCurvePoint] = []
    running_multiplier = 1.0
    for curve in fold_curves:
        if not curve:
            continue
        first_equity = curve[0].equity
        if first_equity <= 0:
            # Pathological — skip this fold rather than divide by zero.
            continue
        for point in curve:
            scaled = point.equity * running_multiplier
            out.append(EquityCurvePoint(timestamp_ms=point.timestamp_ms, equity=scaled))
        # After this fold, the running multiplier compounds by the
        # fold's terminal-over-initial ratio.
        running_multiplier *= curve[-1].equity / first_equity
    return out


def _failed_wf_result(
    wf_id: str,
    spec_hash: str,
    request: WalkForwardRequest,
    created_at: int,
    reason: str,
) -> WalkForwardResult:
    """Build an empty WF result paired with a failed status.

    Used for early failures (split-policy errors, zero folds) where
    we never get to execute any folds. Late failures (some folds
    succeed, some don't) stay ``status='completed'`` and surface
    per-fold via the fold list.
    """
    return WalkForwardResult(
        walk_forward_id=wf_id,
        parent_run_id=request.parent_run_id,
        strategy_spec_hash=spec_hash,
        split_policy=SplitPolicySpec.model_validate(request.split_policy.describe()),
        folds=[],
        combined_oos_equity_curve=[],
        mean_oos_sharpe=None,
        median_oos_sharpe=None,
        pct_profitable_folds=None,
        oos_retention=None,
        alpha_decay=None,
        warnings=[reason],
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status="failed",
        failure_reason=reason,
    )


