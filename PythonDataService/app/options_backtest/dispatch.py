"""Options Backtest chains are owners on the existing shared pool (#3423, #3428)."""
from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.options_backtest.chains import merge_chains, plan_chains, replay_chain
from app.options_backtest.execution_identity import backtest_execution_identity
from app.options_backtest.frozen_inputs import FrozenRunData, FrozenRunInputs
from app.options_backtest.run import ReplayChain, RunResult
from app.options_backtest.run_wire import result_from_wire, result_to_wire
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_backtest.timings import PhaseTiming, ReplayObservations, RunTimings
from app.research.golden_search.evaluator import WorkerLost
from app.research.golden_search.shared_pool import SharedWorkerPool
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.options_golden_search.dispatch import OptionsDispatch
from app.research.options_golden_search.protocol import point_hash
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import et_date_at_ms

WIRE_KIND = "options_backtest_chain"


@dataclass(frozen=True)
class ChainDelivery:
    key: str
    attempt: int
    task_gen: int
    result: RunResult
    timings: tuple[PhaseTiming, ...]
    cache_hits: int
    cache_misses: int
    worker_pid: int
    cache_peak_bytes: int


@dataclass(frozen=True)
class ReadyChain:
    key: str
    payload: Mapping[str, Any]
    attempt: int = 1
    task_gen: int = 1
    begin: Callable[[], None] = lambda: None
    on_generation: Callable[[int], None] = lambda generation: None

    def as_wire(self) -> dict[str, Any]:
        return {"kind": WIRE_KIND, "key": self.key, "attempt": self.attempt,
                "task_gen": self.task_gen, "options": dict(self.payload)}

    def outcome_of(self, message: Mapping[str, Any]) -> ChainDelivery:
        if message.get("key") != self.key:
            raise WorkerLost("chain answer belongs to another run")
        if message.get("type") != "result":
            raise RuntimeError(str(message.get("error", "mandatory options chain failed")))
        return ChainDelivery(self.key, self.attempt, self.task_gen,
            result_from_wire(message["backtest_result"]),
            tuple(PhaseTiming.model_validate(row) for row in message["timings"]),
            int(message["cache_hits"]), int(message["cache_misses"]),
            int(message["worker_pid"]), int(message["cache_peak_bytes"]))


def replay_frozen_run(
    setup: OptionsBacktestSetup, sessions: Sequence[date], frozen: FrozenRunInputs,
    *, context: WorkerDispatchContext, timings: RunTimings, workers: int = 1,
    pool: SharedWorkerPool | None = None,
    cold_cache: bool = False,
) -> RunResult:
    """Whole-result boundary: every mandatory chain succeeds before merge/save.

    Waiting for a lane is ``queue`` until the first chain begins, and ``replay`` after, split on one clock sample.
    What the replay measured is observed on ``timings`` however it ends.
    """
    if frozen.setup_digest != point_hash(setup) or frozen.underlying != setup.underlying:
        raise ValueError("frozen inputs belong to another setup")
    identity = backtest_execution_identity()
    plan = plan_chains(setup)
    cache_epoch = context.job_id if cold_cache else None
    deliveries: dict[str, RunResult] = {}
    with ExitStack() as cleanup:
        with timings.measure("queue") as lap:
            dispatch = OptionsDispatch(context, workers=workers, pool=pool)
            cleanup.callback(dispatch.close)
            hits = misses = 0
            residency: dict[int, int] = {}
            batch = [ReadyChain(key=f"{context.job_id}:{chain.key}", begin=lambda: lap("replay"),
                payload={"setup": setup.model_dump(mode="json"), "chain": {
                    "kind": chain.kind, "slippage_percent": str(chain.slippage_percent),
                    "starting_cash_usd": str(chain.starting_cash_usd), "multiple": str(chain.multiple)},
                    "frozen": frozen.model_dump(mode="json"), "identity": identity,
                    "cache_epoch": cache_epoch}) for chain in plan]
            try:
                for delivery in dispatch(batch):
                    chain_key = delivery.key.removeprefix(f"{context.job_id}:")
                    if chain_key in deliveries:
                        raise ValueError("duplicate chain delivery cannot complete a run twice")
                    deliveries[chain_key] = delivery.result
                    timings.adopt(delivery.timings)
                    residency[delivery.worker_pid] = max(residency.get(delivery.worker_pid, 0),
                                                         delivery.cache_peak_bytes)
                    hits += delivery.cache_hits
                    misses += delivery.cache_misses
            finally:
                timings.observe_replay(ReplayObservations(
                    frozen_input_digest=frozen.digest, execution_identity_digest=hash_payload(identity),
                    runtime_boot_generation=context.generation, worker_width=workers, cache_epoch=cache_epoch,
                    cache_hits=hits, cache_misses=misses, cache_bytes_peak=sum(residency.values())))
        with timings.measure("aggregation"):
            return merge_chains(setup, sessions, deliveries)


def run_backtest_chain_task(task: Mapping[str, Any], *, out: Any,
    ask_admission: Callable[[Any], bytes], cancel_flag: threading.Event, state: dict[str, Any]) -> None:
    """A local frozen replay only. No database, provider, or mutable lake calls."""
    import os
    from decimal import Decimal

    from app.research.golden_search.worker import _residency_fields, _task_checkpoint

    try:
        payload = task["options"]
        if payload["identity"] != backtest_execution_identity():
            out.write({"type": "identity_refused", "key": task["key"], "reason": "Options code identity differs"})
            return
        checkpoint = _task_checkpoint(out, state)
        checkpoint("preparation")
        setup = OptionsBacktestSetup.model_validate(payload["setup"])
        frozen = FrozenRunInputs.model_validate(payload["frozen"])
        wire = payload["chain"]
        chain = ReplayChain(wire["kind"], Decimal(wire["slippage_percent"]), Decimal(wire["starting_cash_usd"]),
                            Decimal(wire["multiple"]))
        cache = state["decoded_cache"]
        epoch = payload.get("cache_epoch")
        cold_epochs = state.setdefault("options_cold_epochs", set())
        if epoch is not None and epoch not in cold_epochs:
            cache.clear()
            # Task windows are globally bounded; the epoch is a benchmark
            # request identity, and this bounded history covers interleaved
            # cold runs without clearing again on every fair-queue turn.
            if len(cold_epochs) >= 128:
                raise ValueError("cold benchmark epoch bound reached; restart the benchmark worker profile")
            cold_epochs.add(epoch)
        before = cache.metrics()
        cache.take_peak_bytes()
        ledger = RunTimings()
        data = FrozenRunData(frozen, cache, timings=ledger, chain_key=chain.key)
        with ledger.measure("replay", chain_key=chain.key):
            result = replay_chain(setup, tuple(et_date_at_ms(ms) for ms in frozen.sessions), chain, data,
                should_stop=cancel_flag.is_set,
                on_session=lambda done, total, day: checkpoint("simulation", cursor_kind="processed_blocks", cursor=done))
        after = cache.metrics()
        out.write({"type": "result", "key": task["key"], "backtest_result": result_to_wire(result),
                   "timings": ledger.wire(),
                   "cache_hits": after["hits"] - before["hits"], "cache_misses": after["misses"] - before["misses"],
                   "worker_pid": os.getpid(),
                   "cache_peak_bytes": cache.take_peak_bytes(),
                   **_residency_fields(state)})
    except BaseException as exc:
        out.write({"type": "failed", "key": str(task.get("key", "?")),
                   "error": f"{type(exc).__name__}: {exc}", **_residency_fields(state)})
