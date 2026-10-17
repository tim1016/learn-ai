"""Options evaluations use the existing bounded shared research worker pool (#3339).

Workers receive immutable retained inputs, compute one complete chronological
window and return typed evidence. Only the parent owns attempts and persistence.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from app.config import settings
from app.options_backtest.run_json import option_days_to_json, pins_from_json
from app.options_backtest.session_result import SessionPins
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.golden_search.evaluator import RETRY_ALLOWANCE, WorkerLost
from app.research.golden_search.shared_pool import (
    SUBMISSION_FACTOR,
    PooledOwnerDispatch,
    SharedWorkerPool,
    shared_worker_pool,
)
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.options_golden_search.evaluator import EvaluationResult, with_input_failures
from app.research.options_golden_search.metrics import evidence_from_json, evidence_to_json, summarize_evidence
from app.research.options_golden_search.protocol import Window

OPTIONS_WIRE_KIND = "options_golden_search"
def pins_to_wire(pins: tuple[SessionPins, ...]) -> list[dict[str, Any]]:
    return [{"underlying_sha256": pin.underlying_sha256, "listing_sha256": pin.listing_sha256,
             "option_days": option_days_to_json(pin)} for pin in pins]


def pins_from_wire(rows: Sequence[Mapping[str, Any]]) -> tuple[SessionPins, ...]:
    return tuple(pins_from_json(row["underlying_sha256"], row["listing_sha256"], row["option_days"]) for row in rows)



@dataclass(frozen=True)
class OptionsDelivery:
    key: str
    attempt: int
    task_gen: int
    result: EvaluationResult
    actual_reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadyOptions:
    key: str
    attempt: int
    task_gen: int
    payload: Mapping[str, Any]
    begin: Callable[[], None] = lambda: None
    on_generation: Callable[[int], None] = lambda generation: None

    def as_wire(self) -> dict[str, Any]:
        return {"kind": OPTIONS_WIRE_KIND, "key": self.key, "attempt": self.attempt,
                "task_gen": self.task_gen, "options": dict(self.payload)}

    def outcome_of(self, message: Mapping[str, Any]) -> OptionsDelivery:
        if message.get("key") != self.key:
            raise WorkerLost("Options worker returned evidence under another evaluation identity")
        if message.get("type") == "failed":
            raise RuntimeError(str(message.get("error", "Options worker evaluation failed")))
        wire = message["options_result"]
        setup = OptionsBacktestSetup.model_validate(self.payload["setup"])
        window = Window.model_validate(self.payload["window"])
        sessions = tuple(evidence_from_json(row) for row in wire["sessions"])
        pins = pins_from_wire(wire["pins"])
        metrics = summarize_evidence(sessions, starting_capital_usd=setup.starting_cash_usd,
            annualization_sessions=window.annualization_sessions,
            risk_free_daily_return=float(self.payload.get("risk_free_daily_return", 0)))
        input_failures = tuple(wire.get("input_failures", ()))
        result = EvaluationResult(sessions, pins, with_input_failures(metrics, input_failures), len(window.session_ms), input_failures)
        return OptionsDelivery(self.key, self.attempt, self.task_gen, result, tuple(wire["actual_reads"]))


class OptionsDispatch(PooledOwnerDispatch):
    """One parent owner on the process-wide pool; next pull acknowledges durable evidence."""

    def __init__(self, context: WorkerDispatchContext, *, workers: int | None = None,
                 pool: SharedWorkerPool | None = None) -> None:
        width = workers if workers is not None else settings.OPTIONS_SEARCH_WORKER_COUNT
        # A caller cannot grant itself capacity beyond the qualified operator profile.
        if width < 1 or width > min(settings.OPTIONS_SEARCH_WORKER_COUNT, settings.RESEARCH_WORKER_COUNT):
            raise ValueError("Options worker width exceeds the current operator profile")
        super().__init__(context, workers=width, window=width,
            pool=pool or shared_worker_pool(workers=settings.RESEARCH_WORKER_COUNT,
                submission_bound=SUBMISSION_FACTOR * settings.RESEARCH_WORKER_COUNT,
                payload_bound_bytes=settings.RESEARCH_POOL_PAYLOAD_BOUND_BYTES,
                result_bound_bytes=settings.RESEARCH_POOL_RESULT_BOUND_BYTES))
        from app.research.golden_search.worker import pooled_cache_bytes

        context.decoded_cache_bytes = pooled_cache_bytes(settings.RESEARCH_WORKER_COUNT)

    def __call__(self, batch: Sequence[ReadyOptions]) -> Iterator[OptionsDelivery]:
        owed = {item.key: item for item in batch}
        if len(owed) != len(batch):
            raise ValueError("dispatch batch contains duplicate evaluation identities")
        retries: dict[str, int] = {}
        epoch, state = self._begin(len(owed))
        try:
            for item, arrival in self._pump(owed, state, epoch):
                if isinstance(arrival, WorkerLost):
                    retries[item.key] = retries.get(item.key, 0) + 1
                    if retries[item.key] > RETRY_ALLOWANCE:
                        raise WorkerLost("Options evaluation exhausted its physical worker retry allowance")
                    item.on_generation(item.task_gen + 1)
                    owed[item.key] = replace(item, task_gen=item.task_gen + 1)
                    state.submitted.discard(item.key)
                else:
                    yield arrival
                self._feed(owed, state, epoch)
        finally:
            self._emit_facts(len(owed), state.adaptive_wait_ms)


def run_options_task(task: Mapping[str, Any], *, out: Any,
                     ask_admission: Callable[[Any], bytes], cancel_flag: threading.Event,
                     state: dict[str, Any]) -> None:
    """Execute against retained blobs alone; there is no database or mutable lake reader."""
    from pathlib import Path

    from app.options_backtest.setup import OptionsBacktestSetup
    from app.research.golden_search.proof import BlobStore
    from app.research.golden_search.worker import _residency_fields, _task_checkpoint
    from app.research.options_golden_search.evaluator import evaluate_setup
    from app.research.options_golden_search.identity import execution_identity
    from app.research.options_golden_search.protocol import Window
    from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData

    try:
        payload = task["options"]
        if execution_identity() != payload["identity"]:
            out.write({"type": "identity_refused", "key": task["key"], "reason": "Options execution identity differs from locked study"})
            return
        checkpoint = _task_checkpoint(out, state)
        checkpoint("preparation")
        setup = OptionsBacktestSetup.model_validate(payload["setup"])
        window = Window.model_validate(payload["window"])
        snapshot = Snapshot.model_validate(payload["snapshot"])
        blobs = BlobStore(Path(payload["blob_root"]))
        data = SnapshotRunData(setup.underlying, snapshot, blobs)
        result = evaluate_setup(setup, window, data,
            fee_multiplier=Decimal(payload.get("fee_multiplier", "1")),
            risk_free_daily_return=float(payload.get("risk_free_daily_return", 0)),
            should_stop=cancel_flag.is_set,
            on_progress=lambda cursor: checkpoint("search", cursor_kind="processed_bars", cursor=cursor) if cursor % 60 == 0 else None)
        state["completed"] = int(state.get("completed", 0)) + 1
        checkpoint("search", cursor_kind="durable_completions", cursor=state["completed"])
        out.write({"type": "result", "key": task["key"],
                   "options_result": {"sessions": [evidence_to_json(s) for s in result.sessions],
                       "pins": pins_to_wire(result.pins), "input_failures": result.input_failures,
                       "actual_reads": sorted(data.actual_reads)}, **_residency_fields(state)})
    except BaseException as exc:  # terminal child sink: every failure is an explicit delivered answer
        out.write({"type": "failed", "key": str(task.get("key", "?")),
                   "error": f"{type(exc).__name__}: {exc}", **_residency_fields(state)})
