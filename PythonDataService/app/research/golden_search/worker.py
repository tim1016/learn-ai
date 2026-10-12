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
    IdentityRefusalError,
    ReadyEvaluation,
    WorkerLost,
    project_response,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# WorkerLost is re-exported for the tests that spawn children directly: #3045
# moved the type to the evaluator, whose retry loop catches it, but its
# historical home here is what those imports name.

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
# which is #3045's follow-up work and will tighten this to seconds.
TASK_SILENCE_BOUND_S = 30 * 60.0
ALIVE_INTERVAL_S = 5.0
KILLED_CHILD_WAIT_S = 2.0
# The authority poll while a task runs (#3065 review): between the adapter's
# own polls nothing watched the coordinator connection, so a fenced
# generation's child checkpoints kept renewing its lease until the task
# returned. Polled at this cadence from run_task's bounded wait.
FENCE_POLL_INTERVAL_S = 2.0

# The environment every child is born with: deterministic hashing and
# single-threaded native math, set before interpreter startup because a
# worker initializer would be too late for both (PRD #3034).
CHILD_ENV_FIXED = {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


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
        "task_gen": item.task_gen,
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
    # The parent sizes it — the pool divides the shared budget across its
    # lanes (#3066 review); a lone child keeps the serial parity ceiling.
    decoded_cache = DecodedArtifactCache(
        max_bytes=int(os.environ.get("RESEARCH_WORKER_DECODED_CACHE_BYTES") or WORKER_DECODED_CACHE_BYTES)
    )
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


def _task_checkpoint(out: _LockedStdout, state: dict[str, Any]) -> Callable[..., None]:
    """The checkpoint writer every task kind shares: a phase transition on the wire.

    ``cursor_kind``/``cursor`` default to the evaluator's processed-blocks
    shape; a task kind with its own monotonic cursor (durable completions)
    passes them per call. Called with the state dict so a sibling runner
    (#3049) reports through the same channel the gate already trusts.
    """

    def checkpoint(phase: str, *, cursor_kind: str = "processed_blocks", cursor: int = 0) -> None:
        out.write(
            {
                "type": "checkpoint",
                "phase": phase,
                "cursor_kind": cursor_kind,
                "cursor": cursor,
                "occurred_at_ms": now_ms_utc(),
                "transition": True,
            }
        )

    return checkpoint


def _residency_fields(state: dict[str, Any]) -> dict[str, Any]:
    # The warm-affinity hint the shared pool's scheduler folds into its
    # lanes (#3048): what this child newly decoded, and what an eviction
    # removed — on the result and every failure. The cache's own
    # counters stay in the child; nothing downstream reads them off the
    # wire.
    added, evicted = state["decoded_cache"].take_residency_delta()
    return {
        "decoded": [key.__dict__ | {"estimated_bytes": estimated} for key, estimated in added],
        "evicted": [key.relative_path for key in evicted],
    }


def _run_one_task(
    task: Mapping[str, Any],
    out: _LockedStdout,
    ask_admission: Callable[[Path], bytes],
    cancel_flag: threading.Event,
    state: dict[str, Any],
) -> None:
    if task.get("kind") is not None:
        # A sibling caller's task kind rides the same child (#3049): Grid
        # Search cells carry their own wire and projection. The golden
        # evaluation stays the unmarked default, so existing wires and
        # fixtures need no migration.
        from app.research.grid_search.dispatch import run_grid_cell_task

        run_grid_cell_task(task, out=out, ask_admission=ask_admission, cancel_flag=cancel_flag, state=state)
        return

    from app.research.golden_search.protocol import ExecutionAssumptions

    checkpoint = _task_checkpoint(out, state)
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
                **_residency_fields(state),
            }
        )
    except BaseException as exc:  # the child's terminal sink: every failure is a delivered answer
        from app.jobs.progress import JobCancelled

        if isinstance(exc, JobCancelled):
            out.write({"type": "failed", "key": task.get("key", "?"), "error": "cancelled", **_residency_fields(state)})
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
# Every spawned child's decoded cache shares this budget inside ADR 0077's
# runtime envelope, leaving the rest of the 16 GiB cgroup for interpreters,
# the engine, the coordinator and accounting (#3066 review): eight children
# at the parity ceiling alone would consume the whole envelope.
WORKER_DECODED_CACHE_TOTAL_BYTES = 8 * 1024**3


def pooled_cache_bytes(workers: int) -> int:
    """One lane's decoded-cache ceiling: the shared budget split, capped at the serial parity ceiling."""
    return min(WORKER_DECODED_CACHE_BYTES, WORKER_DECODED_CACHE_TOTAL_BYTES // max(workers, 1))


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
    # cancellation, never persisted failures. run_task polls it while a task
    # runs too, so a fenced generation stops its child promptly instead of
    # renewing its lease until the task returns (#3065 review).
    cancel_check: Callable[[], object] = lambda: None
    # This child's decoded-cache ceiling in bytes: the serial parity ceiling
    # by default, the shared budget's share under a pool (#3066 review).
    decoded_cache_bytes: int = WORKER_DECODED_CACHE_BYTES


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

    # The per-task relay (#3048), as class defaults so a partially built
    # worker (tests stub past __init__) still reads its context live: a
    # shared lane points these at each item's owner before it runs; unset,
    # the spawn context's live attributes answer, exactly the pre-relay
    # behavior a late context swap relies on.
    _task_cancel_check: Callable[[], object] | None = None
    _task_on_checkpoint: Callable[[Mapping[str, Any]], None] | None = None

    def __init__(self, context: WorkerDispatchContext) -> None:
        self._context = context
        self._lock = threading.Lock()
        self._outcomes: dict[str, dict[str, Any]] = {}
        self._arrived = threading.Condition(self._lock)
        self._alive_signal = threading.Event()
        self._progress_signal = threading.Event()
        self._child: subprocess.Popen | None = None
        self._lost: str | None = None
        self._refusal: str | None = None
        self._pump: threading.Thread | None = None
        from concurrent.futures import ThreadPoolExecutor

        self._verdicts = ThreadPoolExecutor(max_workers=2, thread_name_prefix="admission-verdicts")
        self._spawn()

    def begin_task(
        self,
        *,
        cancel_check: Callable[[], object] | None = None,
        on_checkpoint: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        """Point this child's cancellation poll and checkpoint relay at one owner (#3048).

        Switched before the task line is sent. The pipe's own ordering makes
        the switch safe: every checkpoint of the previous task is drained
        before its result — the message that wakes ``run_task`` — so a late
        relay can never reach the next owner.
        """
        with self._arrived:
            self._task_cancel_check = cancel_check
            self._task_on_checkpoint = on_checkpoint

    # -- lifecycle ---------------------------------------------------------

    def _spawn(self) -> None:
        env = {
            **os.environ,
            **CHILD_ENV_FIXED,
            # The parent sizes the child's decoded cache (#3066 review): the
            # pool divides the shared budget, a lone child keeps the ceiling.
            "RESEARCH_WORKER_DECODED_CACHE_BYTES": str(self._context.decoded_cache_bytes),
        }
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
        ready = self._await(
            lambda: "ready" in self._outcomes or self._lost is not None or self._refusal is not None,
            timeout_s=STARTUP_BOUND_S,
        )
        if self._refusal is not None:
            # The child's environment or identity is wrong before any work:
            # terminal, and nothing may be left running behind the refusal.
            self.close()
            raise IdentityRefusalError(self._refusal)
        if self._lost is not None:
            # Kill and reap before raising (#3045): a replacement loop will try
            # again, and each failure must leave nothing behind.
            self.close()
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
                    relay = self._task_on_checkpoint or self._context.on_checkpoint
                    relay(message)
                elif kind == "alive":
                    self._alive_signal.set()
                elif kind == "identity_refused":
                    self._progress_signal.set()
                    with self._arrived:
                        # A refusal is terminal, not a loss (#3065 review):
                        # recorded separately so run_task raises
                        # IdentityRefusalError and nothing retries it.
                        self._refusal = str(message.get("reason", "identity refused"))
                        self._arrived.notify_all()
                    logger.error("research worker refused its sealed identity: %s", self._refusal)
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
        """One read's verdict: the canonical committed-receipt check, batched (#3047).

        Requests already pending share one set-based lookup on the admission
        loop; the tuple resolution — the marker read and path walk — stays on
        this thread, and the verdict taxonomy is exactly the single-check
        relay's: admitted, refused, or the typed outage.
        """
        from app.data_lake.admission import LakeAdmissionError, admission_batcher, admission_check_for
        from app.data_lake.catalog_client import CatalogUnavailableError
        from app.data_lake.path_policy import lake_root_for

        verdict: dict[str, Any] = {"type": "admission", "id": message["id"]}
        path = Path(str(message["path"]))
        try:
            root = lake_root_for(path) or lake_root_for(path.resolve())
            if root is None:
                verdict["admitted"] = True  # a reference fixture keeps its file-only contract
            else:
                check = admission_check_for(path, root, str(message["digest"]), int(message["size"]))
                verdict["admitted"] = admission_batcher().submit(check)
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

    def lost_reason(self) -> str | None:
        """Why this child can take no more work, or ``None`` while it is live (#3065 review).

        A dispatch asks before writing an item's start marker, so a child
        that died between tasks is replaced first and the marker never lands
        on a run nobody will dispatch.
        """
        with self._arrived:
            if self._refusal is not None:
                return self._refusal
            if self._lost is not None:
                return self._lost
        child = self._child
        if child is not None and child.poll() is not None:
            return f"the research worker exited with code {child.returncode}"
        return None

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
        but wedged one by the task bound, never trusted forever. While the
        task runs, the coordinator's cancellation poll runs too: a cancelled
        or fenced generation kills the child right here instead of renewing
        its lease until the task returns (#3065 review).
        """
        from app.jobs.progress import JobCancelled
        from app.research.persistence.ownership import GenerationFencedError

        key = str(task["key"])
        with self._arrived:
            refusal, lost = self._refusal, self._lost
            self._outcomes.pop(key, None)
        if refusal is not None:
            self.close()
            raise IdentityRefusalError(refusal)
        if lost is not None:
            raise WorkerLost(f"the research worker is lost: {lost}")
        self._send({"type": "task", "task": task})
        last_alive = time.monotonic()
        last_progress = last_alive
        last_fence_poll = last_alive
        while True:
            with self._arrived:
                if key in self._outcomes:
                    return self._outcomes.pop(key)
                refusal, lost = self._refusal, self._lost
                if refusal is None and lost is None:
                    self._alive_signal.clear()
                    self._progress_signal.clear()
                    self._arrived.wait(timeout=0.5)
                    alive_signalled = self._alive_signal.is_set()
                    progress_signalled = self._progress_signal.is_set()
            if refusal is not None:
                self.close()
                raise IdentityRefusalError(refusal)
            if lost is not None:
                raise WorkerLost(f"the research worker is lost: {lost}")
            now = time.monotonic()
            if now - last_fence_poll >= FENCE_POLL_INTERVAL_S:
                last_fence_poll = now
                try:
                    cancelled = (self._task_cancel_check or self._context.cancel_check)()
                except (JobCancelled, GenerationFencedError):
                    # The child stops here, not when its task returns (#3065
                    # review): a fenced generation's lease must stop renewing.
                    self.close()
                    raise
                if cancelled:
                    self.close()
                    raise JobCancelled("cancelled in a research worker while a task was in flight")
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


def delivered_outcome(item: ReadyEvaluation, message: Mapping[str, Any]) -> DeliveredOutcome:
    """One child answer as the evaluator consumes it — the wire contract in one place (#3044, #3046).

    The serial and pooled adapters translate identical child messages
    identically: a cancelled run delivers the cancellation, a failure is
    evidence like any engine failure, and a result carries its metrics,
    detail and curve under the item's own fencing. A sibling caller's ready
    item (#3049, Grid Search cells) owns its own wire, so it decodes its own
    outcome; only the golden evaluation is translated here.
    """
    from app.jobs.progress import JobCancelled
    from app.research.golden_search.selection import Metrics

    if not isinstance(item, ReadyEvaluation):
        return item.outcome_of(message)
    if message["type"] == "failed" and message.get("error") == "cancelled":
        return DeliveredOutcome(
            key=item.key,
            attempt=item.attempt,
            task_gen=item.task_gen,
            result=EvaluationResult(metrics=Metrics.failed("cancelled")),
            cancelled_by=JobCancelled("cancelled in a research worker"),
        )
    if message["type"] == "failed":
        return DeliveredOutcome(
            key=item.key,
            attempt=item.attempt,
            task_gen=item.task_gen,
            result=EvaluationResult(metrics=Metrics.failed(str(message.get("error", "worker failure")))),
        )
    return DeliveredOutcome(
        key=item.key,
        attempt=item.attempt,
        task_gen=item.task_gen,
        result=EvaluationResult(
            metrics=Metrics.from_dict(message["metrics"]),
            detail=message.get("detail"),
            curve=[(int(ms), float(value)) for ms, value in message["curve"]] if message.get("curve") else None,
        ),
    )


class WorkerDispatch:
    """The :data:`BatchDispatch` a stage's evaluator runs through (#3044, #3045).

    Serial in effect — one worker, one task lane, logical order preserved —
    with the parent's ``run`` callable deliberately unused: the coordinator
    never executes and never enters the engine gate. Delivery is lazy: one
    outcome is yielded after the evaluator consumed its predecessor, so each
    result is durable before the next evaluation begins (#3045). Child
    failures arrive as failed metrics (evidence, like any engine failure);
    a lost worker interrupts the batch for the evaluator's bounded
    re-admission — the dead child is killed and reaped here, before any
    replacement exists, and the next dispatch through this same adapter
    spawns a fresh one with an empty decoded cache, never a second child
    beside the first (#3045).
    """

    def __init__(self, context: WorkerDispatchContext) -> None:
        self._context = context
        self._worker: ResearchWorker | None = None

    def _live_worker(self) -> ResearchWorker:
        """The owned child, spawning one only when none is alive (#3045).

        A child that died or refused between tasks is replaced before the
        caller writes the next item's start marker, so a run nobody will
        dispatch never burns retry allowance (#3065 review).
        """
        worker = self._worker
        if worker is not None and worker.lost_reason() is not None:
            self._worker_lost(worker)
        if self._worker is None:
            self._worker = ResearchWorker(self._context)
        return self._worker

    def _worker_lost(self, worker: ResearchWorker) -> None:
        """Cleanup before replacement: kill and reap, then forget the child (#3045)."""
        worker.close()
        if self._worker is worker:
            self._worker = None

    def _cancel(self) -> None:
        """Stop the child promptly — cancel flag, then kill and reap (#3045)."""
        if self._worker is not None:
            self._worker.cancel()

    def cancel(self) -> None:
        """The evaluator's pre-reconciliation reap (#3065 review): stop the child now."""
        self._cancel()

    def __call__(self, run, batch):  # BatchDispatch signature; run unused by design
        from app.jobs.progress import JobCancelled
        from app.research.persistence.ownership import GenerationFencedError

        for item in batch:
            try:
                cancelled = self._context.cancel_check()
            except (JobCancelled, GenerationFencedError):
                # The production polls raise (#3041): the child stops on every
                # cancellation or fencing path, never only the returning one (#3045).
                self._cancel()
                raise
            if cancelled:
                # A truthy return is a cancel token; the reservations release and
                # nothing persists (#3040's contract).
                self._cancel()
                raise JobCancelled("cancelled between research worker tasks")
            task = task_wire(
                item,
                strategy_key=self._context.strategy_key,
                execution=self._context.execution,
                manifest=self._context.manifest,
                identity=self._context.identity,
            )
            worker = self._live_worker()
            item.begin()
            try:
                message = worker.run_task(task)
            except (WorkerLost, JobCancelled, GenerationFencedError, IdentityRefusalError):
                # run_task has already killed and reaped its child on the
                # terminal paths; forget it so nothing reuses the corpse.
                self._worker_lost(worker)
                raise
            outcome = delivered_outcome(item, message)
            if outcome.cancelled_by is not None:
                yield outcome
                return
            yield outcome

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.close()


def worker_dispatch_factory(
    *, on_checkpoint: Callable[[Mapping[str, Any]], None], cancel_check: Callable[[], object] = lambda: None
) -> Callable[[WorkerDispatchContext], WorkerDispatch]:
    """The factory a stage run hands its context to (#3044).

    ``execute_stage`` knows the row's strategy, execution assumptions,
    manifest and identity; the runtime router knows only that execution
    belongs to a spawned worker. This closes the gap: the stage calls the
    factory with what it knows, and gets a dispatch that owns one child at
    a time for the stage's life — replacing a lost one only after its
    cleanup (#3045).
    """

    def build(context: WorkerDispatchContext) -> WorkerDispatch:
        # The stage builds the context from what the row knows; the runtime's
        # sinks are this factory's to attach. on_checkpoint was silently
        # dropped here before #3045's SIGKILL test caught it: the child's
        # checkpoints never reached the lease gate, leaving only the stage's
        # own phase transitions to renew.
        context.cancel_check = cancel_check
        context.on_checkpoint = on_checkpoint
        return WorkerDispatch(context)

    return build
