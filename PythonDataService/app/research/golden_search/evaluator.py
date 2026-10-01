"""Golden Search's one evaluation boundary: execution context, cache identity, capability and budget (#2696, ADR 0074).

Formula:
  * ``context_digest`` — SHA-256 of the canonical JSON of the execution
    context frozen at lock: strategy, program version, parameter schema
    version, code identity (source, environment and digest scheme; the git
    revision and tree state describe provenance and are left out), the data
    snapshot digest, execution assumptions, data policy, metric convention
    and the common run-up. Two studies share an evaluation only if every
    one of those agrees.
  * ``evaluation_key = sha256(canonical_json([context_digest, point_hash,
    window_start_ms, window_end_ms, scenario, detail]))`` — the point hash is
    unique only inside its context, so the key carries the context.
  * capability — an evaluation window must lie inside one interval the
    stage is allowed to score (search, validation and evidence: the
    development interval; exam and proof: the final interval), else
    :class:`CapabilityError` and nothing is dispatched. Warmup sessions
    before the window only prime indicators and are always allowed.
  * warmup — every window reads exactly ``run_up_sessions`` NYSE sessions
    before its start (the canonical calendar), so every evaluation in a
    study is primed to the same depth.
  * budget — a new evaluation atomically reserves one unit and inserts its
    pending row in one transaction under the attempt fence, admitted only
    while ``consumed + 1 <= limit``; the limit keeps the exam and proof
    reservations out of reach of the development stages. A recorded result
    is a cache hit and consumes nothing; a pending row left by a crashed
    attempt is re-run without new budget, at most ``RETRY_ALLOWANCE`` times,
    then recorded failed.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Small
  interfaces with substantial behavior behind them" (Evaluator) and
  "Workload and progress"; the engine projection is
  ``app/research/grid_search/engine_adapter.py``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_evaluator.py,
  tests/research/golden_search/test_study_service.py.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException

from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search import repository as repo
from app.research.golden_search.declarations import point_hash as hash_point
from app.research.golden_search.evidence import daily_equity
from app.research.golden_search.protocol import BASE_SCENARIO, ExecutionAssumptions, StressScenario, canonical_json
from app.research.golden_search.selection import Metrics
from app.research.golden_search.zoom import BudgetExhausted
from app.research.grid_search.service import window_dates
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.grid import RunSpec
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

METRIC_CONVENTION = "engine-statistics/v1"
RETRY_ALLOWANCE = 2
EXIT_AT_WINDOW_END = "Closed at the end of the tested window"
# Provenance of the code, not its meaning: two clean trees at different commits with the
# same sources evaluate identically.
_VOLATILE_IDENTITY_FIELDS = frozenset({"git_revision", "tree_state"})

Window = tuple[int, int]


class CapabilityError(RuntimeError):
    """An evaluation window lies outside every interval this stage may score."""


@dataclass(frozen=True)
class EvaluationCapability:
    """The half-open intervals a stage may score; a window must fit inside one of them."""

    allowed: tuple[Window, ...]

    def check(self, window: Window) -> None:
        start, end = window
        if not start < end:
            raise CapabilityError(f"the evaluation window [{start}, {end}) is empty")
        if not any(low <= start and end <= high for low, high in self.allowed):
            raise CapabilityError(
                f"the evaluation window [{start}, {end}) is outside every interval this stage may score {list(self.allowed)}"
            )


def execution_context(
    *,
    strategy_key: str,
    program_version: str | None,
    parameter_schema_version: str | None,
    code_identity: Mapping[str, Any],
    data_snapshot_digest: str,
    execution: ExecutionAssumptions,
    data_policy: Mapping[str, Any],
    run_up: Mapping[str, Any],
) -> dict[str, Any]:
    """Everything an evaluation's result depends on besides its point, window and scenario."""
    return {
        "strategy_key": strategy_key,
        "program_version": program_version,
        "parameter_schema_version": parameter_schema_version,
        "code_identity": dict(code_identity),
        "data_snapshot_digest": data_snapshot_digest,
        "execution": {
            "fill_mode": execution.fill_mode,
            "commission_per_order": execution.commission_per_order,
            "slippage_per_share": execution.slippage_per_share,
            "initial_cash": execution.initial_cash,
        },
        "data_policy": dict(data_policy),
        "metric_convention": METRIC_CONVENTION,
        "run_up": dict(run_up),
    }


def context_digest(context: Mapping[str, Any]) -> str:
    """SHA-256 of the context's canonical JSON with the code identity's provenance-only fields removed."""
    stable = {**context, "code_identity": {k: v for k, v in context["code_identity"].items() if k not in _VOLATILE_IDENTITY_FIELDS}}
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


def evaluation_key(digest: str, point_hash: str, window: Window, scenario: str, detail: bool) -> str:
    return hashlib.sha256(canonical_json([digest, point_hash, window[0], window[1], scenario, detail]).encode("utf-8")).hexdigest()


def warmup_session(window_start_ms: int, run_up_sessions: int) -> date:
    """The NYSE session ``run_up_sessions`` sessions before the window's first day."""
    if run_up_sessions < 1:
        raise ValueError("a primed window reads at least one run-up session")
    first_day = et_date_at_ms(window_start_ms)
    lookback = run_up_sessions * 2 + 14
    while True:
        sessions = expected_sessions(first_day - timedelta(days=lookback), first_day - timedelta(days=1))
        if len(sessions) >= run_up_sessions:
            return sessions[-run_up_sessions]
        lookback *= 2


# ── The engine call ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvaluationRequest:
    """One engine evaluation the evaluator dispatches: a canonical point over a primed window."""

    point: dict[str, Any]
    point_hash: str
    window: Window
    warmup_from: date
    scenario: StressScenario | None
    detail: bool
    stage: str
    fold_index: int | None


@dataclass(frozen=True)
class EvaluationResult:
    metrics: Metrics
    # Bounded detail: ``initial_cash``, daily equity points and the trade list.
    detail: dict[str, Any] | None = None


ExecuteBacktest = Callable[[EvaluationRequest], EvaluationResult]


def engine_request(request: EvaluationRequest, *, strategy_key: str, execution: ExecutionAssumptions) -> EngineBacktestRequest:
    """The engine request for one evaluation: the window's ET dates, the common run-up and the scenario's costs."""
    start, end = window_dates(*request.window)
    scenario = request.scenario
    return EngineBacktestRequest(
        strategy_name=strategy_key,
        params=dict(request.point),
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        warmup_from_date=request.warmup_from.isoformat() if request.warmup_from < start else None,
        fill_mode=(scenario.fill_mode if scenario is not None and scenario.fill_mode else execution.fill_mode),
        commission_per_order=execution.commission_per_order + (scenario.commission_add if scenario else 0.0),
        slippage_per_share=execution.slippage_per_share + (scenario.slippage_add if scenario else 0.0),
        initial_cash=execution.initial_cash,
        resolution="minute",
        save_study=False,
        auto_fetch=False,
        summary_only=not request.detail,
    )


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _metrics_from_cell(
    status: str,
    *,
    total_trades: int,
    net_profit: float | None,
    total_return_pct: float | None,
    sharpe_ratio: float | None,
    max_drawdown_pct: float | None,
    win_rate: float | None,
    error: str | None,
) -> Metrics:
    return Metrics(
        status="completed" if status == "completed" else "failed",
        total_trades=int(total_trades or 0),
        net_profit=_finite(net_profit),
        total_return_pct=_finite(total_return_pct),
        sharpe_ratio=_finite(sharpe_ratio),
        max_drawdown_pct=_finite(max_drawdown_pct),
        win_rate=_finite(win_rate),
        error=error,
    )


def detail_payload(response: EngineBacktestResponse, initial_cash: float) -> dict[str, Any]:
    """The bounded evidence a detail run keeps: daily equity (session-close anchored) and the trade list.

    A trade's ``pnl`` is its price change times its filled quantity, before fees.
    """
    daily = daily_equity(response.equity_curve)
    trades = [
        {
            "entry_ms": trade.entry_time,
            "exit_ms": trade.exit_time,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "quantity": trade.quantity,
            "pnl": trade.pnl_pts * trade.quantity,
            "pnl_pct": trade.pnl_pct,
            "indicators": dict(trade.indicators),
            "exit_reason": EXIT_AT_WINDOW_END if trade.is_synthetic_exit else None,
        }
        for trade in response.trades
    ]
    return {"initial_cash": initial_cash, "daily_equity": [[ms, equity] for ms, equity in daily], "trades": trades}


def project_response(request: EvaluationRequest, response: EngineBacktestResponse, *, strategy_key: str, initial_cash: float) -> EvaluationResult:
    """Grid Search's statistics mapping (``engine_adapter.cell_from_response``), plus the detail payload when asked."""
    # The adapter imports the engine service; only a real projection needs it.
    from app.research.grid_search.engine_adapter import cell_from_response

    symbol = str(request.point["symbol"])
    cell = cell_from_response(RunSpec(symbol=symbol, strategy_key=strategy_key, params=dict(request.point), params_hash=request.point_hash), response)
    metrics = _metrics_from_cell(
        cell.status,
        total_trades=cell.total_trades,
        net_profit=cell.net_profit,
        total_return_pct=cell.total_return_pct,
        sharpe_ratio=cell.sharpe_ratio,
        max_drawdown_pct=cell.max_drawdown_pct,
        win_rate=cell.win_rate,
        error=cell.error,
    )
    detail = detail_payload(response, initial_cash) if request.detail and metrics.status == "completed" else None
    return EvaluationResult(metrics=metrics, detail=detail)


def engine_executor(
    *,
    strategy_key: str,
    execution: ExecutionAssumptions,
    manifest: Mapping[str, str],
    cancel_check: Callable[[], object] = lambda: None,
) -> ExecuteBacktest:
    """The production engine call: ``execute_engine_backtest`` with reads bound to the receipted snapshot."""

    def _execute(request: EvaluationRequest) -> EvaluationResult:
        # Loaded when an evaluation really runs, so planning and reads never import the engine service.
        from app.services.engine_backtest_service import execute_engine_backtest

        response = execute_engine_backtest(
            request=engine_request(request, strategy_key=strategy_key, execution=execution),
            on_phase=lambda phase: None,
            on_log=lambda message: None,
            data_manifest=manifest,
            while_waiting=lambda: cancel_check(),
        )
        return project_response(request, response, strategy_key=strategy_key, initial_cash=execution.initial_cash)

    return _execute


def _failure_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return f"engine refused the run: {exc.detail}"
    return f"{type(exc).__name__}: {exc}"


# ── The study evaluator ──────────────────────────────────────────────────


@dataclass
class StudyEvaluator:
    """Evaluates canonical points for one stage of one study under its attempt fence.

    ``evaluate`` returns one ``Metrics`` per input point, in input order, and
    raises ``BudgetExhausted`` when a new evaluation does not fit the limit —
    every evaluation before it is already recorded.
    """

    study_id: str
    attempt: int
    strategy_key: str
    context_digest: str
    run_up_sessions: int
    capability: EvaluationCapability
    budget_limit: int
    execute: ExecuteBacktest
    cancel_check: Callable[[], object] = lambda: None
    on_evaluated: Callable[[int], None] = lambda done: None
    new_runs: int = 0
    cache_hits: int = 0
    _warmups: dict[int, date] = field(default_factory=dict)

    def warmup_for(self, window_start_ms: int) -> date:
        if window_start_ms not in self._warmups:
            self._warmups[window_start_ms] = warmup_session(window_start_ms, self.run_up_sessions)
        return self._warmups[window_start_ms]

    def evaluate(
        self,
        points: Sequence[Mapping[str, Any]],
        *,
        window: Window,
        stage: str,
        fold_index: int | None = None,
        scenario: StressScenario | None = None,
        detail: bool = False,
    ) -> list[Metrics]:
        self.capability.check(window)
        scenario_key = BASE_SCENARIO if scenario is None else scenario.key
        keyed: list[tuple[str, dict[str, Any], str]] = []
        for point in points:
            canonical = dict(point)
            digest = hash_point(self.strategy_key, canonical)
            keyed.append((evaluation_key(self.context_digest, digest, window, scenario_key, detail), canonical, digest))
        results: dict[str, Metrics] = {}
        for key, point, digest in keyed:
            if key in results:
                continue
            self.cancel_check()
            reservation = run_sync(
                with_connection(
                    repo.reserve_evaluation,
                    self.study_id,
                    self.attempt,
                    repo.NewEvaluation(
                        evaluation_key=key,
                        point_hash=digest,
                        point=point,
                        window_start_ms=window[0],
                        window_end_ms=window[1],
                        scenario=scenario_key,
                        detail=detail,
                        stage=stage,
                        fold_index=fold_index,
                    ),
                    limit=self.budget_limit,
                    retry_allowance=RETRY_ALLOWANCE,
                )
            )
            if reservation.kind == "exhausted":
                raise BudgetExhausted(f"the study's evaluation budget cannot admit another {stage} evaluation")
            if reservation.kind == "cached":
                assert reservation.record is not None
                self.cache_hits += reservation.counted_as_cache_hit
                results[key] = repo.metrics_of(reservation.record)
                self.on_evaluated(self.new_runs + self.cache_hits)
                continue
            outcome = self._run(
                EvaluationRequest(
                    point=point,
                    point_hash=digest,
                    window=window,
                    warmup_from=self.warmup_for(window[0]),
                    scenario=scenario,
                    detail=detail,
                    stage=stage,
                    fold_index=fold_index,
                )
            )
            run_sync(
                with_connection(
                    repo.complete_evaluation, self.study_id, self.attempt, key, metrics=outcome.metrics, detail=outcome.detail
                )
            )
            self.new_runs += 1
            results[key] = outcome.metrics
            self.on_evaluated(self.new_runs + self.cache_hits)
        return [results[key] for key, _, _ in keyed]

    def _run(self, request: EvaluationRequest) -> EvaluationResult:
        try:
            return self.execute(request)
        except JobCancelled:
            raise
        except Exception as exc:
            # A failed run is evidence, recorded like any result; the job continues.
            logger.warning(
                "golden search evaluation failed",
                extra={"action": "golden_search_evaluation_failed", "study_id": self.study_id, "stage": request.stage},
                exc_info=True,
            )
            return EvaluationResult(metrics=Metrics.failed(_failure_message(exc)))

    def consume(self, count: int, *, step: str) -> None:
        """Atomically consume ``count`` units the caller runs outside this evaluator (the proof)."""
        admitted = run_sync(with_connection(repo.consume_budget, self.study_id, self.attempt, count, limit=self.budget_limit, step=step))
        if not admitted:
            raise BudgetExhausted(f"the study's evaluation budget cannot admit {count} more {step} evaluation(s)")
