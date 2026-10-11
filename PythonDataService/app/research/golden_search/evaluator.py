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
    attempt is re-run without new budget — its retry spent only when its
    run had begun, which the row's start marker decides (#3045) — at most
    ``RETRY_ALLOWANCE`` times, then recorded failed. A run the owner
    cancelled stays pending and is re-run without new budget or a retry.
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
  tests/research/golden_search/test_study_service.py,
  tests/research/golden_search/test_worker_loss_recovery.py.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
from app.research.persistence.ownership import GenerationFencedError
from app.research.sweep.daily_equity import daily_equity
from app.research.sweep.grid import RunSpec
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.utils.background_loop import CallerStoppedWaitingError
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


class WorkerLost(RuntimeError):
    """The execution backend died with work in flight (#3044, #3045).

    The affected evaluations' fates are unknown, not failed: their rows stay
    pending with their start markers, so a re-admission spends retry
    allowance only for a run that may have begun, and the dispatch may
    replace the worker and continue the same attempt. Distinct from a fenced
    generation (``app.research.persistence.ownership.GenerationFencedError``),
    which nothing may continue.
    """


class IdentityRefusalError(RuntimeError):
    """The child refused its sealed identity — the runtime is wrong, not lost (#3065 review).

    A deterministic mismatch between the coordinator's sealed code identity
    and the child's own must fail the stage closed: funnelled into
    :class:`WorkerLost` it would spend retry allowance per evaluation and end
    as fabricated ``retry allowance exhausted`` evidence for work that never
    ran. The evaluator catches nothing here — it propagates.
    """


class EvidenceIntegrityError(RuntimeError):
    """A completed detail evaluation's recorded payload is absent or not the stored shape (#3039)."""


# The keys ``detail_payload`` writes; a completed detail row without them cannot feed a reading.
_DETAIL_REQUIRED_KEYS = ("initial_cash", "daily_equity", "trades")


def _is_number(value: object) -> bool:
    # JSON round-trips every number the payload writes as int or float; a
    # bool is an int subclass but never a recorded quantity.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# The trade scalars the evidence math reads back (``trade_net``,
# ``concentration``): a mapping without these in the recorded shape is
# corrupted evidence, and the KeyError it would raise downstream must not
# surface as an ordinary failed reading (#3039).
_TRADE_INT_FIELDS = ("entry_ms", "exit_ms")
_TRADE_NUMBER_FIELDS = ("entry_price", "exit_price", "quantity", "pnl")


def _trade_integrity_problem(trade: Mapping[str, Any]) -> str | None:
    for name in _TRADE_INT_FIELDS:
        if not isinstance(trade.get(name), int) or isinstance(trade.get(name), bool):
            return f"a recorded trade's {name} is not an int"
    for name in _TRADE_NUMBER_FIELDS:
        if not _is_number(trade.get(name)):
            return f"a recorded trade's {name} is not a number"
    pnl_pct = trade.get("pnl_pct")
    if pnl_pct is not None and not _is_number(pnl_pct):
        return "a recorded trade's pnl_pct is neither a number nor null"
    exit_reason = trade.get("exit_reason")
    if exit_reason is not None and not isinstance(exit_reason, str):
        return "a recorded trade's exit_reason is neither a string nor null"
    return None


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
    if not isinstance(trades, list):
        return "recorded trades is not a list of trade mappings"
    for trade in trades:
        if not isinstance(trade, Mapping):
            return "recorded trades is not a list of trade mappings"
        trade_problem = _trade_integrity_problem(trade)
        if trade_problem is not None:
            return trade_problem
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
    """One admitted evaluation a dispatcher receives: fully keyed and generation-fenced.

    The key is the whole existing evaluation identity — context digest, point
    hash, window, scenario, detail — never the point hash alone; ``attempt``
    is the study generation and ``task_gen`` the dispatch generation inside
    it, and together they fence every start, completion and release (#3045).
    A dispatcher owns only computation: admission, budget and persistence stay
    with the evaluator — including ``begin``, the start marker it must call
    immediately before the item's engine work begins, so a crash between
    admission and execution re-runs free instead of spending a retry.
    """

    key: str
    attempt: int
    task_gen: int
    request: EvaluationRequest
    begin: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class DeliveredOutcome:
    """One dispatched evaluation's arrived outcome.

    ``cancelled_by`` carries the cancellation an execution observed — the
    evaluator releases that reservation and re-raises it, and nothing is
    persisted for the item (the synthetic ``result`` placeholder is never
    recorded). ``lost_reason`` carries one item's worker loss the same way
    (#3066 review): a pool delivers it instead of raising, so the evaluator
    re-admits only that item while healthy siblings keep delivering — the
    placeholder result is never recorded. A batch member with no outcome at
    all never ran. The attempt and task generation travel with the outcome so
    the evaluator can discard a late delivery in memory before the store's
    fences ever see it (#3045).
    """

    key: str
    attempt: int
    task_gen: int
    result: EvaluationResult
    cancelled_by: Exception | None = None
    lost_reason: str | None = None


BatchDispatch = Callable[
    [Callable[[EvaluationRequest], EvaluationResult], Sequence[ReadyEvaluation]],
    Iterable[DeliveredOutcome],
]
"""One ready batch's dispatch contract (#3040, #3045).

An adapter executes ``run`` for the admitted batch and delivers one outcome
per admitted key — a cancellation is *delivered* as ``cancelled_by``, never
raised out of the adapter, so the evaluator can release every unrecorded
reservation; a per-item worker loss may be delivered as ``lost_reason`` the
same way, leaving healthy siblings in flight (#3066 review). The evaluator
records each outcome before it asks for the next, so a production adapter
keeps at most its configured window of items begun-but-undelivered — one,
for the serial runtime (#3044); a bounded few, for a worker pool (#3046) —
and a crash mid-batch keeps every outcome already delivered. Arrival order
is free; logical order is the evaluator's. A :class:`WorkerLost` raised
mid-batch interrupts it for the evaluator's bounded re-admission — the
adapter must be callable again with a replaced worker. An adapter that owns
execution resources (a spawned child) exposes ``close()``, which the stage
calls on every exit, and ``cancel()``, which the evaluator calls before its
durable release reconciliation on a cancellation so the children never wait
out that reconciliation; plain callable adapters need neither. A documented
test adapter (``reordered_dispatch``) may trade the window away entirely to
permute delivery, keeping only the begin-before-engine-work half.
"""


def serial_dispatch(
    run: Callable[[EvaluationRequest], EvaluationResult], batch: Sequence[ReadyEvaluation]
) -> Iterator[DeliveredOutcome]:
    """Today's executor, as the default adapter: one evaluation at a time, in logical order.

    Cancellation stops the walk at the item that observed it — exactly where
    the interleaved loop stopped before this seam existed (#3040). Each item's
    start marker lands immediately before its engine call (#3045).
    """
    for item in batch:
        item.begin()
        try:
            outcome = DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen, result=run(item.request))
        except JobCancelled as cancel:
            yield DeliveredOutcome(
                key=item.key,
                attempt=item.attempt,
                task_gen=item.task_gen,
                result=EvaluationResult(metrics=Metrics.failed("cancelled")),
                cancelled_by=cancel,
            )
            return
        yield outcome


def reordered_dispatch(order: Sequence[int]) -> BatchDispatch:
    """A deterministic adapter delivering outcomes in ``order`` — no processes, no timing.

    Execution still runs the whole batch in logical order; only delivery is
    permuted, so a test can prove the evaluator's logical consumption, winner
    selection and accounting never depend on arrival order (#3040). A test
    adapter by design: it deliberately gives up lazy delivery — the whole
    batch computes before any outcome is consumed — keeping only the contract
    that matters for fencing, one ``begin`` immediately before each item's
    engine work.
    """

    def dispatch(run: Callable[[EvaluationRequest], EvaluationResult], batch: Sequence[ReadyEvaluation]) -> list[DeliveredOutcome]:
        outcomes = []
        for item in batch:
            item.begin()
            outcomes.append(
                DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen, result=run(item.request))
            )
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
        try:
            for key, point, digest in keyed:
                if key in handled:
                    continue
                self.cancel_check()
                reservation = self._reserve(key, point, digest, window=window, stage=stage, fold_index=fold_index, scenario_key=scenario_key, detail=detail)
                if reservation.kind == "exhausted":
                    # The first unaffordable new identity ends the prefix here:
                    # later points are not searched for cheaper or cached work,
                    # and nothing beyond this prefix is dispatched (#3040).
                    exhausted = True
                    break
                if reservation.kind == "cached":
                    handled.add(key)
                    self._serve_cached(key, reservation, results, detail=detail)
                    continue
                handled.add(key)
                admitted.append(self._ready(key, reservation.task_gen, point, digest, window=window, stage=stage, fold_index=fold_index, scenario=scenario, detail=detail))
        except (JobCancelled, GenerationFencedError):
            # An interruption before dispatch leaves the admitted prefix
            # pending with no start marker; a later reserve_evaluation would
            # read those never-run rows as crashed executions and burn their
            # retry allowance. The adapter's children — idle since the last
            # batch, but alive — stop before the release writes reconcile
            # (#3065 review). Release them: the same never-ran semantics the
            # start markers enforce during dispatch (#3040, #3045).
            self._stop_dispatch()
            for item in admitted:
                self._release(item.key, item.task_gen)
            raise
        except BaseException:
            for item in admitted:
                self._release(item.key, item.task_gen)
            raise
        if admitted:
            self._dispatch_and_record(admitted, results, detail=detail)
        if exhausted:
            raise BudgetExhausted(f"the study's evaluation budget cannot admit another {stage} evaluation")
        return [results[key] for key, _, _ in keyed]

    def _reserve(
        self,
        key: str,
        point: Mapping[str, Any],
        digest: str,
        *,
        window: Window,
        stage: str,
        fold_index: int | None,
        scenario_key: str,
        detail: bool,
    ) -> repo.Reservation:
        def call() -> repo.Reservation:
            return run_sync(
                with_connection(
                    repo.reserve_evaluation,
                    self.study_id,
                    self.attempt,
                    repo.NewEvaluation(
                        evaluation_key=key,
                        point_hash=digest,
                        point=dict(point),
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

        try:
            return call()
        except CallerStoppedWaitingError:
            # The reservation's outcome is unknown, not failed (#3045). The
            # fenced transaction is idempotent from the row's side: a first
            # attempt that committed reads back as this key's own row — no
            # second unit, a free re-admission — and one that rolled back
            # inserts now. A second stall is a dead database: propagate it.
            logger.warning(
                "golden search reservation write uncertain; reconciling by re-reserving",
                extra={"action": "golden_search_reserve_reconciled", "study_id": self.study_id, "evaluation_key": key},
            )
            return call()

    def _reserve_item(self, item: ReadyEvaluation) -> repo.Reservation:
        request = item.request
        return self._reserve(
            item.key,
            request.point,
            request.point_hash,
            window=request.window,
            stage=request.stage,
            fold_index=request.fold_index,
            scenario_key=BASE_SCENARIO if request.scenario is None else request.scenario.key,
            detail=request.detail,
        )

    def _begin(self, key: str, task_gen: int) -> Callable[[], None]:
        """The start-marker write an adapter triggers immediately before an item's engine work (#3045).

        An uncertain write reconciles by rewriting (#3065 review): the
        statement is idempotent under the fence, so a delayed commit lands as
        this key's own marker — no retry spent, nothing re-ran — and a
        rollback writes it now. A second stall is a dead database and
        propagates, the row still honestly un-started.
        """

        def write() -> None:
            run_sync(with_connection(repo.start_evaluation, self.study_id, self.attempt, key, task_gen=task_gen))

        def begin() -> None:
            try:
                write()
            except CallerStoppedWaitingError:
                logger.warning(
                    "golden search start-marker write uncertain; rewriting the idempotent marker",
                    extra={"action": "golden_search_start_reconciled", "study_id": self.study_id, "evaluation_key": key},
                )
                write()

        return begin

    def _ready(
        self,
        key: str,
        task_gen: int,
        point: Mapping[str, Any],
        digest: str,
        *,
        window: Window,
        stage: str,
        fold_index: int | None,
        scenario: StressScenario | None,
        detail: bool,
    ) -> ReadyEvaluation:
        return ReadyEvaluation(
            key=key,
            attempt=self.attempt,
            task_gen=task_gen,
            request=EvaluationRequest(
                point=dict(point),
                point_hash=digest,
                window=window,
                warmup_from=self.warmup_for(window[0]),
                scenario=scenario,
                detail=detail,
                stage=stage,
                fold_index=fold_index,
            ),
            begin=self._begin(key, task_gen),
        )

    def _rereadied(self, item: ReadyEvaluation, task_gen: int) -> ReadyEvaluation:
        """The same item under a new generation: the request and key stand, the fencing moves on (#3045)."""
        return ReadyEvaluation(
            key=item.key, attempt=item.attempt, task_gen=task_gen, request=item.request, begin=self._begin(item.key, task_gen)
        )

    def _serve_cached(
        self, key: str, reservation: repo.Reservation, results: dict[str, Metrics], *, detail: bool
    ) -> None:
        self._serve_record(key, reservation.record, results, detail=detail, counted_as_cache_hit=reservation.counted_as_cache_hit)

    def _serve_record(
        self,
        key: str,
        record: Any,
        results: dict[str, Metrics],
        *,
        detail: bool,
        counted_as_cache_hit: int,
        count_as_run: bool = False,
    ) -> None:
        """Serve a row the store already holds — its recorded result, and the accounting the caller names (#3045).

        A reservation answers this way for a cache hit; a reconciled
        uncertain write answers this way for the delayed commit of this
        very run's outcome (``count_as_run`` — one completion happened, even
        though it is nobody's new reservation).
        """
        assert record is not None
        self.cache_hits += counted_as_cache_hit
        if count_as_run:
            self.new_runs += 1
        results[key] = repo.metrics_of(record)
        if detail:
            payload = record.detail_json
            if record.attempt != self.attempt:
                # A row an earlier attempt left is a durable readback —
                # the resume's only authority, which must still hold
                # its payload (#3039).
                payload = require_intact_detail(key, record.status, payload)
            self._details[key] = payload
        self.on_evaluated(self.new_runs + self.cache_hits)

    def _stop_dispatch(self) -> None:
        """Reap the adapter's execution resources before durable reconciliation (#3065 review).

        A cancellation raised in this frame — delivered as an outcome, or
        raised by a poll outside the adapter — never runs the adapter's own
        handlers, so its children would otherwise stay alive through the
        release writes below. An adapter without a ``cancel`` owns nothing
        to stop.
        """
        stop = getattr(self.dispatch, "cancel", None)
        if stop is not None:
            stop()

    def _release(self, key: str, task_gen: int) -> None:
        """Leave a cancelled run's row pending, marked so the next attempt re-runs it without spending a retry."""
        try:
            run_sync(with_connection(repo.release_evaluation, self.study_id, self.attempt, key, task_gen=task_gen))
        except CallerStoppedWaitingError:
            # The release write's outcome is unknown; cancellation must not
            # wait on it (#3045). Its late landing is fenced by the task
            # generation — it can only mark a row still pending under
            # exactly this generation — and the row staying pending is an
            # honest outcome for Finish.
            logger.warning(
                "golden search release write uncertain; leaving the reservation pending",
                extra={"action": "golden_search_release_uncertain", "study_id": self.study_id, "evaluation_key": key},
            )

    def _dispatch_and_record(
        self, admitted: Sequence[ReadyEvaluation], results: dict[str, Metrics], *, detail: bool
    ) -> None:
        """Run the admitted prefix through the adapter, recording each outcome as it arrives.

        Each outcome is durable before the evaluator asks for the next; an
        adapter keeps at most its configured window begun-but-undelivered
        (#3045 made the window one, #3046 lets a bounded pool widen it), so a
        crash mid-batch keeps every delivered outcome and loses at most the
        window. A worker lost with work in flight re-admits exactly the lost
        item — delivered as ``lost_reason`` by a pool, whose healthy siblings
        keep delivering, or raised whole by a serial adapter, whose single
        in-flight item is the only thing outstanding — under an advanced task
        generation (the started one spends a retry, the never-started re-run
        free), the adapter replaces its worker, and the batch continues;
        replacements without durable progress stop after the retry
        allowance, and each item's runs stay bounded by it. Logical
        consumption is by full evaluation key, so arrival order never
        reaches the caller.
        """
        outstanding: dict[str, ReadyEvaluation] = {item.key: item for item in admitted}
        replacements = 0
        while outstanding:
            progressed = False
            exhausted: str | None = None
            # The objects this round entered with: a loss marker resolves its
            # key by REPLACING the item (a fresh generation), so a round that
            # ended with only replacements outstanding re-dispatches rather
            # than breaching — the guard fires on untouched items alone.
            entered = dict(outstanding)
            try:
                outcomes = (self.dispatch or serial_dispatch)(self._run, list(outstanding.values()))
                try:
                    for outcome in outcomes:
                        item = outstanding.get(outcome.key)
                        if item is None or outcome.attempt != self.attempt or outcome.task_gen != item.task_gen:
                            # Late output from a cancelled, timed-out or replaced dispatch:
                            # the store's attempt and task-generation fences would refuse
                            # it; discard it here, explicitly, and let the owed outcome land (#3045).
                            logger.warning(
                                "golden search discarded a stale delivered outcome",
                                extra={
                                    "action": "golden_search_stale_outcome_discarded",
                                    "study_id": self.study_id,
                                    "evaluation_key": outcome.key,
                                    "outcome_attempt": outcome.attempt,
                                    "outcome_task_gen": outcome.task_gen,
                                },
                            )
                            continue
                        if outcome.lost_reason is not None:
                            # One item's worker was lost (#3066 review): re-admit only
                            # it, never the siblings still delivering elsewhere. The
                            # re-admitted item rejoins through the next dispatch round.
                            reservation = self._reserve_item(item)
                            if reservation.kind == "cached":
                                self._serve_cached(outcome.key, reservation, results, detail=detail)
                                del outstanding[outcome.key]
                                replacements = 0
                            else:
                                outstanding[outcome.key] = self._rereadied(item, reservation.task_gen)
                                replacements += 1
                                if replacements > RETRY_ALLOWANCE:
                                    # Consecutive replacements bought no durable progress:
                                    # stop trusting the runtime and leave the honest pending
                                    # rows for Finish. The per-item bound is the retry
                                    # allowance, spent by reserve_evaluation as each
                                    # started run re-admits.
                                    exhausted = outcome.lost_reason
                                    break
                            continue
                        if outcome.cancelled_by is not None:
                            raise outcome.cancelled_by
                        self._persist(outcome, results, detail=detail)
                        del outstanding[outcome.key]
                        replacements = 0
                        progressed = True
                    if outstanding and exhausted is None and any(outstanding.get(key) is item for key, item in entered.items()):
                        raise RuntimeError("the dispatch adapter ended without delivering an outcome for every admitted key")
                finally:
                    # Close the round's adapter deterministically, not by garbage
                    # collection: an adapter holding execution resources frees them
                    # (a pool's window permits among them) the moment the round is
                    # abandoned, never at the interpreter's pleasure (#3066 review).
                    close = getattr(outcomes, "close", None)
                    if close is not None:
                        close()
            except JobCancelled:
                # A cancellation — delivered, or raised by an adapter that breaks
                # the deliver-never-raise contract — stops the children first
                # (#3065 review: the slot must not wait out the reconciliation),
                # then releases every unrecorded reservation, exactly the keys
                # the interleaved loop never reserved (#3040).
                self._stop_dispatch()
                for key, pending_item in outstanding.items():
                    self._release(key, pending_item.task_gen)
                raise
            except WorkerLost:
                outstanding, resolved = self._readmit(outstanding, results, detail=detail)
                # Durable progress — a completion this round, or a readmit that
                # resolved an item — resets the counter; only replacements that
                # resolve nothing count toward giving up (#3045).
                replacements = 0 if (progressed or resolved) else replacements + 1
                if replacements > RETRY_ALLOWANCE:
                    raise
            if exhausted is not None:
                raise WorkerLost(exhausted)

    def _readmit(
        self, outstanding: Mapping[str, ReadyEvaluation], results: dict[str, Metrics], *, detail: bool
    ) -> tuple[dict[str, ReadyEvaluation], bool]:
        """Re-reserve every unfinished item after a worker loss (#3045).

        A re-admission never draws a second budget unit — the row exists.
        One whose run had begun spends a retry, bounded by the allowance; one
        that never started, or whose allowance is spent, resolves here as a
        cached (possibly failed) record instead of a re-run. Returns the
        still-unfinished items and whether anything resolved durably.
        """
        readmitted: dict[str, ReadyEvaluation] = {}
        resolved = False
        for key, item in outstanding.items():
            reservation = self._reserve_item(item)
            if reservation.kind == "cached":
                self._serve_cached(key, reservation, results, detail=detail)
                resolved = True
            else:
                readmitted[key] = self._rereadied(item, reservation.task_gen)
        return readmitted, resolved

    def _persist(self, outcome: DeliveredOutcome, results: dict[str, Metrics], *, detail: bool) -> None:
        """Record one outcome exactly once under its task generation (#3045).

        An uncertain write — the caller stopped waiting, the operation may
        still commit — is reconciled through a fenced generation swap before
        anything is retried: the row lock decides against the delayed write,
        so either the row resolved (it committed; its recorded result stands)
        or the same in-hand result persists under the new generation, fencing
        the delayed write out. No retry is spent and the engine never re-runs.
        """
        try:
            run_sync(
                with_connection(
                    repo.complete_evaluation,
                    self.study_id,
                    self.attempt,
                    outcome.key,
                    metrics=outcome.result.metrics,
                    detail=outcome.result.detail,
                    curve=outcome.result.curve,
                    task_gen=outcome.task_gen,
                )
            )
        except CallerStoppedWaitingError:
            # The write is unknown, not failed: it may still commit under
            # this generation, and a pending read proves nothing while it is
            # in flight. Swap the generation under the fence — the row lock
            # decides against the delayed write — then either the row
            # resolved (it committed; done, nothing re-runs) or the same
            # result persists under the new generation, which fences the
            # delayed write out. No retry is spent and the engine never
            # re-runs (#3045).
            logger.warning(
                "golden search completion write uncertain; reconciling the durable row",
                extra={"action": "golden_search_completion_reconciled", "study_id": self.study_id, "evaluation_key": outcome.key},
            )
            claim = run_sync(
                with_connection(repo.replace_generation, self.study_id, self.attempt, outcome.key, task_gen=outcome.task_gen)
            )
            if claim.resolved is not None:
                self._serve_record(outcome.key, claim.resolved, results, detail=detail, counted_as_cache_hit=0, count_as_run=True)
                return None
            # The delayed write is fenced out by the new generation; the same
            # result — no engine re-run, no retry — lands under it. A second
            # uncertain write propagates: the row stays honestly pending, its
            # start marker cleared, for Finish to re-run free.
            run_sync(
                with_connection(
                    repo.complete_evaluation,
                    self.study_id,
                    self.attempt,
                    outcome.key,
                    metrics=outcome.result.metrics,
                    detail=outcome.result.detail,
                    curve=outcome.result.curve,
                    task_gen=claim.task_gen,
                )
            )
        results[outcome.key] = outcome.result.metrics
        self._count_persisted(outcome, detail=detail)
        return None

    def _count_persisted(self, outcome: DeliveredOutcome, *, detail: bool) -> None:
        self.new_runs += 1
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
        # Reservation checks cancellation once per batch up front, so an
        # adapter that runs items back-to-back would not observe a
        # cancellation delivered mid-batch; checking here gives every
        # adapter a pre-execution poll, delivered through the seam as a
        # cancelled outcome (#3040).
        self.cancel_check()
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
