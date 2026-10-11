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
    then recorded failed. A run the owner cancelled stays pending and is
    re-run without new budget or a retry.
  * daily curve — every completed run keeps ``(session close ms, equity)``
    for each session it scored: the engine's last equity sample of each ET
    date (``equity_day_ends``, filled on a summary run too), anchored by
    ``sweep.daily_equity`` in Grid Search's engine projection. It is the series a detail run's
    ``daily_equity`` holds. A run whose day ends cannot be anchored keeps
    its metrics and no curve.
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
from app.research.golden_search.protocol import BASE_SCENARIO, ExecutionAssumptions, StressScenario, canonical_json
from app.research.golden_search.selection import Metrics
from app.research.golden_search.zoom import BudgetExhausted, RetryAllowanceExhausted
from app.research.grid_search.service import window_dates
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.daily_equity import daily_equity
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


class EvidenceIntegrityError(RuntimeError):
    """A completed detail evaluation's recorded payload is absent or not the stored shape (#3039)."""


# The keys ``detail_payload`` writes; a completed detail row without them cannot feed a reading.
_DETAIL_REQUIRED_KEYS = ("initial_cash", "daily_equity", "trades")


def _is_number(value: object) -> bool:
    # JSON round-trips every number the payload writes as int or float; a
    # bool is an int subclass but never a recorded quantity.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def detail_integrity_problem(detail: object) -> str | None:
    """What disqualifies a recorded detail payload as evidence, or ``None`` when it holds the required shape."""
    if not isinstance(detail, Mapping):
        return "no recorded detail payload"
    missing = [name for name in _DETAIL_REQUIRED_KEYS if name not in detail]
    if missing:
        return f"recorded detail payload is missing {', '.join(missing)}"
    if not _is_number(detail["initial_cash"]):
        return "recorded initial_cash is not a number"
    daily = detail["daily_equity"]
    if not isinstance(daily, list) or any(
        not isinstance(point, list)
        or len(point) != 2
        or not isinstance(point[0], int)
        or isinstance(point[0], bool)
        or not _is_number(point[1])
        for point in daily
    ):
        return "recorded daily_equity is not a list of (session close ms, equity) points"
    trades = detail["trades"]
    if not isinstance(trades, list) or any(not isinstance(trade, Mapping) for trade in trades):
        return "recorded trades is not a list of trade mappings"
    return None


def require_intact_detail(key: str, status: str, detail: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The payload of a durable detail readback, or an integrity error (#3039).

    A completed evaluation read back from the store — a resume's cache hit or
    :meth:`StudyEvaluator.detail_of`'s read — must still hold the payload
    ``detail_payload`` wrote; anything absent or misshapen raises instead of
    resurfacing as a spurious missing-evidence reading. An in-flight engine
    outcome never passes through here: the engine producing a run without
    detail is the protocol's named-missing case, not a failure (#3012).
    """
    if status == "completed":
        problem = detail_integrity_problem(detail)
        if problem is not None:
            raise EvidenceIntegrityError(f"evaluation {key}: {problem}")
    return detail


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
    # ``(session close ms, equity)`` per session, for every completed run whose day ends could be anchored.
    curve: Sequence[tuple[int, float]] | None = None


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
    """Grid Search's statistics mapping and daily curve (``engine_adapter.cell_from_response``), plus the detail payload when asked."""
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
    return EvaluationResult(metrics=metrics, detail=detail, curve=cell.daily_equity)


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


# ── The ready-batch dispatch seam (#3040) ─────────────────────────────────


@dataclass(frozen=True)
class ReadyEvaluation:
    """One admitted evaluation a dispatcher receives: fully keyed and attempt-fenced.

    The key is the whole existing evaluation identity — context digest, point
    hash, window, scenario, detail — never the point hash alone; ``attempt``
    is the generation every reservation and completion is fenced by. A
    dispatcher owns only computation: admission, budget and persistence stay
    with the evaluator.
    """

    key: str
    attempt: int
    request: EvaluationRequest


@dataclass(frozen=True)
class DeliveredOutcome:
    """One dispatched evaluation's arrived outcome.

    ``cancelled_by`` carries the cancellation an execution observed — the
    evaluator releases that reservation and re-raises it, and nothing is
    persisted for the item (the synthetic ``result`` placeholder is never
    recorded). A batch member with no outcome at all never ran.
    """

    key: str
    result: EvaluationResult
    cancelled_by: Exception | None = None


BatchDispatch = Callable[
    [Callable[[EvaluationRequest], EvaluationResult], Sequence[ReadyEvaluation]],
    Sequence[DeliveredOutcome],
]
"""One ready batch's dispatch contract (#3040).

An adapter executes ``run`` for the admitted batch and returns one outcome
per admitted key — a cancellation is *delivered* as ``cancelled_by``, never
raised out of the adapter, so the evaluator can release every unrecorded
reservation. Arrival order is free; logical order is the evaluator's.
"""


def serial_dispatch(
    run: Callable[[EvaluationRequest], EvaluationResult], batch: Sequence[ReadyEvaluation]
) -> list[DeliveredOutcome]:
    """Today's executor, as the default adapter: one evaluation at a time, in logical order.

    Cancellation stops the walk at the item that observed it — exactly where
    the interleaved loop stopped before this seam existed (#3040).
    """
    outcomes: list[DeliveredOutcome] = []
    for item in batch:
        try:
            outcomes.append(DeliveredOutcome(key=item.key, result=run(item.request)))
        except JobCancelled as cancel:
            outcomes.append(DeliveredOutcome(key=item.key, result=EvaluationResult(metrics=Metrics.failed("cancelled")), cancelled_by=cancel))
            break
    return outcomes


def reordered_dispatch(order: Sequence[int]) -> BatchDispatch:
    """A deterministic adapter delivering outcomes in ``order`` — no processes, no timing.

    Execution still runs the whole batch in logical order; only delivery is
    permuted, so a test can prove the evaluator's logical consumption, winner
    selection and accounting never depend on arrival order (#3040).
    """

    def dispatch(run: Callable[[EvaluationRequest], EvaluationResult], batch: Sequence[ReadyEvaluation]) -> list[DeliveredOutcome]:
        outcomes = [DeliveredOutcome(key=item.key, result=run(item.request)) for item in batch]
        return [outcomes[index] for index in order]

    return dispatch


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
    # The ready-batch adapter (#3040). None — every production caller today —
    # runs the serial adapter; tests inject ``reordered_dispatch`` to prove
    # order independence. Production dispatch stays serial until the worker
    # runtime exists; the gate contract is untouched by this seam.
    dispatch: BatchDispatch | None = None
    new_runs: int = 0
    cache_hits: int = 0
    _warmups: dict[int, date] = field(default_factory=dict)
    # Each detail run's bounded evidence, by evaluation key, as recorded by
    # this instance or read back from the study's durable rows. An optional
    # cache: ``detail_of`` resolves completed evaluations without it (#3039).
    _details: dict[str, Mapping[str, Any] | None] = field(default_factory=dict)

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
        """Evaluate the caller's ready batch, in input order, under the attempt fence.

        Four separated phases (#3040): capability and key preparation, then
        atomic reservation in serial logical order — the affordable prefix
        only, stopping at the first unaffordable new identity — then
        dispatch of exactly that admitted prefix through the batch adapter
        (serial by default), then fenced persistence and logical consumption
        in input order as outcomes arrive. Cached and duplicate identities
        are served during reservation exactly as before and consume the same
        units. ``BudgetExhausted`` is raised after the admitted prefix is
        recorded, never instead of it.
        """
        self.capability.check(window)
        scenario_key = BASE_SCENARIO if scenario is None else scenario.key
        keyed: list[tuple[str, dict[str, Any], str]] = []
        for point in points:
            canonical = dict(point)
            digest = hash_point(self.strategy_key, canonical)
            keyed.append((evaluation_key(self.context_digest, digest, window, scenario_key, detail), canonical, digest))
        results: dict[str, Metrics] = {}
        admitted: list[ReadyEvaluation] = []
        handled: set[str] = set()
        exhausted = False
        for key, point, digest in keyed:
            if key in handled:
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
                # The first unaffordable new identity ends the prefix here:
                # later points are not searched for cheaper or cached work,
                # and nothing beyond this prefix is dispatched (#3040).
                exhausted = True
                break
            if reservation.kind == "cached":
                assert reservation.record is not None
                handled.add(key)
                self.cache_hits += reservation.counted_as_cache_hit
                results[key] = repo.metrics_of(reservation.record)
                if detail:
                    payload = reservation.record.detail_json
                    if reservation.record.attempt != self.attempt:
                        # A row an earlier attempt left is a durable readback —
                        # the resume's only authority, which must still hold
                        # its payload (#3039).
                        payload = require_intact_detail(key, reservation.record.status, payload)
                    self._details[key] = payload
                self.on_evaluated(self.new_runs + self.cache_hits)
                continue
            handled.add(key)
            admitted.append(
                ReadyEvaluation(
                    key=key,
                    attempt=self.attempt,
                    request=EvaluationRequest(
                        point=point,
                        point_hash=digest,
                        window=window,
                        warmup_from=self.warmup_for(window[0]),
                        scenario=scenario,
                        detail=detail,
                        stage=stage,
                        fold_index=fold_index,
                    ),
                )
            )
        if admitted:
            self._dispatch_and_record(admitted, results, detail=detail)
        if exhausted:
            raise BudgetExhausted(f"the study's evaluation budget cannot admit another {stage} evaluation")
        return [results[key] for key, _, _ in keyed]

    def _dispatch_and_record(
        self, admitted: Sequence[ReadyEvaluation], results: dict[str, Metrics], *, detail: bool
    ) -> None:
        """Run the admitted prefix through the adapter and persist outcomes as they arrive.

        Logical consumption is by full evaluation key, so arrival order never
        reaches the caller: results land in the caller's input order whatever
        the dispatcher delivered. A cancellation releases the reservation it
        stopped on and every never-ran member of the batch — the same keys
        the interleaved loop never reserved (#3040).
        """
        try:
            outcomes = (self.dispatch or serial_dispatch)(self._run, admitted)
        except JobCancelled:
            # The adapter contract says a cancellation is delivered, not
            # raised; this guard keeps an adapter bug from leaving the
            # batch's reservations pending with no cancellation marker.
            for item in admitted:
                run_sync(with_connection(repo.release_evaluation, self.study_id, self.attempt, item.key))
            raise
        recorded: set[str] = set()
        for outcome in outcomes:
            if outcome.cancelled_by is not None:
                never_ran = [item.key for item in admitted if item.key not in recorded and item.key != outcome.key]
                for key in (outcome.key, *never_ran):
                    run_sync(with_connection(repo.release_evaluation, self.study_id, self.attempt, key))
                raise outcome.cancelled_by
            run_sync(
                with_connection(
                    repo.complete_evaluation,
                    self.study_id,
                    self.attempt,
                    outcome.key,
                    metrics=outcome.result.metrics,
                    detail=outcome.result.detail,
                    curve=outcome.result.curve,
                )
            )
            recorded.add(outcome.key)
            self.new_runs += 1
            results[outcome.key] = outcome.result.metrics
            if detail:
                self._details[outcome.key] = outcome.result.detail
            self.on_evaluated(self.new_runs + self.cache_hits)

    def detail_of(self, point: Mapping[str, Any], *, window: Window) -> Mapping[str, Any] | None:
        """The detail payload of ``point``'s base-scenario detail run over ``window``; None without one.

        The local dictionary is an optional cache, not the authority (#3039):
        a completed evaluation this instance has not evaluated is read back
        from the study's durable rows by its exact evaluation key — context,
        point, window, base scenario, detail — without an engine call and
        without touching budget or retry allowance. A completed row whose
        payload is absent or not the stored shape raises
        :class:`EvidenceIntegrityError`; a row that never ran, or whose run
        failed, is legitimate missing evidence and stays ``None``.
        """
        digest = hash_point(self.strategy_key, dict(point))
        key = evaluation_key(self.context_digest, digest, window, BASE_SCENARIO, True)
        if key not in self._details:
            record = run_sync(with_connection(repo.get_evaluation, self.study_id, key))
            if record is None or record.status != "completed":
                return None
            self._details[key] = require_intact_detail(key, record.status, record.detail_json)
        return self._details[key]

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

    def consume(self, count: int, *, step: str, once_key: str) -> None:
        """Atomically consume ``count`` units the caller runs outside this evaluator (the proof), once per ``once_key``."""
        outcome = run_sync(
            with_connection(
                repo.consume_budget,
                self.study_id,
                self.attempt,
                count,
                limit=self.budget_limit,
                step=step,
                once_key=once_key,
                retry_allowance=RETRY_ALLOWANCE,
            )
        )
        if outcome == "over_budget":
            raise BudgetExhausted(f"the study's evaluation budget cannot admit {count} more {step} evaluation(s)")
        if outcome == "retries_spent":
            raise RetryAllowanceExhausted(f"the {once_key} step was already retried {RETRY_ALLOWANCE} times")
