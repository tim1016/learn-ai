"""One persistent spawned worker executes Golden Search evaluations (#3044, PRD #3034 slice 9).

The child is a bare interpreter — ``python -m app.research.golden_search.worker``
— started with ``PYTHONHASHSEED=0`` and the native thread counts pinned to
one *before interpreter startup*, speaking one JSON-line protocol over
stdin/stdout. It owns computation and nothing else: no database, no Redis,
no provider, no broker, no schema. Every artifact read it performs asks its
parent for the current committed-byte admission verdict over the same pipe
(a fresh verdict per read — no authorization survives an individual read),
then runs the manifest/adjustment checks and the decode — the same
``execute_engine_backtest`` the API runs in-process, gates and all, in the
child where they are process-local. Results, detail and checkpoints come
back as plain JSON, the exact shape the durable store already polices.

The parent side (this module's :class:`ResearchWorker`) owns the child from
spawn through import, readiness, work and exit: bounded startup, one task
in flight, admission verdicts relayed on the coordinator's own I/O path,
checkpoints forwarded to the job's lease gate, and kill-and-reap cleanup on
silence, loss or shutdown. The coordinator never enters the engine gate
while waiting on the child; one child serializes its own runs.

Reference: #3044 / PRD #3034; ADR 0077 (the runtime this worker lives in);
``app.services.option_search`` for the spawn/ownership/kill-and-reap
precedent; ``app.jobs.checkpoints`` for the lease contract these
checkpoints feed; #3038 for the per-process decoded-artifact cache.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.research.golden_search.evaluator import (
    DeliveredOutcome,
    EvaluationRequest,
    EvaluationResult,
    ReadyEvaluation,
    project_response,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# The PRD's envelope: readiness/import bound, and the silence bound between
# real signals. Work itself is unbounded — an evaluation runs as long as it
# runs — but a child that stops emitting alives, checkpoints or answers for
# this long is dead or wedged and gets fenced.
STARTUP_BOUND_S = 60.0
# Above the catalog's own 35s verify bound: a slow verdict must never read
# as a silent child (#3044 review).
SILENCE_BOUND_S = 45.0
# Heartbeats prove the process, never the work: a wedged engine loop keeps
# heartbeating forever, so task progress — checkpoints, admission requests,
# outcomes — must arrive at least this often. Generous beyond any legitimate
# single evaluation until the child emits advancing in-engine checkpoints,
# which is #3045's work and will tighten this to seconds.
TASK_SILENCE_BOUND_S = 30 * 60.0
ALIVE_INTERVAL_S = 5.0
KILLED_CHILD_WAIT_S = 2.0

# The environment every child is born with: deterministic hashing and
# single-threaded native math, set before interpreter startup because a
# worker initializer would be too late for both (PRD #3034).
CHILD_ENV_FIXED = {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


class WorkerLost(RuntimeError):
    """The spawned worker died or went silent with work in flight (#3044).

    The affected evaluations are not failed evidence — they never ran to an
    answer — so their reservations are released and the stage interrupts;
    bounded retry is #3045's, not this slice's.
    """


# ── The wire (one JSON object per line; every time is int64 ms UTC) ───────
#
# parent -> child:
#   {"type": "task", "task": {...}}            one evaluation to run
#   {"type": "admission", "id": n, ...verdict} the answer to one read
#   {"type": "cancel"}                         stop; the child exits after
#                                              its in-flight run observes it
# child -> parent:
#   {"type": "ready", "pid": ..., "hash_seed": 0, "identity": {...}}
#   {"type": "identity_refused", "reason": str}
#   {"type": "admission_request", "id": n, "path": str, "digest": str, "size": n}
#   {"type": "checkpoint", ...}                an ExecutionCheckpoint's fields
#   {"type": "alive", "at_ms": n}              liveness only; renews nothing
#   {"type": "result", "key": str, "metrics": {...}, "detail": {...}|null, "curve": [[ms, v]]|null}
#   {"type": "failed", "key": str, "error": str}


def task_wire(
    item: ReadyEvaluation,
    *,
    strategy_key: str,
    execution: Mapping[str, Any],
    manifest: Mapping[str, str],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    """One evaluation as plain JSON data — the full key, generation and frozen context, no live objects."""
    request = item.request
    scenario = None
    if request.scenario is not None:
        scenario = {
            "key": request.scenario.key,
            "label": request.scenario.label,
            "slippage_add": request.scenario.slippage_add,
            "commission_add": request.scenario.commission_add,
            "fill_mode": request.scenario.fill_mode,
        }
    return {
        "key": item.key,
        "attempt": item.attempt,
        "point": dict(request.point),
        "point_hash": request.point_hash,
        "window_start_ms": request.window[0],
        "window_end_ms": request.window[1],
        "warmup_from_ms": _et_midnight_ms(request.warmup_from),
        "scenario": scenario,
        "detail": request.detail,
        "stage": request.stage,
        "fold_index": request.fold_index,
        "strategy_key": strategy_key,
        "execution": dict(execution),
        "manifest": dict(manifest),
        "identity": {
            "source_digest": identity["source_digest"],
            "environment_digest": identity["environment_digest"],
            "digest_scheme": identity["digest_scheme"],
        },
    }


def _et_midnight_ms(day: Any) -> int:
    from app.utils.session_anchors import et_midnight_ms

    return et_midnight_ms(day)


def request_of_wire(task: Mapping[str, Any]) -> tuple[EvaluationRequest, str]:
    """Rebuild the evaluation request in the child; dates cross as ET-midnight ms anchors (ADR 0022)."""

    from app.research.golden_search.protocol import StressScenario
    from app.utils.session_anchors import et_date_at_ms

    scenario = None
    if task["scenario"] is not None:
        scenario = StressScenario(**task["scenario"])
    request = EvaluationRequest(
        point=dict(task["point"]),
        point_hash=task["point_hash"],
        window=(int(task["window_start_ms"]), int(task["window_end_ms"])),
        warmup_from=et_date_at_ms(int(task["warmup_from_ms"])),
        scenario=scenario,
        detail=bool(task["detail"]),
        stage=str(task["stage"]),
        fold_index=task["fold_index"],
    )
    return request, str(task["strategy_key"])


# ── The child ─────────────────────────────────────────────────────────────


def _child_main() -> int:
    """The child's whole life: ready, then one task at a time until the pipe closes."""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent kills; a terminal interrupt must not race it
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s worker %(message)s")

    out = _LockedStdout()
    identity = _child_identity()
    pinned = {key: os.environ.get(key) for key in CHILD_ENV_FIXED}
    if any(value != expected for value, expected in zip(pinned.values(), CHILD_ENV_FIXED.values(), strict=True)):
        out.write({"type": "identity_refused", "reason": f"hash/native-thread environment not pinned: {pinned}"})
        return 2
    out.write({"type": "ready", "pid": os.getpid(), "env": pinned, "identity": identity})

    pending_admissions: dict[int, threading.Event] = {}
    admission_answers: dict[int, Any] = {}
    cancel_flag = threading.Event()
    from app.engine.data.decoded_cache import DecodedArtifactCache

    # One cache for the child's whole life: the persistent worker exists to
    # reuse admitted decodes across evaluations, so it must not be rebuilt
    # per task (#3038, #3044). The single-slot lane makes it single-threaded.
    decoded_cache = DecodedArtifactCache(max_bytes=WORKER_DECODED_CACHE_BYTES)
    state = {"identity": identity, "admission_sequence": 0, "decoded_cache": decoded_cache}

    def ask_admission(path: Path) -> bytes:
        payload = path.read_bytes()
        import hashlib

        state["admission_sequence"] += 1
        admission_id = int(state["admission_sequence"])
        done = threading.Event()
        pending_admissions[admission_id] = done
        out.write(
            {
                "type": "admission_request",
                "id": admission_id,
                "path": str(path),
                "digest": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
        if not done.wait(timeout=60.0) or admission_id not in admission_answers:
            from app.data_lake.admission import LakeAdmissionError

            raise LakeAdmissionError(f"{path}: no admission verdict from the coordinator in time")
        answer = admission_answers.pop(admission_id)
        if answer.get("unavailable"):
            from app.data_lake.catalog_client import CatalogUnavailableError

            raise CatalogUnavailableError(f"{path}: the coordinator cannot verify committed lake receipts")
        if not answer.get("admitted"):
            from app.data_lake.admission import LakeAdmissionError

            raise LakeAdmissionError(f"{path}: no committed catalog receipt for these bytes; retry capture")
        return payload

    from concurrent.futures import ThreadPoolExecutor

    heartbeat = threading.Thread(target=_heartbeat, args=(out, cancel_flag), daemon=True)
    heartbeat.start()
    # One slot: the lane stays serial, and the stdin loop stays free to
    # answer admission requests and cancellation while a task runs (#3044).
    lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-lane")

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            logger.warning("the coordinator sent an unparseable line: %.120s", line)
            continue
        kind = message.get("type")
        if kind == "admission":
            admission_id = int(message["id"])
            admission_answers[admission_id] = message
            event = pending_admissions.pop(admission_id, None)
            if event is not None:
                event.set()
        elif kind == "cancel":
            cancel_flag.set()
        elif kind == "task":
            lane.submit(_run_one_task, message["task"], out, ask_admission, cancel_flag, state)
    lane.shutdown(wait=True)
    return 0


def _child_identity() -> dict[str, Any]:
    from app.research.sweep.identity import resolve_code_identity

    identity = resolve_code_identity()
    return {
        "source_digest": identity.source_digest,
        "environment_digest": identity.environment_digest,
        "digest_scheme": identity.digest_scheme,
    }


def _heartbeat(out: _LockedStdout, cancel_flag: threading.Event) -> None:
    """Liveness only — resets the parent's silence clock, renews no lease (#3041)."""
    while not cancel_flag.wait(ALIVE_INTERVAL_S):
        out.write({"type": "alive", "at_ms": now_ms_utc()})


def _run_one_task(
    task: Mapping[str, Any],
    out: _LockedStdout,
    ask_admission: Callable[[Path], bytes],
    cancel_flag: threading.Event,
    state: dict[str, Any],
) -> None:
    from app.research.golden_search.protocol import ExecutionAssumptions

    def checkpoint(phase: str) -> None:
        out.write(
            {
                "type": "checkpoint",
                "phase": phase,
                "cursor_kind": "processed_blocks",
                "cursor": 0,
                "occurred_at_ms": now_ms_utc(),
                "transition": True,
            }
        )

    completed_before = int(state.get("completed", 0))
    try:
        request, strategy_key = request_of_wire(task)
        if state["identity"] != _expected_identity(task):
            out.write(
                {"type": "identity_refused", "key": task["key"], "reason": "sealed identity differs from the task's"}
            )
            return
        checkpoint("preparation")

        from app.research.golden_search.evaluator import engine_request
        from app.services.engine_backtest_service import execute_engine_backtest

        execution = ExecutionAssumptions(**task["execution"])
        manifest = dict(task["manifest"])

        def raise_if_cancelled() -> None:
            from app.jobs.progress import JobCancelled

            if cancel_flag.is_set():
                raise JobCancelled("cancelled by the coordinator")

        response = execute_engine_backtest(
            request=engine_request(request, strategy_key=strategy_key, execution=execution),
            on_phase=lambda phase: checkpoint(phase),
            on_log=lambda message: None,
            data_manifest=manifest,
            while_waiting=raise_if_cancelled,
            decoded_cache=state["decoded_cache"],
            admission=ask_admission,
        )
        result = project_response(request, response, strategy_key=strategy_key, initial_cash=execution.initial_cash)
        state["completed"] = completed_before + 1
        out.write(
            {
                "type": "checkpoint",
                "phase": "search",
                "cursor_kind": "durable_completions",
                "cursor": state["completed"],
                "occurred_at_ms": now_ms_utc(),
                "transition": True,
            }
        )
        out.write(
            {
                "type": "result",
                "key": task["key"],
                "metrics": result.metrics.as_dict(),
                "detail": result.detail,
                "curve": [[ms, value] for ms, value in (result.curve or [])] if result.curve is not None else None,
            }
        )
    except BaseException as exc:  # the child's terminal sink: every failure is a delivered answer
        from app.jobs.progress import JobCancelled

        if isinstance(exc, JobCancelled):
            out.write({"type": "failed", "key": task.get("key", "?"), "error": "cancelled"})
            cancel_flag.set()
            return
        out.write({"type": "failed", "key": str(task.get("key", "?")), "error": f"{type(exc).__name__}: {exc}"})


def _expected_identity(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_digest": str(task["identity"]["source_digest"]),
        "environment_digest": str(task["identity"]["environment_digest"]),
        "digest_scheme": task["identity"]["digest_scheme"],
    }


class _LockedStdout:
    """One writer for the whole child: JSON lines, serialized, flushed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def write(self, message: Mapping[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":"), allow_nan=False)
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()


# Parity with the serial baseline's cache bound (app.research.benchmark's
# DECODED_CACHE_DEFAULT_BYTES): one worker holds what one serial process held.
WORKER_DECODED_CACHE_BYTES = 2 * 1024**3


if __name__ == "__main__":
    raise SystemExit(_child_main())


# ── The parent's side: one owned child, one task lane ─────────────────────


@dataclass
class WorkerDispatchContext:
    """What a stage knows and a spawned worker must be told (#3044)."""

    strategy_key: str
    execution: Mapping[str, Any]
    manifest: Mapping[str, str]
    identity: Mapping[str, Any]
    job_id: str
    generation: int
    on_checkpoint: Callable[[Mapping[str, Any]], None] = lambda checkpoint: None
    # The stage's own cancellation check (#3044): polled between tasks, the
    # seam's shape — a cancelled batch stops dispatching and delivers the
    # cancellation, never persisted failures.
    cancel_check: Callable[[], object] = lambda: None


class ResearchWorker:
    """A spawned child owned from creation through exit (#3044).

    One child, one task lane: tasks are submitted one at a time and their
    outcomes awaited before the next, exactly as the serial adapter runs —
    the study's logical order never sees the process boundary. The pump
    thread drains the child's messages forever, so a blocked dispatch never
    blocks the child's writes; admission verdicts run on the coordinator's
    own bounded I/O path; checkpoints feed the job's lease gate. Alives
    refresh only the process clock; task progress refreshes both, so a
    heartbeating-but-wedged worker is fenced by the task bound, not trusted
    forever.
    """

    def __init__(self, context: WorkerDispatchContext) -> None:
        self._context = context
        self._lock = threading.Lock()
        self._outcomes: dict[str, dict[str, Any]] = {}
        self._arrived = threading.Condition(self._lock)
        self._alive_signal = threading.Event()
        self._progress_signal = threading.Event()
        self._child: subprocess.Popen | None = None
        self._lost: str | None = None
        self._pump: threading.Thread | None = None
        from concurrent.futures import ThreadPoolExecutor

        self._verdicts = ThreadPoolExecutor(max_workers=2, thread_name_prefix="admission-verdicts")
        self._spawn()

    # -- lifecycle ---------------------------------------------------------

    def _spawn(self) -> None:
        env = {**os.environ, **CHILD_ENV_FIXED}
        self._child = subprocess.Popen(
            [sys.executable, "-m", "app.research.golden_search.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # the child's structured logs flow to the container's stderr
            env=env,
            text=True,
            bufsize=1,
        )
        self._pump = threading.Thread(
            target=self._pump_messages, name=f"research-worker-{self._child.pid}", daemon=True
        )
        self._pump.start()
        ready = self._await(lambda: "ready" in self._outcomes or self._lost is not None, timeout_s=STARTUP_BOUND_S)
        if self._lost is not None:
            raise WorkerLost(f"the research worker failed to start: {self._lost}")
        if not ready:
            self.close()
            raise WorkerLost(f"the research worker did not report readiness within {STARTUP_BOUND_S:.0f}s")

    def _pump_messages(self) -> None:
        child = self._child
        assert child is not None and child.stdout is not None
        try:
            for line in child.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    logger.warning("research worker sent an unparseable line: %.120s", line)
                    continue
                kind = message.get("type")
                if kind in ("result", "failed"):
                    self._progress_signal.set()
                    with self._arrived:
                        self._outcomes[message["key"]] = message
                        self._arrived.notify_all()
                elif kind == "ready":
                    with self._arrived:
                        self._outcomes["ready"] = message
                        self._arrived.notify_all()
                elif kind == "admission_request":
                    # A read in flight is work in flight: it counts toward
                    # the task clock. The verdict itself runs off the pump
                    # thread — a slow catalog verdict must not stop alives
                    # and checkpoints from being drained (#3044).
                    self._progress_signal.set()
                    self._verdicts.submit(self._answer_admission, message)
                elif kind == "checkpoint":
                    self._progress_signal.set()
                    self._context.on_checkpoint(message)
                elif kind == "alive":
                    self._alive_signal.set()
                elif kind == "identity_refused":
                    self._progress_signal.set()
                    with self._arrived:
                        self._lost = message.get("reason", "identity refused")
                        self._arrived.notify_all()
                    logger.error("research worker refused its sealed identity: %s", self._lost)
        except Exception as exc:  # the pump dying is worker loss, never a silent hang
            with self._arrived:
                self._lost = f"pump failed: {exc}"
                self._arrived.notify_all()
            logger.exception("research worker message pump failed")
        else:
            # EOF: the child's stdout closed — it died or never started.
            with self._arrived:
                self._lost = self._lost or f"the research worker closed its output (exit code {child.poll()})"
                self._arrived.notify_all()

    def _answer_admission(self, message: Mapping[str, Any]) -> None:
        """One read's verdict: the parent runs the canonical committed-receipt check."""
        from app.data_lake.admission import LakeAdmissionError, verify_committed_digest
        from app.data_lake.catalog_client import CatalogUnavailableError
        from app.data_lake.path_policy import lake_root_for

        verdict: dict[str, Any] = {"type": "admission", "id": message["id"]}
        path = Path(str(message["path"]))
        try:
            root = lake_root_for(path) or lake_root_for(path.resolve())
            if root is None:
                verdict["admitted"] = True  # a reference fixture keeps its file-only contract
            else:
                verify_committed_digest(path, root, str(message["digest"]), int(message["size"]))
                verdict["admitted"] = True
        except CatalogUnavailableError:
            verdict["unavailable"] = True
        except LakeAdmissionError:
            verdict["admitted"] = False
        self._send(verdict)

    def _send(self, message: Mapping[str, Any]) -> None:
        with self._lock:
            child = self._child
            if child is None or child.poll() is not None or child.stdin is None:
                return
            try:
                child.stdin.write(json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n")
                child.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # a dead child is the pump's loss to report

    # -- the task lane ------------------------------------------------------

    def run_task(
        self,
        task: Mapping[str, Any],
        *,
        silence_bound_s: float = SILENCE_BOUND_S,
        task_silence_bound_s: float = TASK_SILENCE_BOUND_S,
    ) -> dict[str, Any]:
        """Submit one task and return its delivered outcome message.

        Waits bounded by real signals — outcomes, alives and task progress —
        so a dead child is fenced by the process bound and a heartbeating
        but wedged one by the task bound, never trusted forever.
        """
        key = str(task["key"])
        with self._arrived:
            if self._lost is not None:
                raise WorkerLost(f"the research worker is lost: {self._lost}")
            self._outcomes.pop(key, None)
        self._send({"type": "task", "task": task})
        last_alive = time.monotonic()
        last_progress = last_alive
        while True:
            with self._arrived:
                if key in self._outcomes:
                    return self._outcomes.pop(key)
                if self._lost is not None:
                    raise WorkerLost(f"the research worker is lost: {self._lost}")
                self._alive_signal.clear()
                self._progress_signal.clear()
                self._arrived.wait(timeout=0.5)
                alive_signalled = self._alive_signal.is_set()
                progress_signalled = self._progress_signal.is_set()
            now = time.monotonic()
            if progress_signalled:
                # Work in flight proves the process too; heartbeats alone
                # prove nothing about the lane — they refresh the process
                # clock only, never the task clock below.
                last_alive = last_progress = now
            elif alive_signalled:
                last_alive = now
            else:
                if self._child is not None and self._child.poll() is not None:
                    raise WorkerLost(f"the research worker exited with code {self._child.returncode}")
            if now - last_alive > silence_bound_s:
                self.close()
                raise WorkerLost(
                    f"the research worker went silent for {silence_bound_s:.0f}s with a task in flight; fenced and killed"
                )
            if now - last_progress > task_silence_bound_s:
                self.close()
                raise WorkerLost(
                    f"the research worker made no task progress for {task_silence_bound_s:.0f}s "
                    "while its heartbeats continued; fenced and killed"
                )

    def cancel(self) -> None:
        """Stop: the child observes the flag between reads; the slot is free once reaped."""
        self._send({"type": "cancel"})
        if self._child is not None and self._child.poll() is None:
            time.sleep(1.0)
        self.close()

    def close(self) -> None:
        """Kill and reap, whatever the child is doing; idempotent."""
        with self._lock:
            child, self._child = self._child, None
        if child is None:
            return
        # A child already dead refuses kill and close; both are fine here —
        # the reap below reports the one failure that matters (still alive).
        with contextlib.suppress(OSError):
            child.kill()
        try:
            child.wait(timeout=KILLED_CHILD_WAIT_S)
        except subprocess.TimeoutExpired:
            logger.error("research worker pid %s did not die within %.1fs after kill", child.pid, KILLED_CHILD_WAIT_S)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        self._verdicts.shutdown(wait=False)

    def _await(self, predicate: Callable[[], bool], *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._arrived:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._arrived.wait(timeout=min(remaining, 0.5))
        return True


class WorkerDispatch:
    """The :data:`BatchDispatch` a stage's evaluator runs through (#3044).

    Serial in effect — one worker, one task lane, logical order preserved —
    with the parent's ``run`` callable deliberately unused: the coordinator
    never executes and never enters the engine gate. Child failures arrive
    as failed metrics (evidence, like any engine failure); a lost worker
    interrupts the stage with the affected reservations left pending for
    Finish.
    """

    def __init__(self, worker: ResearchWorker, context: WorkerDispatchContext) -> None:
        self._worker = worker
        self._context = context

    def __call__(self, run, batch):  # BatchDispatch signature; run unused by design
        from app.jobs.progress import JobCancelled
        from app.research.golden_search.selection import Metrics

        outcomes = []
        for item in batch:
            cancelled = self._context.cancel_check()
            if cancelled is not None or (type(cancelled) is bool and cancelled):
                # Stop dispatching, tell the child, deliver the cancellation —
                # the reservations release and nothing persists (#3040's contract).
                self._worker.cancel()
                raise JobCancelled("cancelled between research worker tasks")
            task = task_wire(
                item,
                strategy_key=self._context.strategy_key,
                execution=self._context.execution,
                manifest=self._context.manifest,
                identity=self._context.identity,
            )
            try:
                message = self._worker.run_task(task)
            except WorkerLost:
                raise
            if message["type"] == "failed" and message.get("error") == "cancelled":
                from app.jobs.progress import JobCancelled

                outcomes.append(
                    DeliveredOutcome(
                        key=item.key,
                        result=EvaluationResult(metrics=Metrics.failed("cancelled")),
                        cancelled_by=JobCancelled("cancelled in the research worker"),
                    )
                )
            elif message["type"] == "failed":
                outcomes.append(
                    DeliveredOutcome(
                        key=item.key,
                        result=EvaluationResult(metrics=Metrics.failed(str(message.get("error", "worker failure")))),
                    )
                )
            else:
                outcomes.append(
                    DeliveredOutcome(
                        key=item.key,
                        result=EvaluationResult(
                            metrics=Metrics.from_dict(message["metrics"]),
                            detail=message.get("detail"),
                            curve=[(int(ms), float(value)) for ms, value in message["curve"]]
                            if message.get("curve")
                            else None,
                        ),
                    )
                )
        return outcomes

    def close(self) -> None:
        self._worker.close()


def worker_dispatch_factory(
    *, on_checkpoint: Callable[[Mapping[str, Any]], None], cancel_check: Callable[[], object] = lambda: None
) -> Callable[[WorkerDispatchContext], WorkerDispatch]:
    """The factory a stage run hands its context to (#3044).

    ``execute_stage`` knows the row's strategy, execution assumptions,
    manifest and identity; the runtime router knows only that execution
    belongs to a spawned worker. This closes the gap: the stage calls the
    factory with what it knows, and gets a dispatch bound to one owned
    child for the stage's life.
    """

    def build(context: WorkerDispatchContext) -> WorkerDispatch:
        context.cancel_check = cancel_check
        return WorkerDispatch(ResearchWorker(context), context)

    return build
