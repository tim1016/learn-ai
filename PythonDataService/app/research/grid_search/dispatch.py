"""Grid Search cells through the shared research worker runtime (#3049; PRD #3034 slice 14).

A Grid Search run keeps its own scientific contract whole: cells stay keyed
by ``params_hash`` (never a Golden Search evaluation key), persistence,
failed-cell handling, ranking and terminal status stay in the coordinator
under the existing attempt fence (ADR 0055 §4), and Finish reuse stays the
durable ``existing_params_hashes`` skip set. What moves into the runtime is
only the computation: one cell is one task for a spawned research worker —
the same child, wire, admission batcher, decoded-cache budget and shared
pool Golden Search owns (#3044-#3048) — so independent cells overlap on the
pool while the search's logical order, progress counts and ranking never
see the pool.

The wire is grid's own task kind: the receipt's ms interval table, the
frozen execution contract, the data manifest and the sealed code identity
ride every task (#3049); the child rebuilds the engine request through
``engine_request_over`` — the exact constructor the in-process adapter uses
— and projects the response with ``cell_from_response``, so a dispatched
cell and a locally executed one are the same numbers. Temporal values cross
as ``int64 ms UTC`` (ADR 0022), never ISO strings.

Worker loss is not a cell verdict: a lost cell is re-offered under an
advanced task generation, bounded by the evaluator's retry allowance, and a
loss past the bound fails the search honestly instead of recording a cell
that never ran — a durable spurious failed cell could never be re-run by
Finish. Arrival order is free; persistence order is the runner's.
Reference: #3049 / PRD #3034; ``app.research.golden_search.worker`` for the
protocol; ``app.research.golden_search.pool`` for the pooled precedent.
Canonical implementation: this file, beside the adapters it mirrors.
Validated against: tests/research/grid_search/test_dispatch.py.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.config import settings
from app.jobs.progress import JobCancelled
from app.research.golden_search.evaluator import RETRY_ALLOWANCE, IdentityRefusalError, WorkerLost
from app.research.golden_search.shared_pool import (
    DEFAULT_PAYLOAD_BOUND_BYTES,
    DEFAULT_RESULT_BOUND_BYTES,
    SUBMISSION_FACTOR,
    OwnerHandle,
    SharedWorkerPool,
    shared_worker_pool,
)
from app.research.golden_search.worker import (
    ResearchWorker,
    WorkerDispatchContext,
    pooled_cache_bytes,
)
from app.research.grid_search.engine_adapter import cell_from_response, engine_request_over
from app.research.grid_search.models import CellResult, GridSearchSpec, SearchRow
from app.research.persistence.ownership import GenerationFencedError
from app.research.sweep.grid import RunSpec

logger = logging.getLogger(__name__)

#: The task-kind marker a grid cell carries on the worker wire (#3049). The
#: golden evaluation wire stays the unmarked default, so existing children
#: and fixtures need no migration.
GRID_WIRE_KIND = "grid_search"


# ── The ready item and its outcome ────────────────────────────────────────


@dataclass(frozen=True)
class ReadyCell:
    """One admitted cell a dispatcher receives: keyed by ``params_hash``, generation-fenced.

    Grid Search keeps no reservation rows — a cell is durable only once
    completed — so ``begin`` is the contract's no-op here and ``task_gen``
    fences only re-offers after a worker loss, where golden's fences every
    start marker too (#3045). The interval and execution blocks are the
    receipt's frozen parts; the child rebuilds the engine request from them.
    """

    key: str
    attempt: int
    task_gen: int
    candidate: RunSpec
    interval: Mapping[str, int]
    execution: Mapping[str, Any]
    manifest: Mapping[str, str]
    identity: Mapping[str, Any]
    begin: Callable[[], None] = lambda: None

    def as_wire(self) -> dict[str, Any]:
        return {
            "kind": GRID_WIRE_KIND,
            "key": self.key,
            "attempt": self.attempt,
            "task_gen": self.task_gen,
            "candidate": {
                "symbol": self.candidate.symbol,
                "strategy_key": self.candidate.strategy_key,
                "params": dict(self.candidate.params),
                "params_hash": self.candidate.params_hash,
            },
            "interval": dict(self.interval),
            "execution": dict(self.execution),
            "manifest": dict(self.manifest),
            "identity": {
                "source_digest": str(self.identity["source_digest"]),
                "environment_digest": str(self.identity["environment_digest"]),
                "digest_scheme": self.identity["digest_scheme"],
            },
        }

    def outcome_of(self, message: Mapping[str, Any]) -> CellOutcome:
        return cell_outcome(self, message)


@dataclass(frozen=True)
class CellOutcome:
    """One dispatched cell's arrived outcome — the grid twin of golden's ``DeliveredOutcome``.

    A cancellation is *delivered* (``cancelled_by``), never raised out of a
    child, so the dispatch stops its lanes and re-raises it on the
    coordinator's terms; a failed run is a failed cell exactly as the
    in-process adapter records one; the attempt and task generation travel
    with the outcome so a straggler from a superseded re-offer is discarded
    in memory before the store's fences ever see it.
    """

    key: str
    attempt: int
    task_gen: int
    cell: CellResult | None
    cancelled_by: Exception | None = None
    lost_reason: str | None = None


def cell_outcome(item: ReadyCell, message: Mapping[str, Any]) -> CellOutcome:
    """One child answer as the runner consumes it — the same translation both adapters make."""
    if message["type"] == "failed" and message.get("error") == "cancelled":
        return CellOutcome(
            key=item.key,
            attempt=item.attempt,
            task_gen=item.task_gen,
            cell=None,
            cancelled_by=JobCancelled("cancelled in a research worker"),
        )
    if message["type"] == "failed":
        return CellOutcome(
            key=item.key,
            attempt=item.attempt,
            task_gen=item.task_gen,
            cell=CellResult(
                params_hash=item.key,
                params=dict(item.candidate.params),
                status="failed",
                error=str(message.get("error", "worker failure")),
            ),
        )
    cell = dict(message["cell"])
    equity = cell.get("daily_equity")
    return CellOutcome(
        key=item.key,
        attempt=item.attempt,
        task_gen=item.task_gen,
        cell=CellResult(
            params_hash=cell["params_hash"],
            params=dict(cell["params"]),
            status=cell["status"],
            total_trades=int(cell["total_trades"]),
            net_profit=cell["net_profit"],
            total_return_pct=cell["total_return_pct"],
            sharpe_ratio=cell["sharpe_ratio"],
            max_drawdown_pct=cell["max_drawdown_pct"],
            win_rate=cell["win_rate"],
            bars_consumed=cell["bars_consumed"],
            error=cell["error"],
            daily_equity=[(int(ms), float(value)) for ms, value in equity] if equity else None,
        ),
    )


def cell_to_wire(cell: CellResult) -> dict[str, Any]:
    """A projected cell as plain JSON data for the result message; the curve crosses as ``[ms, value]`` pairs."""
    return {
        "params_hash": cell.params_hash,
        "params": dict(cell.params),
        "status": cell.status,
        "total_trades": cell.total_trades,
        "net_profit": cell.net_profit,
        "total_return_pct": cell.total_return_pct,
        "sharpe_ratio": cell.sharpe_ratio,
        "max_drawdown_pct": cell.max_drawdown_pct,
        "win_rate": cell.win_rate,
        "bars_consumed": cell.bars_consumed,
        "error": cell.error,
        "daily_equity": [[ms, value] for ms, value in cell.daily_equity] if cell.daily_equity else None,
    }


#: What ``run_grid`` consumes from either adapter: each arrival as its
#: ``(candidate, result)`` pair, in arrival order — the runner persists and
#: counts as it consumes, so arrival order is free and logical order is the
#: ranking's.
CellDispatch = Callable[[Iterable[RunSpec]], Iterator[tuple[RunSpec, CellResult]]]


def ready_cell_builder(row: SearchRow, spec: GridSearchSpec, attempt: int) -> Callable[[RunSpec], ReadyCell]:
    """The per-candidate item builder: the receipt's frozen parts, one cell's identity."""

    def build(candidate: RunSpec) -> ReadyCell:
        return ReadyCell(
            key=candidate.params_hash,
            attempt=attempt,
            task_gen=1,
            candidate=candidate,
            interval={
                "data_start_ms": int(row.receipt["interval_table"]["data_start_ms"]),
                "evaluation_start_ms": int(row.receipt["interval_table"]["evaluation_start_ms"]),
                "evaluation_end_ms": int(row.receipt["interval_table"]["evaluation_end_ms"]),
            },
            execution={
                "strategy_key": spec.strategy_key,
                "symbol": spec.symbol,
                "fill_mode": spec.fill_mode,
                "commission_per_order": spec.commission_per_order,
                "slippage_per_share": spec.slippage_per_share,
                "initial_cash": spec.initial_cash,
                "resolution": spec.resolution,
            },
            manifest=row.receipt["data_snapshot"]["artifacts"],
            identity=row.receipt["code_identity"],
        )

    return build


# ── The child's half of one task (#3049) ──────────────────────────────────


def run_grid_cell_task(
    task: Mapping[str, Any],
    *,
    out: Any,
    ask_admission: Callable[[Any], bytes],
    cancel_flag: threading.Event,
    state: dict[str, Any],
) -> None:
    """Execute one grid cell in the child: identity-check, engine call, grid projection.

    Mirrors the golden task runner's sinks exactly — preparation checkpoint,
    admission-routed reads, cancellation through the raise-only poll, the
    durable-completions checkpoint, residency hints on every answer — with
    the request built and the response projected by grid's own adapter, so
    the numbers are the in-process adapter's numbers.
    """
    from app.research.golden_search.worker import _expected_identity, _residency_fields, _task_checkpoint
    from app.services.engine_backtest_service import execute_engine_backtest

    candidate = RunSpec(
        symbol=str(task["candidate"]["symbol"]),
        strategy_key=str(task["candidate"]["strategy_key"]),
        params={str(name): float(value) for name, value in task["candidate"]["params"].items()},
        params_hash=str(task["candidate"]["params_hash"]),
    )

    def raise_if_cancelled() -> None:
        if cancel_flag.is_set():
            raise JobCancelled("cancelled by the coordinator")

    try:
        if state["identity"] != _expected_identity(task):
            out.write(
                {"type": "identity_refused", "key": task["key"], "reason": "sealed identity differs from the task's"}
            )
            return
        checkpoint = _task_checkpoint(out, state)
        checkpoint("preparation")
        response = execute_engine_backtest(
            request=engine_request_over(candidate, interval=task["interval"], execution=task["execution"]),
            on_phase=lambda phase: checkpoint(phase),
            on_log=lambda message: None,
            data_manifest=dict(task["manifest"]),
            while_waiting=raise_if_cancelled,
            decoded_cache=state["decoded_cache"],
            admission=ask_admission,
        )
        cell = cell_from_response(candidate, response)
        state["completed"] = int(state.get("completed", 0)) + 1
        checkpoint("search", cursor_kind="durable_completions", cursor=state["completed"])
        out.write({"type": "result", "key": task["key"], "cell": cell_to_wire(cell), **_residency_fields(state)})
    except BaseException as exc:  # the child's terminal sink: every failure is a delivered answer
        if isinstance(exc, JobCancelled):
            out.write(
                {"type": "failed", "key": str(task.get("key", "?")), "error": "cancelled", **_residency_fields(state)}
            )
            cancel_flag.set()
            return
        out.write(
            {
                "type": "failed",
                "key": str(task.get("key", "?")),
                "error": f"{type(exc).__name__}: {exc}",
                **_residency_fields(state),
            }
        )


# ── The serial adapter: one owned child, one task lane ────────────────────


class WorkerCellDispatch:
    """The one-worker cell dispatch: grid's twin of the golden serial adapter (#3044, #3049).

    Serial in effect — one child, one task lane, the runner's logical order
    preserved — with the coordinator never executing and never entering the
    engine gate. A lost child is killed and reaped before any replacement
    exists, the lost cell is re-offered under an advanced task generation
    within the retry allowance, and a loss past the bound raises instead of
    recording a cell nobody ran.
    """

    def __init__(self, context: WorkerDispatchContext, ready: Callable[[RunSpec], ReadyCell]) -> None:
        self._context = context
        self._ready = ready
        self._worker: ResearchWorker | None = None

    def _live_worker(self) -> ResearchWorker:
        worker = self._worker
        if worker is not None and worker.lost_reason() is not None:
            self._worker_lost(worker)
        if self._worker is None:
            self._worker = ResearchWorker(self._context)
        return self._worker

    def _worker_lost(self, worker: ResearchWorker) -> None:
        worker.close()
        if self._worker is worker:
            self._worker = None

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def cancel(self) -> None:
        self._cancel()

    def __call__(self, cells: Iterable[RunSpec]) -> Iterator[tuple[RunSpec, CellResult]]:
        for candidate in cells:
            try:
                cancelled = self._context.cancel_check()
            except (JobCancelled, GenerationFencedError):
                self._cancel()
                raise
            if cancelled:
                self._cancel()
                raise JobCancelled("cancelled between research worker cell tasks")
            item = self._ready(candidate)
            losses = 0
            while True:
                worker = self._live_worker()
                item.begin()
                try:
                    message = worker.run_task(item.as_wire())
                except WorkerLost as exc:
                    self._worker_lost(worker)
                    losses += 1
                    if losses > RETRY_ALLOWANCE:
                        raise WorkerLost(
                            f"grid cell {item.key[:8]} lost its worker {losses} times; "
                            "the search stops rather than record a cell that never ran"
                        ) from exc
                    item = replace(item, task_gen=item.task_gen + 1)
                    logger.warning(
                        "a research worker was lost with grid cell %s in flight; re-offering under generation %s (%s)",
                        item.key[:8],
                        item.task_gen,
                        exc,
                        extra={"action": "grid_search_cell_reoffered", "params_hash": item.key},
                    )
                    continue
                except (JobCancelled, GenerationFencedError, IdentityRefusalError):
                    self._worker_lost(worker)
                    raise
                break
            outcome = item.outcome_of(message)
            if outcome.cancelled_by is not None:
                self._cancel()
                raise outcome.cancelled_by
            assert outcome.cell is not None  # only a cancellation delivers without one
            yield candidate, outcome.cell

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.close()


# ── The pooled adapter: one owner on the shared pool ──────────────────────


class PooledCellDispatch:
    """The cell dispatch as one owner on a worker pool (#3049), grid's twin of ``PooledWorkerDispatch``.

    Offers cells up to its window — the permit bound that keeps a whole
    search's candidates from becoming resident execution — and yields each
    outcome as it arrives; the runner persists each cell before pulling the
    next. A lost lane is that cell's loss: it is re-offered under an
    advanced task generation within the retry allowance while healthy
    siblings keep delivering, and a loss past the bound stops the search
    honestly. A straggler from a superseded re-offer is discarded against
    the task generation, never persisted.
    """

    def __init__(
        self,
        context: WorkerDispatchContext,
        ready: Callable[[RunSpec], ReadyCell],
        *,
        workers: int,
        window: int,
        pool: SharedWorkerPool | None = None,
        on_facts: Callable[[Mapping[str, Any]], None] = lambda facts: None,
        on_capacity_handoff: Callable[[], None] = lambda: None,
    ) -> None:
        if workers < 1:
            raise ValueError("a worker pool has at least one lane")
        self._context = context
        self._ready = ready
        self._worker_count = workers
        self._window = max(window, workers)
        self._on_facts = on_facts
        context.decoded_cache_bytes = pooled_cache_bytes(workers)
        self._owns_pool = pool is None
        self._pool = pool or SharedWorkerPool(
            workers=workers,
            submission_bound=max(SUBMISSION_FACTOR * workers, self._window),
            payload_bound_bytes=DEFAULT_PAYLOAD_BOUND_BYTES,
            result_bound_bytes=DEFAULT_RESULT_BOUND_BYTES,
        )
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        self._outstanding = 0
        self._handle: OwnerHandle | None = self._pool.register(
            context,
            window=self._window,
            epoch_of=self._current_epoch,
            on_capacity_handoff=on_capacity_handoff,
        )

    def _current_epoch(self) -> int:
        with self._epoch_lock:
            return self._epoch

    def __call__(self, cells: Iterable[RunSpec]) -> Iterator[tuple[RunSpec, CellResult]]:
        owed: dict[str, ReadyCell] = {}
        candidates: dict[str, RunSpec] = {}
        for candidate in cells:
            item = self._ready(candidate)
            owed[item.key] = item
            candidates[item.key] = candidate
        with self._epoch_lock:
            self._epoch += 1
            epoch = self._epoch
        submitted: set[str] = set()
        retries: dict[str, int] = {}
        self._outstanding = 0
        adaptive_wait_ms = 0
        self._emit_facts(len(owed), adaptive_wait_ms)
        try:
            self._feed(owed, submitted, epoch)
            while owed:
                try:
                    cancelled = self._context.cancel_check()
                except (JobCancelled, GenerationFencedError):
                    self._interrupt(epoch)
                    raise
                if cancelled:
                    self._interrupt(epoch)
                    raise JobCancelled("cancelled between pooled grid cell tasks")
                waited_at = time.monotonic()
                try:
                    key, task_gen, arrival = self._handle.arrived.get(timeout=0.2)
                except queue.Empty:
                    adaptive_wait_ms += int((time.monotonic() - waited_at) * 1000)
                    self._feed(owed, submitted, epoch)
                    continue
                item = owed.get(key)
                if item is None or item.task_gen != task_gen:
                    logger.warning(
                        "a pooled worker delivered an outcome the current grid search no longer owes",
                        extra={"action": "grid_search_pool_straggler_discarded", "params_hash": key},
                    )
                    self._handle.consume(key, task_gen)
                    continue
                if isinstance(arrival, WorkerLost):
                    self._handle.consume(key, task_gen)
                    self._outstanding -= 1
                    retries[key] = retries.get(key, 0) + 1
                    if retries[key] > RETRY_ALLOWANCE:
                        raise WorkerLost(
                            f"grid cell {key[:8]} lost its worker {retries[key]} times; "
                            "the search stops rather than record a cell that never ran"
                        )
                    owed[key] = replace(item, task_gen=item.task_gen + 1)
                    submitted.discard(key)
                    logger.warning(
                        "a pooled research worker was lost with grid cell %s in flight; re-offering under generation %s",
                        key[:8],
                        owed[key].task_gen,
                        extra={"action": "grid_search_cell_reoffered", "params_hash": key},
                    )
                    self._feed(owed, submitted, epoch)
                    continue
                if isinstance(arrival, BaseException):
                    self._handle.consume(key, task_gen)
                    if isinstance(arrival, (JobCancelled, GenerationFencedError)):
                        self._interrupt(epoch)
                    else:
                        with self._epoch_lock:
                            if self._epoch == epoch:
                                self._epoch += 1
                    raise arrival
                self._handle.consume(key, task_gen)
                self._outstanding -= 1
                del owed[key]
                if arrival.cancelled_by is not None:
                    self._interrupt(epoch)
                    raise arrival.cancelled_by
                assert arrival.cell is not None  # only a cancellation delivers without one
                yield candidates.pop(key), arrival.cell
                # The runner resumed — the cell it took is durable — so the
                # window opens by one for the next submission.
                self._feed(owed, submitted, epoch)
        finally:
            self._emit_facts(len(owed), adaptive_wait_ms)

    def _feed(self, owed: Mapping[str, ReadyCell], submitted: set[str], epoch: int) -> None:
        handle = self._handle
        if handle is None:
            return
        for key, item in owed.items():
            if key in submitted:
                continue
            if self._outstanding >= self._window:
                return
            if not handle.offer(epoch, item):
                return
            submitted.add(key)
            self._outstanding += 1

    def _interrupt(self, epoch: int) -> None:
        with self._epoch_lock:
            if self._epoch == epoch:
                self._epoch += 1
        if self._handle is not None:
            self._handle.interrupt_running()

    def cancel(self) -> None:
        with self._epoch_lock:
            self._epoch += 1
        if self._handle is not None:
            self._handle.interrupt_running()

    def _emit_facts(self, ready_width: int, adaptive_wait_ms: int) -> None:
        self._on_facts(
            {**self._pool.facts(), "ready_width": ready_width, "window": self._window, "adaptive_wait_ms": adaptive_wait_ms}
        )

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._owns_pool:
            self._pool.close()


# ── What the runtime builds (#3049) ───────────────────────────────────────


def runtime_cell_dispatch(
    row: SearchRow,
    spec: GridSearchSpec,
    *,
    attempt: int,
    job_id: str | None,
    on_checkpoint: Callable[[Mapping[str, Any]], None],
    cancel_check: Callable[[], object],
    on_log: Callable[[str], None] = lambda message: None,
    pool: SharedWorkerPool | None = None,
) -> WorkerCellDispatch | PooledCellDispatch:
    """The dispatch the research runtime executes grid cells through (#3049).

    ``RESEARCH_WORKER_COUNT`` is the deployment's opt-in width — one is a
    lone spawned child, exactly the serial runtime's shape (#3044) — and
    every wider run is one owner on the process's shared pool, where
    round-robin fairness, the global submission bound and warm-lane affinity
    live (#3048). A capacity handoff renews the lease exactly as a checkpoint
    does. The pool's scheduling facts ride the existing log channel.
    """
    ready = ready_cell_builder(row, spec, attempt)
    context = WorkerDispatchContext(
        strategy_key=spec.strategy_key,
        # Grid cells carry their own frozen execution block per task wire;
        # the context's execution field is the golden wire's, unused here.
        execution={},
        manifest=row.receipt["data_snapshot"]["artifacts"],
        identity=row.receipt["code_identity"],
        job_id=job_id or "",
        generation=attempt,
    )
    context.on_checkpoint = on_checkpoint
    context.cancel_check = cancel_check
    if settings.RESEARCH_WORKER_COUNT < 2:
        return WorkerCellDispatch(context, ready)

    def on_facts(facts: Mapping[str, Any]) -> None:
        on_log(f"research worker pool: {dict(facts)}")

    def on_capacity_handoff() -> None:
        # A handoff is real advancement (#3048), renewed through the same
        # gated checkpoint relay the child's own progress rides.
        on_checkpoint({"phase": "scheduling", "cursor_kind": "fair_queue_advancement", "cursor": 1})

    return PooledCellDispatch(
        context,
        ready,
        workers=settings.RESEARCH_WORKER_COUNT,
        window=settings.RESEARCH_DISPATCH_WINDOW,
        pool=pool or shared_worker_pool(
            workers=settings.RESEARCH_WORKER_COUNT,
            submission_bound=SUBMISSION_FACTOR * settings.RESEARCH_WORKER_COUNT,
            payload_bound_bytes=settings.RESEARCH_POOL_PAYLOAD_BOUND_BYTES,
            result_bound_bytes=settings.RESEARCH_POOL_RESULT_BOUND_BYTES,
        ),
        on_facts=on_facts,
        on_capacity_handoff=on_capacity_handoff,
    )
