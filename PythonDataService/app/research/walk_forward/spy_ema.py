"""Production research pipeline for SPY EMA normalized-gap walk-forward.

The frozen V1 protocol uses 180-day training windows, 30-day test windows,
and 30-day steps. Orders fill at the next bar open with zero commission and
zero slippage. See ``docs/references/spy-ema-normalized-gap-walk-forward.md``
for the source, fixture, and tolerance record.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.engine.strategy.spec import StrategySpec, load_spec_from_path
from app.engine.strategy.spec.schema import (
    ConstOperand,
    DifferenceBps,
    IndicatorComparison,
)
from app.research.runs import (
    BacktestRunResult,
    RunLedger,
    RunRequest,
    run_strategy_spec,
    save_run,
)
from app.research.runs.ledger import resolve_data_root_revision
from app.research.walk_forward.result import WalkForwardConfig, WalkForwardResult
from app.research.walk_forward.runner import (
    CancelCheck,
    WalkForwardRequest,
    run_walk_forward,
)
from app.research.walk_forward.selection import (
    ParameterCandidate,
    ParameterSearchRequest,
)
from app.research.walk_forward.splits import RollingSplitPolicy, SplitPolicy
from app.research.walk_forward.storage import save_walk_forward

DEFAULT_GAP_THRESHOLDS_BPS = (1.0, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0)
SPY_EMA_PROTOCOL_ID = "spy-ema-normalized-gap"
SPY_EMA_PROTOCOL_VERSION = "1.0"
SPY_EMA_PROTOCOL_START_MS = 1_722_484_800_000
SPY_EMA_PROTOCOL_END_MS = 1_785_556_800_000

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "engine" / "strategy" / "spec" / "fixtures"
_CONTROL_FIXTURE = _FIXTURE_ROOT / "spy_ema_crossover.spec.json"
_NORMALIZED_FIXTURE = _FIXTURE_ROOT / "spy_ema_normalized_gap.spec.json"
_NY = ZoneInfo("America/New_York")


class SpyEmaPipelineError(RuntimeError):
    """Raised when the control run cannot establish a valid pipeline parent."""


@dataclass(frozen=True)
class SpyEmaPipelineRequest:
    start_ms: int
    end_ms: int
    split_policy: SplitPolicy
    thresholds_bps: tuple[float, ...] = DEFAULT_GAP_THRESHOLDS_BPS
    min_train_trades: int = 5
    initial_cash: float = 100_000.0
    fill_mode: str = "next_bar_open"
    commission_per_order: float = 0.0
    slippage_per_share: float = 0.0
    random_seed: int = 0
    protocol_id: str | None = None
    protocol_version: str | None = None

    def __post_init__(self) -> None:
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be strictly before end_ms")
        if (self.protocol_id is None) != (self.protocol_version is None):
            raise ValueError("protocol_id and protocol_version must be provided together")
        if len(self.thresholds_bps) < 2:
            raise ValueError("at least two gap thresholds are required")
        if len(set(self.thresholds_bps)) != len(self.thresholds_bps):
            raise ValueError("gap thresholds must be unique")
        if tuple(sorted(self.thresholds_bps)) != self.thresholds_bps:
            raise ValueError("gap thresholds must be in ascending order")
        if any(not math.isfinite(threshold) or threshold <= 0 or threshold > 100 for threshold in self.thresholds_bps):
            raise ValueError("gap thresholds must be in (0, 100]")
        if self.min_train_trades < 1:
            raise ValueError("min_train_trades must be at least 1")
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if not math.isfinite(self.commission_per_order) or self.commission_per_order < 0:
            raise ValueError("commission_per_order must be finite and non-negative")
        if not math.isfinite(self.slippage_per_share) or self.slippage_per_share < 0:
            raise ValueError("slippage_per_share must be finite and non-negative")


@dataclass(frozen=True)
class SpyEmaPipelineResult:
    control_ledger: RunLedger
    control_result: BacktestRunResult
    walk_forward_config: WalkForwardConfig
    walk_forward_result: WalkForwardResult


def frozen_spy_ema_v1_request() -> SpyEmaPipelineRequest:
    """Return the immutable, server-owned V1 research protocol."""
    return SpyEmaPipelineRequest(
        start_ms=SPY_EMA_PROTOCOL_START_MS,
        end_ms=SPY_EMA_PROTOCOL_END_MS,
        split_policy=RollingSplitPolicy(
            train_days=180,
            test_days=30,
            step_days=30,
        ),
        protocol_id=SPY_EMA_PROTOCOL_ID,
        protocol_version=SPY_EMA_PROTOCOL_VERSION,
    )


def frozen_spy_ema_v1_spec() -> StrategySpec:
    """Load the committed normalized-gap specification owned by V1."""
    return load_spec_from_path(_NORMALIZED_FIXTURE)


def build_spy_ema_gap_candidates(
    thresholds_bps: tuple[float, ...],
) -> tuple[ParameterCandidate, ...]:
    """Materialize an ordered candidate grid from the committed V1 spec."""
    normalized_spec = frozen_spy_ema_v1_spec()
    return tuple(
        ParameterCandidate(
            parameters={"gap_bps": threshold},
            spec=materialize_gap_threshold(normalized_spec, threshold),
        )
        for threshold in thresholds_bps
    )


def run_spy_ema_pipeline(
    request: SpyEmaPipelineRequest,
    *,
    data_source_factory: Any,
    artifacts_root: Any | None = None,
    data_root_revision: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel_check: CancelCheck | None = None,
) -> SpyEmaPipelineResult:
    """Run and persist the absolute-gap control plus optimized OOS study."""
    start_date = datetime.fromtimestamp(request.start_ms / 1000, tz=_NY).date()
    end_date = datetime.fromtimestamp(request.end_ms / 1000, tz=_NY).date()
    pinned_revision = data_root_revision or resolve_data_root_revision(
        symbol="SPY",
        start_date=start_date,
        end_date=end_date,
    )
    fold_count = len(request.split_policy.folds(request.start_ms, request.end_ms))
    total_steps = 1 + fold_count * (len(request.thresholds_bps) + 1)
    if cancel_check is not None:
        cancel_check()
    control_spec = load_spec_from_path(_CONTROL_FIXTURE)
    control_request = RunRequest(
        spec=control_spec,
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        strategy_spec_id="spy_ema_crossover",
    )
    control_ledger, control_result = run_strategy_spec(
        control_request,
        data_source_factory=data_source_factory,
        data_root_revision=pinned_revision,
    )
    save_run(control_ledger, control_result, root=artifacts_root)
    if control_ledger.status != "completed":
        raise SpyEmaPipelineError(control_ledger.failure_reason or "canonical control run failed")
    if on_progress is not None:
        on_progress(1, total_steps, "persisted the canonical control run")

    normalized_spec = frozen_spy_ema_v1_spec()
    candidates = build_spy_ema_gap_candidates(request.thresholds_bps)
    wf_request = WalkForwardRequest(
        spec=normalized_spec,
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        split_policy=request.split_policy,
        initial_cash=request.initial_cash,
        fill_mode=request.fill_mode,
        commission_per_order=request.commission_per_order,
        slippage_per_share=request.slippage_per_share,
        random_seed=request.random_seed,
        parent_run_id=control_ledger.run_id,
        parameter_search=ParameterSearchRequest(
            candidates=candidates,
            min_trades=request.min_train_trades,
        ),
        protocol_id=request.protocol_id,
        protocol_version=request.protocol_version,
    )

    def report_child_run(current: int, _total: int, message: str) -> None:
        if on_progress is not None:
            on_progress(current + 1, total_steps, message)

    config, result = run_walk_forward(
        wf_request,
        data_source_factory=data_source_factory,
        artifacts_root=artifacts_root,
        data_root_revision=pinned_revision,
        on_progress=report_child_run,
        cancel_check=cancel_check,
    )
    save_walk_forward(config, result, root=artifacts_root)
    return SpyEmaPipelineResult(
        control_ledger=control_ledger,
        control_result=control_result,
        walk_forward_config=config,
        walk_forward_result=result,
    )


def materialize_gap_threshold(
    base_spec: StrategySpec,
    threshold_bps: float,
) -> StrategySpec:
    """Return a typed copy with the sole DifferenceBps threshold replaced."""
    if not math.isfinite(threshold_bps) or threshold_bps <= 0 or threshold_bps > 100:
        raise ValueError("gap threshold must be finite and in (0, 100]")
    conditions = []
    matches = 0
    for condition in base_spec.entry.conditions:
        if isinstance(condition, IndicatorComparison) and isinstance(condition.left, DifferenceBps):
            if not isinstance(condition.right, ConstOperand):
                raise ValueError("DifferenceBps condition must compare against Const")
            condition = condition.model_copy(
                update={
                    "right": ConstOperand(kind="Const", value=threshold_bps),
                }
            )
            matches += 1
        conditions.append(condition)
    if matches != 1:
        raise ValueError(f"normalized SPY EMA spec requires exactly one DifferenceBps condition; got {matches}")
    entry = base_spec.entry.model_copy(update={"conditions": conditions})
    return base_spec.model_copy(update={"entry": entry})
