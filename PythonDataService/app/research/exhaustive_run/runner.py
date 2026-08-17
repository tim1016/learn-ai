"""Orchestrate the frozen SPY EMA Exhaustive Run protocol.

The source walk-forward remains immutable. This runner reads its persisted
training receipts, retains up to five candidates per fold, deduplicates exact
specifications, then produces two linked evidence tracks per unique spec:

* one explicitly full-data two-year run (selection/look-ahead biased);
* one fixed-spec walk-forward over all 18 canonical forward test windows.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Callable, Sequence
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.strategy.spec import StrategySpec
from app.research.artifact.errors import ArtifactError
from app.research.exhaustive_run.errors import ExhaustiveRunSourceError
from app.research.exhaustive_run.metrics import summarize_trade_recency
from app.research.exhaustive_run.result import (
    CandidateFoldEvidence,
    CandidateForwardStability,
    ExhaustiveCandidateResult,
    ExhaustiveRunConfig,
    ExhaustiveRunResult,
    FoldCandidateSelection,
)
from app.research.exhaustive_run.selection import (
    select_fold_candidates,
    selected_candidate_configs,
)
from app.research.exhaustive_run.storage import save_exhaustive_run
from app.research.runs import RunRequest, load_run, run_strategy_spec, save_run
from app.research.runs.hashing import hash_payload
from app.research.runs.ledger import resolve_data_root_revision
from app.research.walk_forward.metrics import mean_fold_retention, sharpe_retention
from app.research.walk_forward.result import (
    FoldResult,
    ParameterCandidateConfig,
    WalkForwardConfig,
    WalkForwardResult,
)
from app.research.walk_forward.runner import CancelCheck, WalkForwardRequest, run_walk_forward
from app.research.walk_forward.splits import build_split_policy
from app.research.walk_forward.spy_ema import (
    SPY_EMA_PROTOCOL_END_MS,
    SPY_EMA_PROTOCOL_ID,
    SPY_EMA_PROTOCOL_START_MS,
    SPY_EMA_PROTOCOL_VERSION,
)
from app.research.walk_forward.storage import load_walk_forward, save_walk_forward
from app.utils.timestamps import now_ms_utc

EXHAUSTIVE_PROTOCOL_ID = "spy-ema-exhaustive-run"
EXHAUSTIVE_PROTOCOL_VERSION = "1.0"
EXHAUSTIVE_RECENT_START_MS = 1_769_922_000_000
MAX_CANDIDATES_PER_FOLD = 5
_FIXED_STABILITY_PROTOCOL_ID = "spy-ema-exhaustive-fixed-gap"
_NY = ZoneInfo("America/New_York")
ProgressCallback = Callable[[int, int, str], None]


def run_exhaustive_analysis(
    source_walk_forward_id: str,
    *,
    data_source_factory: Any,
    artifacts_root: Path | None = None,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> tuple[ExhaustiveRunConfig, ExhaustiveRunResult]:
    """Run, persist, and return one frozen Exhaustive Run analysis."""
    source_config, source_result = load_walk_forward(
        source_walk_forward_id,
        root=artifacts_root,
    )
    _validate_source(source_config, source_result)
    selections = select_fold_candidates(
        source_config,
        source_result,
        max_per_fold=MAX_CANDIDATES_PER_FOLD,
    )
    candidate_configs = selected_candidate_configs(source_config, selections)
    if not candidate_configs:
        raise ExhaustiveRunSourceError("source walk-forward produced no eligible candidates")

    data_revision = _validated_source_revision(
        source_config,
        selections,
        artifacts_root=artifacts_root,
    )
    exhaustive_run_id = uuid.uuid4().hex
    created_at_ms = now_ms_utc()
    config = ExhaustiveRunConfig(
        exhaustive_run_id=exhaustive_run_id,
        source_walk_forward_id=source_walk_forward_id,
        source_protocol_id=source_config.protocol_id or "",
        source_protocol_version=source_config.protocol_version or "",
        start_ms=source_config.start_ms,
        end_ms=source_config.end_ms,
        recent_window_start_ms=EXHAUSTIVE_RECENT_START_MS,
        initial_cash=source_config.initial_cash,
        fill_mode=source_config.fill_mode,
        commission_per_order=source_config.commission_per_order,
        slippage_per_share=source_config.slippage_per_share,
        random_seed=source_config.random_seed,
        data_root_revision=data_revision,
        created_at_ms=created_at_ms,
    )

    split_policy = build_split_policy(source_config.split_policy.model_dump(mode="json"))
    fold_count = len(split_policy.folds(source_config.start_ms, source_config.end_ms))
    total_runs = len(candidate_configs) * (fold_count + 1)
    completed_runs = 0
    candidate_results: list[ExhaustiveCandidateResult] = []
    warnings: list[str] = []

    for candidate_config in candidate_configs:
        if cancel_check is not None:
            cancel_check()
        candidate_selections = [
            selection
            for selection in selections
            if selection.strategy_spec_hash == candidate_config.strategy_spec_hash
        ]
        progress_base = completed_runs
        try:
            candidate = _run_candidate(
                exhaustive_run_id=exhaustive_run_id,
                source_config=source_config,
                source_result=source_result,
                candidate_config=candidate_config,
                selections=candidate_selections,
                data_revision=data_revision,
                data_source_factory=data_source_factory,
                artifacts_root=artifacts_root,
                cancel_check=cancel_check,
                on_full_run_completed=lambda message, base=progress_base: _emit_progress(
                    on_progress,
                    base + 1,
                    total_runs,
                    message,
                ),
                on_fold_completed=lambda current, message, base=progress_base: _emit_progress(
                    on_progress,
                    base + 1 + current,
                    total_runs,
                    message,
                ),
            )
        except (ArtifactError, OSError, RuntimeError, ValueError) as exc:
            reason = f"candidate {candidate_config.parameters} failed: {exc}"
            warnings.append(reason)
            candidate = _failed_candidate(
                candidate_config,
                candidate_selections,
                reason,
            )
        candidate_results.append(candidate)
        completed_runs += fold_count + 1

    failed = [candidate for candidate in candidate_results if candidate.status == "failed"]
    status = "failed" if failed else "completed"
    failure_reason = (
        f"{len(failed)} of {len(candidate_results)} unique candidates failed"
        if failed
        else None
    )
    result = ExhaustiveRunResult(
        exhaustive_run_id=exhaustive_run_id,
        source_walk_forward_id=source_walk_forward_id,
        selections=selections,
        candidates=candidate_results,
        warnings=warnings,
        status=status,
        failure_reason=failure_reason,
        created_at_ms=created_at_ms,
        completed_at_ms=now_ms_utc(),
    )
    save_exhaustive_run(config, result, root=artifacts_root)
    return config, result


def _run_candidate(
    *,
    exhaustive_run_id: str,
    source_config: WalkForwardConfig,
    source_result: WalkForwardResult,
    candidate_config: ParameterCandidateConfig,
    selections: Sequence[FoldCandidateSelection],
    data_revision: str,
    data_source_factory: Any,
    artifacts_root: Path | None,
    cancel_check: CancelCheck | None,
    on_full_run_completed: Callable[[str], None],
    on_fold_completed: Callable[[int, str], None],
) -> ExhaustiveCandidateResult:
    spec = StrategySpec.model_validate(candidate_config.strategy_spec_json)
    actual_hash = hash_payload(spec.model_dump(mode="json"))
    if actual_hash != candidate_config.strategy_spec_hash:
        raise ExhaustiveRunSourceError("candidate specification hash does not match persisted lineage")

    full_request = RunRequest(
        spec=spec,
        start_ms=source_config.start_ms,
        end_ms=source_config.end_ms,
        initial_cash=source_config.initial_cash,
        fill_mode=source_config.fill_mode,
        commission_per_order=source_config.commission_per_order,
        slippage_per_share=source_config.slippage_per_share,
        random_seed=source_config.random_seed,
        strategy_spec_id=f"exhaustive:{exhaustive_run_id}:{candidate_config.strategy_spec_hash[:12]}",
        parent_run_id=exhaustive_run_id,
        parent_spec_hash=source_config.strategy_spec_hash,
    )
    full_ledger, full_result = run_strategy_spec(
        full_request,
        data_source_factory=data_source_factory,
        data_root_revision=data_revision,
    )
    save_run(full_ledger, full_result, root=artifacts_root)
    if full_ledger.status != "completed" or full_result.bars_consumed == 0:
        raise RuntimeError(full_ledger.failure_reason or "full-period run produced no auditable bars")
    on_full_run_completed(f"completed full-period run for {_parameter_label(candidate_config)}")

    if cancel_check is not None:
        cancel_check()
    split_policy = build_split_policy(source_config.split_policy.model_dump(mode="json"))
    stability_request = WalkForwardRequest(
        spec=spec,
        start_ms=source_config.start_ms,
        end_ms=source_config.end_ms,
        split_policy=split_policy,
        initial_cash=source_config.initial_cash,
        fill_mode=source_config.fill_mode,
        commission_per_order=source_config.commission_per_order,
        slippage_per_share=source_config.slippage_per_share,
        random_seed=source_config.random_seed,
        parent_run_id=full_ledger.run_id,
        protocol_id=_FIXED_STABILITY_PROTOCOL_ID,
        protocol_version=EXHAUSTIVE_PROTOCOL_VERSION,
    )

    def report_fold(current: int, _total: int, message: str) -> None:
        on_fold_completed(current, f"{_parameter_label(candidate_config)} · {message}")

    stability_config, stability_result = run_walk_forward(
        stability_request,
        data_source_factory=data_source_factory,
        artifacts_root=artifacts_root,
        data_root_revision=data_revision,
        on_progress=report_fold,
        cancel_check=cancel_check,
    )
    save_walk_forward(stability_config, stability_result, root=artifacts_root)
    if (
        stability_result.status != "completed"
        or len(stability_result.folds) != len(source_result.folds)
        or any(fold.status != "completed" for fold in stability_result.folds)
    ):
        raise RuntimeError(
            stability_result.failure_reason
            or "fixed-gap forward stability did not complete all 18 folds"
        )

    fold_evidence = _candidate_fold_evidence(
        source_result.folds,
        stability_result.folds,
        candidate_config,
    )
    retention = mean_fold_retention(fold.retention for fold in fold_evidence)
    appearance_indices = sorted(selection.fold_index for selection in selections)
    recency = summarize_trade_recency(
        full_result.trades,
        study_start_ms=source_config.start_ms,
        recent_start_ms=EXHAUSTIVE_RECENT_START_MS,
        study_end_ms=source_config.end_ms,
    )
    return ExhaustiveCandidateResult(
        parameters=dict(candidate_config.parameters),
        strategy_spec_hash=candidate_config.strategy_spec_hash,
        appearance_count=len(selections),
        appearance_fold_indices=appearance_indices,
        best_fold_rank=min(selection.fold_rank for selection in selections),
        latest_selected_fold_index=max(appearance_indices),
        mean_selection_score=statistics.fmean(
            selection.selection_score for selection in selections
        ),
        status="completed",
        full_period_run_id=full_ledger.run_id,
        full_period_metrics=full_result.metrics,
        trade_recency=recency,
        forward_stability=CandidateForwardStability(
            walk_forward_id=stability_result.walk_forward_id,
            folds=fold_evidence,
            pct_profitable_folds=stability_result.pct_profitable_folds,
            mean_oos_sharpe=stability_result.mean_oos_sharpe,
            median_oos_sharpe=stability_result.median_oos_sharpe,
            alpha_decay=stability_result.alpha_decay,
            mean_fold_retention=retention,
        ),
    )


def _candidate_fold_evidence(
    source_folds: Sequence[FoldResult],
    stability_folds: Sequence[FoldResult],
    candidate_config: ParameterCandidateConfig,
) -> list[CandidateFoldEvidence]:
    test_by_index = {fold.fold_index: fold for fold in stability_folds}
    evidence: list[CandidateFoldEvidence] = []
    for source_fold in source_folds:
        training = next(
            (
                candidate
                for candidate in source_fold.training_candidates
                if candidate.parameters == candidate_config.parameters
            ),
            None,
        )
        test_fold = test_by_index.get(source_fold.fold_index)
        if training is None or test_fold is None:
            raise ExhaustiveRunSourceError(
                f"candidate evidence is incomplete for fold {source_fold.fold_index}"
            )
        retention = (
            sharpe_retention(
                test_fold.test_metrics.sharpe_ratio,
                training.train_metrics.sharpe_ratio,
            )
            if test_fold.status == "completed"
            else None
        )
        evidence.append(
            CandidateFoldEvidence(
                fold_index=source_fold.fold_index,
                train_start_ms=source_fold.train_start_ms,
                train_end_ms=source_fold.train_end_ms,
                test_start_ms=source_fold.test_start_ms,
                test_end_ms=source_fold.test_end_ms,
                train_run_id=training.train_run_id,
                test_run_id=test_fold.test_run_id,
                train_metrics=training.train_metrics,
                test_metrics=test_fold.test_metrics,
                retention=retention,
                status=test_fold.status,
                failure_reason=test_fold.failure_reason,
            )
        )
    return evidence


def _failed_candidate(
    candidate_config: ParameterCandidateConfig,
    selections: Sequence[FoldCandidateSelection],
    reason: str,
) -> ExhaustiveCandidateResult:
    indices = sorted(selection.fold_index for selection in selections)
    return ExhaustiveCandidateResult(
        parameters=dict(candidate_config.parameters),
        strategy_spec_hash=candidate_config.strategy_spec_hash,
        appearance_count=len(selections),
        appearance_fold_indices=indices,
        best_fold_rank=min(selection.fold_rank for selection in selections),
        latest_selected_fold_index=max(indices),
        mean_selection_score=statistics.fmean(
            selection.selection_score for selection in selections
        ),
        status="failed",
        failure_reason=reason,
    )


def _validate_source(config: WalkForwardConfig, result: WalkForwardResult) -> None:
    if config.protocol_id != SPY_EMA_PROTOCOL_ID or config.protocol_version != SPY_EMA_PROTOCOL_VERSION:
        raise ExhaustiveRunSourceError("Exhaustive Run requires canonical SPY EMA V1 evidence")
    if config.start_ms != SPY_EMA_PROTOCOL_START_MS or config.end_ms != SPY_EMA_PROTOCOL_END_MS:
        raise ExhaustiveRunSourceError("source walk-forward does not use the frozen two-year window")
    if config.parameter_search is None:
        raise ExhaustiveRunSourceError("source walk-forward has no candidate grid")
    if (
        result.status != "completed"
        or len(result.folds) != 18
        or any(fold.status != "completed" for fold in result.folds)
    ):
        raise ExhaustiveRunSourceError("source walk-forward must contain 18 completed protocol folds")


def _validated_source_revision(
    config: WalkForwardConfig,
    selections: Sequence[FoldCandidateSelection],
    *,
    artifacts_root: Path | None,
) -> str:
    revisions: set[str] = set()
    for selection in selections:
        ledger, _result = load_run(selection.train_run_id, root=artifacts_root)
        revisions.add(ledger.data_snapshot_id.rsplit("|", maxsplit=1)[-1])
    if len(revisions) != 1:
        raise ExhaustiveRunSourceError("selected training receipts do not share one data revision")
    source_revision = next(iter(revisions))
    current_revision = resolve_data_root_revision(
        symbol=config.symbol,
        start_date=_date_from_ms(config.start_ms),
        end_date=_date_from_ms(config.end_ms),
    )
    if current_revision != source_revision:
        raise ExhaustiveRunSourceError(
            "local market-data revision changed after the source walk-forward; rerun the canonical study first"
        )
    return source_revision


def _date_from_ms(value: int) -> Date:
    return datetime.fromtimestamp(value / 1000, tz=_NY).date()


def _parameter_label(candidate: ParameterCandidateConfig) -> str:
    gap = candidate.parameters.get("gap_bps")
    return f"{gap:g} bps" if gap is not None else candidate.strategy_spec_hash[:12]


def _emit_progress(
    callback: ProgressCallback | None,
    current: int,
    total: int,
    message: str,
) -> None:
    if callback is not None:
        callback(current, total, message)
