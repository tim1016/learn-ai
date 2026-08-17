"""``run_walk_forward`` — orchestrates fold execution and aggregation.

Takes a validated ``StrategySpec`` plus a window plus a split policy,
generates folds, and runs each fold through the canonical engine. With no
``parameter_search`` it preserves Phase 4A fixed-spec behavior. With a search,
it runs every fully-materialized candidate on TRAIN, deterministically selects
the best eligible train Sharpe, freezes that candidate, and runs it on TEST.
Every train candidate and test fold is persisted as a child ``RunLedger``.

Failures are persisted, not raised. A fold that hits an unsupported
spec feature, infrastructure error, or engine crash gets a
``status='failed'`` ledger via the underlying ``run_strategy_spec``
contract — the WF runner records the failure in the fold list,
skips it from aggregation, and continues with the remaining folds.
The WF result flips to ``status='failed'`` when the split is degenerate,
every test fold fails, or train-side selection cannot produce an eligible
candidate. Selection failure is fail-closed: no fallback threshold is tested.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.engine.strategy.spec import StrategySpec
from app.research.runs import (
    RunLedger,
    RunRequest,
    run_date_to_ms,
    run_strategy_spec,
    save_run,
)
from app.research.runs.hashing import hash_payload
from app.research.runs.result import BacktestRunResult, EquityCurvePoint
from app.research.walk_forward.metrics import mean_fold_retention, sharpe_retention
from app.research.walk_forward.result import (
    FoldResult,
    ParameterCandidateConfig,
    ParameterSearchConfig,
    SelectionFailureResult,
    SplitPolicySpec,
    TrainingCandidateResult,
    WalkForwardConfig,
    WalkForwardResult,
    parse_split_policy_spec,
)
from app.research.walk_forward.selection import (
    ParameterSearchRequest,
    ParameterSelectionError,
    select_candidate_index,
)
from app.research.walk_forward.splits import FoldWindow, SplitPolicy
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
CancelCheck = Callable[[], bool | None]


@dataclass(frozen=True)
class WalkForwardRequest:
    """Validated inputs for one walk-forward execution.

    Mirrors ``RunRequest`` for cost / fill semantics; adds the
    split policy and an optional ``parent_run_id`` linking this WF to
    a baseline full-window run (typically the run that established
    the strategy was worth folding in the first place).
    """

    spec: StrategySpec
    start_ms: int
    end_ms: int
    split_policy: SplitPolicy
    initial_cash: float = 100_000.0
    fill_mode: str = "signal_bar_close"
    commission_per_order: float = 0.0
    slippage_per_share: float = 0.0
    random_seed: int = 0
    parent_run_id: str | None = None
    parameter_search: ParameterSearchRequest | None = None
    fold_position_policy: Literal["flat_at_test_boundaries"] = "flat_at_test_boundaries"
    protocol_id: str | None = None
    protocol_version: str | None = None

    def __post_init__(self) -> None:
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be strictly before end_ms")
        if (self.protocol_id is None) != (self.protocol_version is None):
            raise ValueError("protocol_id and protocol_version must be provided together")
        if self.parameter_search is None:
            return
        symbol = self.spec.symbols[0]
        resolution = self.spec.resolution.period_minutes
        for candidate in self.parameter_search.candidates:
            if candidate.spec.symbols[0] != symbol:
                raise ValueError("parameter candidates must use the base spec symbol")
            if candidate.spec.resolution.period_minutes != resolution:
                raise ValueError("parameter candidates must use the base spec resolution")


def run_walk_forward(
    request: WalkForwardRequest,
    *,
    data_source_factory: Any,
    artifacts_root: Any | None = None,
    data_root_revision: str | None = None,
    parent_sharpe: float | None = None,
    walk_forward_id: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel_check: CancelCheck | None = None,
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

    start_ms = request.start_ms
    end_ms = request.end_ms

    split_policy_spec = parse_split_policy_spec(request.split_policy.describe())
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
        split_policy=split_policy_spec,
        parameter_search=_parameter_search_config(request.parameter_search),
        fold_position_policy=request.fold_position_policy,
        protocol_id=request.protocol_id,
        protocol_version=request.protocol_version,
        created_at_ms=created_at,
    )

    # 1. Generate folds. A failure here (e.g., window too short for the
    #    requested split) is a 4xx-shape error — surface as a failed WF
    #    result so the caller can persist it uniformly.
    try:
        windows = request.split_policy.folds(start_ms, end_ms)
    except ValueError as exc:
        return config, _failed_wf_result(
            wf_id,
            spec_hash,
            request,
            split_policy_spec,
            created_at,
            f"split-policy error: {exc}",
        )

    if not windows:
        return config, _failed_wf_result(
            wf_id,
            spec_hash,
            request,
            split_policy_spec,
            created_at,
            "split policy produced zero folds",
        )

    # 2. Select on train when configured, then run the frozen spec on test.
    #    Every candidate and test run is persisted for auditability.
    folds: list[FoldResult] = []
    fold_curves: list[list[EquityCurvePoint]] = []
    selection_failures: list[SelectionFailureResult] = []
    warnings: list[str] = []
    completed_child_runs = 0
    runs_per_fold = len(request.parameter_search.candidates) + 1 if request.parameter_search is not None else 1
    total_child_runs = len(windows) * runs_per_fold

    def report_child_run(message: str) -> None:
        nonlocal completed_child_runs
        completed_child_runs += 1
        if on_progress is not None:
            on_progress(completed_child_runs, total_child_runs, message)

    for window in windows:
        if cancel_check is not None:
            cancel_check()
        selected_spec = request.spec
        selected_parameters: dict[str, float] = {}
        training_candidates: list[TrainingCandidateResult] = []
        selected_train_sharpe: float | None = None
        if request.parameter_search is not None:
            try:
                (
                    selected_spec,
                    selected_parameters,
                    training_candidates,
                    selected_train_sharpe,
                    selection_warnings,
                ) = _select_fold_candidate(
                    request,
                    window,
                    wf_id=wf_id,
                    spec_hash=spec_hash,
                    data_source_factory=data_source_factory,
                    artifacts_root=artifacts_root,
                    data_root_revision=data_root_revision,
                    cancel_check=cancel_check,
                    on_candidate_completed=report_child_run,
                )
                warnings.extend(selection_warnings)
            except ParameterSelectionError as exc:
                warnings.extend(exc.warnings)
                reason = f"fold {window.fold_index} parameter selection failed: {exc}"
                warnings.append(reason)
                selection_failures.append(
                    SelectionFailureResult(
                        fold_index=window.fold_index,
                        train_start_ms=window.train_start_ms,
                        train_end_ms=window.train_end_ms,
                        test_start_ms=window.test_start_ms,
                        test_end_ms=window.test_end_ms,
                        training_candidates=list(exc.candidates),
                        failure_reason=reason,
                    )
                )
                return config, _failed_wf_result(
                    wf_id,
                    spec_hash,
                    request,
                    split_policy_spec,
                    created_at,
                    reason,
                    folds=folds,
                    fold_curves=fold_curves,
                    selection_failures=selection_failures,
                    parent_sharpe=parent_sharpe,
                    warnings=warnings,
                )

        # Cancellation may arrive while the final training candidate is
        # executing. Re-check at the engine boundary so a cancelled job never
        # launches the fold's full TEST run.
        if cancel_check is not None:
            cancel_check()

        fold_request = RunRequest(
            spec=selected_spec,
            start_ms=window.test_start_ms,
            end_ms=run_date_to_ms(_ms_to_inclusive_end_date(window.test_end_ms)),
            warmup_start_ms=window.train_start_ms,
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
        persistence_warning = _persist_child_run(
            ledger,
            result,
            artifacts_root=artifacts_root,
            context=f"fold {window.fold_index}",
            wf_id=wf_id,
        )
        if persistence_warning is not None:
            warnings.append(persistence_warning)

        result_failure_reason: str | None = None
        for child_warning in result.warnings:
            warnings.append(f"fold {window.fold_index} test warning: {child_warning}")
        if ledger.status == "completed" and result.bars_consumed == 0:
            result_failure_reason = (
                "test window consumed zero input bars; refusing to treat it as out-of-sample evidence"
            )
            warnings.append(f"fold {window.fold_index} {result_failure_reason}")

        fold = _fold_to_result(
            window,
            ledger,
            result,
            selected_parameters=selected_parameters,
            training_candidates=training_candidates,
            selected_train_sharpe=selected_train_sharpe,
            persisted=persistence_warning is None,
            result_failure_reason=result_failure_reason,
        )
        folds.append(fold)
        if fold.status == "completed" and result.equity_curve:
            fold_curves.append(result.equity_curve)
        report_child_run(f"completed out-of-sample fold {window.fold_index + 1} of {len(windows)}")
        if persistence_warning is not None:
            reason = (
                f"fold {window.fold_index} test receipt persistence failed; "
                "refusing to aggregate unverifiable OOS evidence"
            )
            warnings.append(reason)
            return config, _failed_wf_result(
                wf_id,
                spec_hash,
                request,
                split_policy_spec,
                created_at,
                reason,
                folds=folds,
                fold_curves=fold_curves,
                selection_failures=selection_failures,
                parent_sharpe=parent_sharpe,
                warnings=warnings,
            )

    if not any(fold.status == "completed" for fold in folds):
        reason = "every test fold failed; no auditable out-of-sample aggregate exists"
        warnings.append(reason)
        return config, _failed_wf_result(
            wf_id,
            spec_hash,
            request,
            split_policy_spec,
            created_at,
            reason,
            folds=folds,
            fold_curves=fold_curves,
            selection_failures=selection_failures,
            parent_sharpe=parent_sharpe,
            warnings=warnings,
        )
    result = _build_wf_result(
        wf_id=wf_id,
        spec_hash=spec_hash,
        request=request,
        split_policy_spec=split_policy_spec,
        created_at=created_at,
        folds=folds,
        fold_curves=fold_curves,
        selection_failures=selection_failures,
        parent_sharpe=parent_sharpe,
        warnings=warnings,
        status="completed",
    )
    return config, result


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _ms_to_inclusive_end_date(ms: int) -> Date:
    """Convert a half-open fold boundary to the *last day* of the fold.

    Split policies emit fold boundaries as half-open ms intervals
    ``[test_start_ms, test_end_ms)``. The engine's data filter is
    *inclusive* on both ends (``start <= bar.date() <= end``), so the
    exclusive end boundary must be converted to the preceding NY-local
    date. For adjacent folds this also prevents the boundary day from
    appearing in both folds; generic policies may still choose a step
    that deliberately creates a gap or overlap.

    Subtract one calendar day in NY-local time (DST-safe — uses
    ``timedelta(days=1)`` on a tz-aware datetime so spring-forward /
    fall-back doesn't shift the resulting date).
    """
    return (datetime.fromtimestamp(ms / 1000, tz=_NY) - timedelta(days=1)).date()


def _fold_to_result(
    window: FoldWindow,
    ledger: RunLedger,
    result: BacktestRunResult,
    *,
    selected_parameters: dict[str, float],
    training_candidates: list[TrainingCandidateResult],
    selected_train_sharpe: float | None,
    persisted: bool,
    result_failure_reason: str | None = None,
) -> FoldResult:
    # Mirror the underlying ledger's lifecycle status onto the fold so
    # aggregation can exclude failed folds without re-loading ledgers.
    # Phase A's RunLedger.status is one of {"running", "completed",
    # "failed"}; "running" is a transient state the runner never
    # surfaces synchronously, so collapse to a 2-value field here.
    fold_status = (
        "failed" if ledger.status == "failed" or not persisted or result_failure_reason is not None else "completed"
    )
    test_sharpe = result.metrics.sharpe_ratio
    fold_retention = (
        sharpe_retention(test_sharpe, selected_train_sharpe)
        if fold_status == "completed"
        else None
    )
    return FoldResult(
        fold_index=window.fold_index,
        train_start_ms=window.train_start_ms,
        train_end_ms=window.train_end_ms,
        test_start_ms=window.test_start_ms,
        test_end_ms=window.test_end_ms,
        test_run_id=ledger.run_id if persisted else None,
        test_metrics=result.metrics,
        test_trade_count=result.metrics.total_trades,
        status=fold_status,
        failure_reason=(
            ledger.failure_reason
            if ledger.status == "failed"
            else "test receipt could not be persisted"
            if not persisted
            else result_failure_reason
        ),
        selected_parameters=selected_parameters,
        training_candidates=training_candidates,
        selected_train_sharpe=selected_train_sharpe,
        oos_retention=fold_retention,
    )


def _parameter_search_config(
    search: ParameterSearchRequest | None,
) -> ParameterSearchConfig | None:
    if search is None:
        return None
    return ParameterSearchConfig(
        min_trades=search.min_trades,
        candidates=[
            ParameterCandidateConfig(
                parameters=dict(candidate.parameters),
                strategy_spec_hash=hash_payload(candidate.spec.model_dump(mode="json")),
                strategy_spec_json=candidate.spec.model_dump(mode="json"),
            )
            for candidate in search.candidates
        ],
    )


def _select_fold_candidate(
    request: WalkForwardRequest,
    window: FoldWindow,
    *,
    wf_id: str,
    spec_hash: str,
    data_source_factory: Any,
    artifacts_root: Any | None,
    data_root_revision: str | None,
    cancel_check: CancelCheck | None,
    on_candidate_completed: Callable[[str], None] | None,
) -> tuple[
    StrategySpec,
    dict[str, float],
    list[TrainingCandidateResult],
    float,
    list[str],
]:
    search = request.parameter_search
    if search is None:  # pragma: no cover - caller guards
        raise ValueError("parameter search is required")

    results: list[TrainingCandidateResult] = []
    warnings: list[str] = []
    persistence_failures: list[str] = []
    for candidate_index, candidate in enumerate(search.candidates):
        if cancel_check is not None:
            cancel_check()
        train_request = RunRequest(
            spec=candidate.spec,
            start_ms=window.train_start_ms,
            end_ms=run_date_to_ms(_ms_to_inclusive_end_date(window.train_end_ms)),
            initial_cash=request.initial_cash,
            fill_mode=request.fill_mode,
            commission_per_order=request.commission_per_order,
            slippage_per_share=request.slippage_per_share,
            random_seed=request.random_seed,
            strategy_spec_id=(f"wf:{wf_id}:fold-{window.fold_index}:train-{candidate_index}"),
            parent_run_id=wf_id,
            parent_spec_hash=spec_hash,
        )
        ledger, result = run_strategy_spec(
            train_request,
            data_source_factory=data_source_factory,
            data_root_revision=data_root_revision,
        )
        persistence_warning = _persist_child_run(
            ledger,
            result,
            artifacts_root=artifacts_root,
            context=f"fold {window.fold_index} train candidate {candidate_index}",
            wf_id=wf_id,
        )
        if persistence_warning is not None:
            warnings.append(persistence_warning)
            persistence_failures.append(persistence_warning)

        ineligibility_reason = _candidate_ineligibility_reason(
            ledger,
            result,
            min_trades=search.min_trades,
        )
        results.append(
            TrainingCandidateResult(
                parameters=dict(candidate.parameters),
                train_run_id=ledger.run_id,
                train_metrics=result.metrics,
                receipt_persisted=persistence_warning is None,
                eligible=ineligibility_reason is None,
                ineligibility_reason=ineligibility_reason,
            )
        )
        if on_candidate_completed is not None:
            on_candidate_completed(
                "completed training candidate "
                f"{candidate_index + 1} of {len(search.candidates)} "
                f"for fold {window.fold_index + 1}"
            )

    if persistence_failures:
        raise ParameterSelectionError(
            "training candidate receipt persistence failed; refusing to test an incompletely auditable selection",
            candidates=tuple(results),
            warnings=tuple(warnings),
        )
    try:
        winner_index = select_candidate_index(results)
    except ParameterSelectionError as exc:
        raise ParameterSelectionError(
            str(exc),
            candidates=tuple(results),
            warnings=tuple(warnings),
        ) from exc
    winner = search.candidates[winner_index]
    winner_sharpe = results[winner_index].train_metrics.sharpe_ratio
    if winner_sharpe is None:  # pragma: no cover - selection rejects this
        raise AssertionError("selected train candidate has null Sharpe")
    return winner.spec, dict(winner.parameters), results, winner_sharpe, warnings


def _candidate_ineligibility_reason(
    ledger: RunLedger,
    result: BacktestRunResult,
    *,
    min_trades: int,
) -> str | None:
    if ledger.status != "completed":
        return ledger.failure_reason or "training run failed"
    if result.metrics.total_trades < min_trades:
        return f"train trades {result.metrics.total_trades} below minimum {min_trades}"
    if result.metrics.sharpe_ratio is None:
        return "train Sharpe is null"
    return None


def _persist_child_run(
    ledger: RunLedger,
    result: BacktestRunResult,
    *,
    artifacts_root: Any | None,
    context: str,
    wf_id: str,
) -> str | None:
    try:
        save_run(ledger, result, root=artifacts_root)
    except Exception as exc:
        logger.exception(
            "[WF] failed to persist %s of walk_forward=%s",
            context,
            wf_id,
        )
        return f"{context} could not be persisted: {exc}"
    return None


def _alpha_decay(folds: list[FoldResult]) -> float | None:
    """Slope of ``test_metrics.sharpe_ratio`` vs ``fold_index`` via OLS.

    Returns ``None`` when fewer than 2 folds have non-None sharpe — a
    single point can't establish a slope. Negative slopes indicate
    decay; positive slopes indicate the strategy is still working
    (or improving). The metric is meant to be *directional*, not a
    pass/fail gate.
    """
    points = [(f.fold_index, f.test_metrics.sharpe_ratio) for f in folds if f.test_metrics.sharpe_ratio is not None]
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


def _oos_retention(mean_oos_sharpe: float | None, parent_sharpe: float | None) -> float | None:
    """Ratio of OOS Sharpe to the parent full-window Sharpe.

    Formula: oos_retention = mean_oos_sharpe / parent_sharpe.
    Reference: Internal Build Alpha-style validation contract; see
      docs/references/walk-forward.md.
    Canonical implementation: this file.
    Validated against: tests/research/walk_forward/test_runner.py::test_oos_retention_uses_parent_sharpe
    """
    return sharpe_retention(mean_oos_sharpe, parent_sharpe)


def _compound_oos_curve(
    fold_curves: list[list[EquityCurvePoint]],
) -> list[EquityCurvePoint]:
    """Concatenate fold equity curves with compounding.

    Each fold's curve normally starts at the same configured
    ``initial_cash`` (because every fold runs through the same
    ``RunRequest.initial_cash`` value) and ends at some terminal
    equity. To produce a compounded sequence of independently-flat OOS
    returns, fold N+1's curve is *scaled* so its first point equals
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
    split_policy_spec: SplitPolicySpec,
    created_at: int,
    reason: str,
    *,
    folds: list[FoldResult] | None = None,
    fold_curves: list[list[EquityCurvePoint]] | None = None,
    selection_failures: list[SelectionFailureResult] | None = None,
    parent_sharpe: float | None = None,
    warnings: list[str] | None = None,
) -> WalkForwardResult:
    """Build a failed result without discarding already-produced evidence."""
    return _build_wf_result(
        wf_id=wf_id,
        spec_hash=spec_hash,
        request=request,
        split_policy_spec=split_policy_spec,
        created_at=created_at,
        folds=folds or [],
        fold_curves=fold_curves or [],
        selection_failures=selection_failures or [],
        parent_sharpe=parent_sharpe,
        warnings=warnings or [reason],
        status="failed",
        failure_reason=reason,
    )


def _build_wf_result(
    *,
    wf_id: str,
    spec_hash: str,
    request: WalkForwardRequest,
    split_policy_spec: SplitPolicySpec,
    created_at: int,
    folds: list[FoldResult],
    fold_curves: list[list[EquityCurvePoint]],
    selection_failures: list[SelectionFailureResult],
    parent_sharpe: float | None,
    warnings: list[str],
    status: Literal["completed", "failed"],
    failure_reason: str | None = None,
) -> WalkForwardResult:
    """Aggregate only auditable completed folds into one typed result."""
    successful_folds = [fold for fold in folds if fold.status == "completed"]
    sharpes = [
        fold.test_metrics.sharpe_ratio for fold in successful_folds if fold.test_metrics.sharpe_ratio is not None
    ]
    profitable_count = sum(1 for fold in successful_folds if fold.test_metrics.total_return_pct > 0)
    pct_profitable = profitable_count / len(successful_folds) if successful_folds else None
    mean_oos_sharpe = statistics.fmean(sharpes) if sharpes else None
    median_oos_sharpe = statistics.median(sharpes) if sharpes else None

    if request.parameter_search is not None:
        oos_retention = _mean_fold_retention(successful_folds)
        retention_basis: (
            Literal[
                "mean_fold_test_to_selected_train",
                "mean_oos_to_parent",
            ]
            | None
        ) = "mean_fold_test_to_selected_train"
    else:
        oos_retention = _oos_retention(mean_oos_sharpe, parent_sharpe)
        retention_basis = "mean_oos_to_parent" if parent_sharpe is not None else None

    return WalkForwardResult(
        walk_forward_id=wf_id,
        parent_run_id=request.parent_run_id,
        strategy_spec_hash=spec_hash,
        split_policy=split_policy_spec,
        folds=folds,
        selection_failures=selection_failures,
        combined_oos_equity_curve=_compound_oos_curve(fold_curves),
        mean_oos_sharpe=mean_oos_sharpe,
        median_oos_sharpe=median_oos_sharpe,
        pct_profitable_folds=pct_profitable,
        oos_retention=oos_retention,
        oos_retention_basis=retention_basis,
        alpha_decay=_alpha_decay(successful_folds),
        warnings=warnings,
        created_at_ms=created_at,
        completed_at_ms=now_ms_utc(),
        status=status,
        failure_reason=failure_reason,
    )


def _mean_fold_retention(folds: list[FoldResult]) -> float | None:
    """Equal-weight mean of eligible fold-local test/train Sharpe ratios.

    This deliberately differs from a ratio of aggregate Sharpes. Each fold's
    TEST result is compared only with the train-side winner that selected it,
    then the eligible ratios receive equal weight.
    """
    return mean_fold_retention(fold.oos_retention for fold in folds)
