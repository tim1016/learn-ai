"""Recency runs through the shared research worker runtime (#3051; PRD #3034 slice 16).

A Recency launch keeps its own scientific contract whole: runs stay keyed
by the composite (symbol, strategy, params_hash) cell identity — never a
Golden Search key — and snapshots, trade/evidence deduplication and the
durable-vs-per-attempt accounting stay in the coordinator under the launch
attempt fence (ADR 0065 decision 4). What moves into the runtime is only
the computation: one run is one task for a spawned research worker — the
same child, wire, admission batcher and decoded-cache budget Golden Search
and Grid Search own (#3044-#3049) — so independent runs overlap on the
pool while the launch's counts, skip set and cancellation contract never
see it.

The wire is recency's own task kind: the window as ``int64 ms UTC``, the
frozen fill/commission execution block, the sealed code identity. The
child rebuilds the engine request through ``engine_request_over`` — the
exact constructor the in-process executor uses — and returns only the
pieces the coordinator's snapshot needs: the resolved data policy, the
trades and the study id. Fingerprints, windowed P&L and Sharpe are
computed coordinator-side from those pieces, exactly as the serial path
computes them from the engine's response, so a dispatched run and a local
one produce identical persisted trades, fees and evidence (ADR 0072).

Worker loss is not a run verdict: a lost run is re-offered under an
advanced task generation within the evaluator's retry allowance, and a
loss past the bound aborts the launch honestly — a failed run leaves no
durable trace and a resume re-runs it, so the abort is recoverable by the
existing Finish. Arrival order is free; persistence order is the
coordinator's.
Reference: #3051 / PRD #3034; ``app.research.grid_search.dispatch`` for
the twin this mirrors; ``app.research.recency.runner`` for the loop.
Canonical implementation: this file.
Validated against: tests/research/recency/test_dispatch.py.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Protocol

from app.config import settings
from app.jobs.progress import JobCancelled
from app.research.golden_search.evaluator import RETRY_ALLOWANCE, IdentityRefusalError, WorkerLost
from app.research.golden_search.shared_pool import (
    SUBMISSION_FACTOR,
    PooledOwnerDispatch,
    SharedWorkerPool,
    shared_worker_pool,
)
from app.research.golden_search.worker import ChildOwnerDispatch, WorkerDispatchContext
from app.research.persistence.ownership import GenerationFencedError
from app.research.sweep.grid import RunSpec

logger = logging.getLogger(__name__)

#: The task-kind marker a recency run carries on the worker wire (#3051).
RECENCY_WIRE_KIND = "recency_run"

def run_key(symbol: str, strategy_key: str, params_hash: str) -> str:
    """The dispatch identity of one run: the composite cell identity, never the params hash alone.

    A launch may sweep the same parameter assignment across symbols or
    strategies, so ``params_hash`` is not unique across the grid; the
    durable skip set is the same triple (ADR 0065 decision 5), and the
    dispatch key must be too.
    """
    return f"{symbol}|{strategy_key}|{params_hash}"


# ── The ready item and its outcome ────────────────────────────────────────


@dataclass(frozen=True)
class ReadyRun:
    """One admitted run a dispatcher receives: keyed by the composite cell identity, generation-fenced.

    Recency keeps no reservation rows — a run is durable only once its
    snapshot persists — so ``begin`` is the contract's no-op and
    ``task_gen`` fences only re-offers after a worker loss. The window and
    execution blocks are the launch's frozen parts; the child rebuilds the
    engine request from them.
    """

    key: str
    attempt: int
    task_gen: int
    run_spec: RunSpec
    window: Mapping[str, int]
    execution: Mapping[str, Any]
    identity: Mapping[str, Any]
    begin: Callable[[], None] = lambda: None

    def as_wire(self) -> dict[str, Any]:
        return {
            "kind": RECENCY_WIRE_KIND,
            "key": self.key,
            "attempt": self.attempt,
            "task_gen": self.task_gen,
            "run": {
                "symbol": self.run_spec.symbol,
                "strategy_key": self.run_spec.strategy_key,
                "params": dict(self.run_spec.params),
                "params_hash": self.run_spec.params_hash,
            },
            "window": dict(self.window),
            "execution": dict(self.execution),
            "identity": {
                "source_digest": str(self.identity["source_digest"]),
                "environment_digest": str(self.identity["environment_digest"]),
                "digest_scheme": self.identity["digest_scheme"],
            },
        }

    def outcome_of(self, message: Mapping[str, Any]) -> RunOutcome:
        return run_outcome(self, message)


@dataclass(frozen=True)
class RunOutcome:
    """One dispatched run's arrived outcome — recency's twin of grid's ``CellOutcome``.

    A failed run arrives
    as its error string exactly as the serial loop's ``on_error`` would see
    it; a succeeded run arrives as the coordinator's snapshot pieces. The
    attempt and task generation travel with the outcome so a straggler
    from a superseded re-offer is discarded in memory.
    """

    key: str
    attempt: int
    task_gen: int
    pieces: dict[str, Any] | None
    error: str | None = None


def run_outcome(item: ReadyRun, message: Mapping[str, Any]) -> RunOutcome:
    """One child answer as the runner consumes it — the same translation both adapters make."""
    if message.get("key") != item.key:
        # The item's identity is the one the launch's cell fence will check;
        # a child answer keyed otherwise is a protocol break, never a foreign
        # run the durable record would skip forever (the grid twin's rule).
        raise WorkerLost(f"a research worker answered run {item.key} as {message.get('key')!s}")
    if message["type"] == "failed":
        return RunOutcome(
            key=item.key,
            attempt=item.attempt,
            task_gen=item.task_gen,
            pieces=None,
            error=str(message.get("error", "worker failure")),
        )
    return RunOutcome(
        key=item.key,
        attempt=item.attempt,
        task_gen=item.task_gen,
        pieces=dict(message["run"]),
    )


class RunPieces:
    """The wire's run pieces as the snapshot builder reads them — the engine response's recency slice.

    ``_snapshot_for_run`` reads the resolved policy (as its JSON), the
    trades and the study id; those three cross the wire, and this wrapper
    presents them under the same attribute names.
    """

    def __init__(self, pieces: Mapping[str, Any]) -> None:
        self.study_id = pieces.get("study_id")

        class _PolicyJson:
            def __init__(self, dumped: str) -> None:
                self._dumped = dumped

            def model_dump_json(self) -> str:
                return self._dumped

        self.data_policy = _PolicyJson(str(pieces["data_policy"]))
        self.trades = [
            SimpleNamespace(
                entry_time=int(trade["entry_ms"]),
                exit_time=int(trade["exit_ms"]),
                pnl_pts=float(trade["pnl_pts"]),
                pnl_pct=float(trade["pnl_pct"]),
                quantity=int(trade["quantity"]),
                is_synthetic_exit=bool(trade["is_synthetic_exit"]),
                signal_reason=str(trade["signal_reason"]),
            )
            for trade in pieces["trades"]
        ]


def pieces_to_wire(response: Any) -> dict[str, Any]:
    """One engine response's recency slice as plain JSON data for the result message."""
    return {
        "data_policy": response.data_policy.model_dump_json() if response.data_policy is not None else None,
        "trades": [
            {
                "entry_ms": trade.entry_time,
                "exit_ms": trade.exit_time,
                "pnl_pts": trade.pnl_pts,
                "pnl_pct": trade.pnl_pct,
                "quantity": trade.quantity,
                "is_synthetic_exit": trade.is_synthetic_exit,
                "signal_reason": trade.signal_reason,
            }
            for trade in response.trades
        ],
        "study_id": response.study_id,
    }


#: What ``run_recency`` consumes from either adapter: each arrival as its
#: ``(run, pieces, error)`` triple — a succeeded run's snapshot pieces, or
#: the failure string the serial loop's ``on_error`` would have seen. The
#: lifecycle members are the contract's, not optional: the coordinator reaps
#: (``cancel``) before its abort-state writes and closes (``close``) on
#: every exit path (#3065's rule).
class RunDispatch(Protocol):
    def __call__(self, runs: Iterable[RunSpec]) -> Iterator[tuple[RunSpec, dict[str, Any] | None, str | None]]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


def ready_run_builder(window: Mapping[str, int], execution: Mapping[str, Any], identity: Mapping[str, Any], attempt: int) -> Callable[[RunSpec], ReadyRun]:
    """The per-run item builder: the launch's frozen parts, one cell's composite identity."""

    def build(run_spec: RunSpec) -> ReadyRun:
        return ReadyRun(
            key=run_key(run_spec.symbol, run_spec.strategy_key, run_spec.params_hash),
            attempt=attempt,
            task_gen=1,
            run_spec=run_spec,
            window=window,
            execution=execution,
            identity=identity,
        )

    return build


# ── The child's half of one task (#3051) ──────────────────────────────────


def run_recency_run_task(
    task: Mapping[str, Any],
    *,
    out: Any,
    ask_admission: Callable[[Any], bytes],
    cancel_flag: threading.Event,
    state: dict[str, Any],
) -> None:
    """Execute one recency run in the child: identity-check, engine call, the recency projection.

    Mirrors the golden and grid task runners' sinks exactly — preparation
    checkpoint, admission-routed reads, cancellation through the raise-only
    poll, the durable-completions checkpoint, residency hints on every
    answer — with the request built by recency's own constructor and the
    response reduced to the pieces the coordinator's snapshot needs.
    """
    from app.research.golden_search.worker import _expected_identity, _residency_fields, _task_checkpoint
    from app.research.recency.service import engine_request_over
    from app.services.engine_backtest_service import execute_engine_backtest

    run_spec = RunSpec(
        symbol=str(task["run"]["symbol"]),
        strategy_key=str(task["run"]["strategy_key"]),
        params={str(name): float(value) for name, value in task["run"]["params"].items()},
        params_hash=str(task["run"]["params_hash"]),
    )

    try:
        if state["identity"] != _expected_identity(task):
            out.write(
                {"type": "identity_refused", "key": task["key"], "reason": "sealed identity differs from the task's"}
            )
            return
        checkpoint = _task_checkpoint(out, state)
        checkpoint("preparation")
        response = execute_engine_backtest(
            request=engine_request_over(run_spec, window=task["window"], execution=task["execution"]),
            on_phase=lambda phase: checkpoint(phase),
            on_log=lambda message: None,
            decoded_cache=state["decoded_cache"],
            admission=ask_admission,
        )
        if not response.success:
            # The serial executor raises on an unsuccessful run; the child
            # delivers it as the run's failure, exactly as ``on_error`` sees it.
            raise RuntimeError(response.error or "backtest did not succeed")
        state["completed"] = int(state.get("completed", 0)) + 1
        checkpoint("search", cursor_kind="durable_completions", cursor=state["completed"])
        out.write({"type": "result", "key": task["key"], "run": pieces_to_wire(response), **_residency_fields(state)})
    except BaseException as exc:  # the child's terminal sink: every failure is a delivered answer
        out.write(
            {
                "type": "failed",
                "key": str(task.get("key", "?")),
                "error": f"{type(exc).__name__}: {exc}",
                **_residency_fields(state),
            }
        )


# ── The serial adapter: one owned child, one task lane ────────────────────


class WorkerRunDispatch(ChildOwnerDispatch):
    """The one-worker run dispatch: recency's twin of the grid serial adapter (#3051).

    The child lifecycle is the shared serial base's; this subclass is
    exactly recency's wire, decode and loss policy.
    """

    def __init__(self, context: WorkerDispatchContext, ready: Callable[[RunSpec], ReadyRun]) -> None:
        super().__init__(context)
        self._ready = ready

    def __call__(self, runs: Iterable[RunSpec]) -> Iterator[tuple[RunSpec, dict[str, Any] | None, str | None]]:
        for run_spec in runs:
            self._poll_cancel()
            item = self._ready(run_spec)
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
                            f"recency run {item.key} lost its worker {losses} times; "
                            "the launch aborts rather than record a run that never happened"
                        ) from exc
                    item = replace(item, task_gen=item.task_gen + 1)
                    logger.warning(
                        "a research worker was lost with recency run %s in flight; re-offering under generation %s (%s)",
                        item.key,
                        item.task_gen,
                        exc,
                        extra={"action": "recency_run_reoffered", "run_key": item.key},
                    )
                    continue
                except (JobCancelled, GenerationFencedError, IdentityRefusalError):
                    self._worker_lost(worker)
                    raise
                break
            try:
                outcome = item.outcome_of(message)
            except WorkerLost:
                # A protocol break from this child: reap it before the raise.
                self._worker_lost(worker)
                raise
            yield run_spec, outcome.pieces, outcome.error


# ── The pooled adapter: one owner on the shared pool ──────────────────────


class PooledRunDispatch(PooledOwnerDispatch):
    """The run dispatch as one owner on a worker pool (#3051), over the shared owner base.

    The window arithmetic, epoch fence, cancellation poll and budget
    settlement live in the base (#3049); this subclass is exactly
    recency's per-arrival policy: a lost lane's run is re-offered under an
    advanced task generation within the retry allowance, a loss past the
    bound aborts the launch honestly, and a delivered cancellation
    re-raises on the runner's terms.
    """

    def __init__(
        self,
        context: WorkerDispatchContext,
        ready: Callable[[RunSpec], ReadyRun],
        *,
        workers: int,
        window: int,
        pool: SharedWorkerPool | None = None,
        on_facts: Callable[[Mapping[str, Any]], None] = lambda facts: None,
        on_capacity_handoff: Callable[[], None] = lambda: None,
    ) -> None:
        super().__init__(
            context, workers=workers, window=window, pool=pool,
            on_capacity_handoff=on_capacity_handoff, on_facts=on_facts,
        )
        self._ready = ready

    def __call__(self, runs: Iterable[RunSpec]) -> Iterator[tuple[RunSpec, dict[str, Any] | None, str | None]]:
        owed: dict[str, ReadyRun] = {}
        candidates: dict[str, RunSpec] = {}
        for run_spec in runs:
            item = self._ready(run_spec)
            owed[item.key] = item
            candidates[item.key] = run_spec
        retries: dict[str, int] = {}
        epoch, state = self._begin(len(owed))
        try:
            for item, arrival in self._pump(owed, state, epoch):
                if isinstance(arrival, WorkerLost):
                    retries[item.key] = retries.get(item.key, 0) + 1
                    if retries[item.key] > RETRY_ALLOWANCE:
                        raise WorkerLost(
                            f"recency run {item.key} lost its worker {retries[item.key]} times; "
                            "the launch aborts rather than record a run that never happened"
                        )
                    owed[item.key] = replace(item, task_gen=item.task_gen + 1)
                    state.submitted.discard(item.key)
                    logger.warning(
                        "a pooled research worker was lost with recency run %s in flight; re-offering under generation %s",
                        item.key,
                        owed[item.key].task_gen,
                        extra={"action": "recency_run_reoffered", "run_key": item.key},
                    )
                else:
                    yield candidates.pop(item.key), arrival.pieces, arrival.error
                # The runner resumed — the snapshot it took is durable — so
                # the window opens by one for the next submission (or the
                # lost run's re-offer takes the freed permit).
                self._feed(owed, state, epoch)
        finally:
            self._emit_facts(len(owed), state.adaptive_wait_ms)


# ── What the runtime builds (#3051) ───────────────────────────────────────


def runtime_run_dispatch(
    window_ms: Mapping[str, int],
    execution: Mapping[str, Any],
    *,
    attempt: int,
    job_id: str | None,
    on_checkpoint: Callable[[Mapping[str, Any]], None],
    cancel_check: Callable[[], object],
    on_log: Callable[[str], None] = lambda message: None,
    on_waiting: Callable[[str | None, Mapping[str, Any]], None] = lambda waiting, facts: None,
    pool: SharedWorkerPool | None = None,
) -> WorkerRunDispatch | PooledRunDispatch:
    """The dispatch the research runtime executes recency runs through (#3051).

    ``RESEARCH_WORKER_COUNT`` is the deployment's opt-in width — one is a
    lone spawned child — and every wider run is one owner on the process's
    shared pool. A recency launch carries no data manifest (its reads are
    the engine's own lake policy), so there is no warm-affinity hint to
    bind; fairness and the bounds are the pool's either way.
    """
    from app.research.sweep.identity import resolve_code_identity

    identity = resolve_code_identity().as_dict()
    ready = ready_run_builder(window_ms, execution, identity, attempt)
    context = WorkerDispatchContext(
        strategy_key="recency",
        # A recency run carries its own frozen execution block per task wire;
        # the context's execution field is the golden wire's, unused here.
        execution={},
        manifest={},
        identity=identity,
        job_id=job_id or "",
        generation=attempt,
    )
    context.on_checkpoint = on_checkpoint
    context.cancel_check = cancel_check
    if settings.RESEARCH_WORKER_COUNT < 2:
        return WorkerRunDispatch(context, ready)

    def on_facts(facts: Mapping[str, Any]) -> None:
        from app.research.golden_search.shared_pool import waiting_state_of

        on_log(f"research worker pool: {dict(facts)}")
        on_waiting(waiting_state_of(facts), dict(facts))

    def on_capacity_handoff() -> None:
        on_checkpoint({"phase": "scheduling", "cursor_kind": "fair_queue_advancement", "cursor": 1})

    return PooledRunDispatch(
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
