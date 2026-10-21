"""One durable acquisition identity across HTTP disconnects and API restarts."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import active_root_id, settings
from app.data_lake.root_identity import LakeRootIdentityError, read_marker, resolve_root_context
from app.data_lake.work_progress import observe_work
from app.jobs.progress import CancellationCheck
from app.options_backtest import service
from app.options_backtest.execution_identity import backtest_execution_identity
from app.options_backtest.frozen_inputs import FrozenRunInputs, freeze_run_inputs
from app.options_backtest.input_staging import CollectorBusy, InputStaging, job_root
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.golden_search.proof import BlobStore
from app.research.runs.hashing import hash_payload
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

MAX_EXTERNAL_WAIT_MS = 1_860_000
RECEIPT_PUBLICATION_INTERVAL_S = 0.5


class PreparationConflict(ValueError):
    """The job id already binds another request or decoder generation."""


def preparation_identity(req: OptionsBacktestJobRequest, *, source_root_id: UUID | None = None) -> str:
    return hash_payload({"request": req.model_dump(mode="json"), "root": str(active_root_id() if source_root_id is None else source_root_id),
                         "execution": backtest_execution_identity()})


def runtime_preparation_identity(req: OptionsBacktestJobRequest) -> str:
    """Bind the runtime to the same physical root without importing provider secrets.

    The API keeps its configured UUID and validates that root normally. The
    isolated runtime reads the shared mount's canonical marker; an explicit
    runtime UUID still has to agree. Unmarked managed roots remain refused.
    """
    base = Path(settings.LEAN_DATA_WRITE_ROOT)
    marker = read_marker(base)
    if marker is None:
        raise LakeRootIdentityError("Options replay requires the shared lake's root identity marker")
    expected = active_root_id() if settings.DATA_LAKE_ROOT_ID else marker.data_root_id
    context = resolve_root_context(expected, base_root=base)
    return preparation_identity(req, source_root_id=context.root_id)


def read_preparation(root: Path, identity: str) -> dict[str, Any] | None:
    try:
        state = json.loads((root / "preparation.json").read_text())
    except FileNotFoundError:
        return None
    if state.get("identity") != identity:
        raise PreparationConflict("the preparation job binds another request or decoder generation")
    return state


class _Journal:
    def __init__(self, root: Path, identity: str, ledger: RunTimings) -> None:
        self.root, self.identity, self.ledger = root, identity, ledger
        self.state = read_preparation(root, identity) or {
            "identity": identity, "status": "collecting", "sequence": 0, "work_cursor": 0,
            "phase": "preparation", "done": 0, "total": 0, "wait": None,
        }
        self.ledger.adopt(PhaseTiming.model_validate(row) for row in self.state.get("timings", []))
        self._lock = threading.RLock()
        self._seen: set[str] = set()
        self._published = 0.0
        self.active = True

    def publish(self) -> None:
        self.state.update(sequence=self.state["sequence"] + 1, updated_at_ms=now_ms_utc(), timings=self.ledger.wire())
        atomic_write_bytes(self.root / "preparation.json", json.dumps(self.state, sort_keys=True, separators=(",", ":")).encode())
        self._published = time.monotonic()

    def phase(self, phase: str) -> None:
        with self._lock:
            self.state.update(phase=phase, done=0, total=0)
            self.publish()

    def progress(self, done: int, total: int) -> None:
        with self._lock:
            self.state.update(done=done, total=total, work_cursor=self.state["work_cursor"] + 1)
            self.publish()

    def work(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self.active or self.state["status"] != "collecting":
                return
            if event["kind"] == "work":
                key = event["work_kind"] + ":" + event["identity"]
                if key in self._seen:
                    return
                if len(self._seen) >= 1_000_000:
                    raise ValueError("preparation receipt bound exceeded")
                self._seen.add(key)
                self.state["work_cursor"] += 1
                if time.monotonic() - self._published < RECEIPT_PUBLICATION_INTERVAL_S:
                    return
            elif event["kind"] == "external_wait":
                bound = event["bound_ms"]
                if type(bound) is not int or not 0 < bound <= MAX_EXTERNAL_WAIT_MS:
                    raise ValueError("preparation external wait exceeds its acquisition bound")
                self.state["wait"] = {"id": self.state["sequence"] + 1, "deadline_ms": now_ms_utc() + bound}
            elif event["kind"] == "external_wait_end":
                self.state["wait"] = None
            else:
                raise ValueError("unknown acquisition progress receipt")
            self.publish()

    def terminal(self, status: str, **fields: Any) -> None:
        with self._lock:
            self.state.update(status=status, wait=None, **fields)
            self.publish()
            self.active = False

    def deactivate(self) -> None:
        """Drain receipt publication before releasing the collector's file lock."""
        with self._lock:
            self.active = False


def _verify_frozen(frozen: FrozenRunInputs) -> None:
    blobs = BlobStore(Path(frozen.root))
    retained = sum(len(blobs.get(digest)) for digest in frozen.sessions.values())
    if retained != frozen.retained_bytes:
        raise ValueError("completed preparation does not match its retained-byte inventory")


def ensure_preparation(req: OptionsBacktestJobRequest, identity: str) -> None:
    """Join the collector lock, or resume a crashed collector from verified files."""
    root = job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)
    ledger = RunTimings()
    journal = None
    try:
        with InputStaging(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id) as stage:
            try:
                journal = _Journal(root, identity, ledger)
                if journal.state["status"] == "complete":
                    _verify_frozen(FrozenRunInputs.model_validate(journal.state["inputs"]))
                    return
                if journal.state["status"] != "collecting":
                    return
                # Resuming the same identity changes no financial input. Completed
                # phase observations remain operational history, outside results.
                journal.state["wait"] = None
                journal.publish()
                cancelled = CancellationCheck(req.job_id, renews_lease=False)
                sessions = service.sessions_of_range(req.start_ms, req.end_ms)
                def stop() -> bool:
                    return (root / "stop-requested").exists() or cancelled.should_cancel()

                hooks = service.RunHooks(journal.phase, journal.progress, lambda message: None, stop)
                checkpoint_path = root / "freeze-checkpoint.json"
                resume = FrozenRunInputs.model_validate_json(checkpoint_path.read_text()) if checkpoint_path.exists() else None
                stage.retain_verified({} if resume is None else resume.sessions)
                with observe_work(journal.work):
                    service.prepare_run_data(req.setup, sessions, hooks, now_ms_utc, timings=ledger)
                    journal.phase("freezing_inputs")
                    with ledger.measure("input_freeze"):
                        frozen = freeze_run_inputs(req.setup, sessions, LakeRunData(req.setup.underlying), root,
                            should_stop=stop, reserve_bytes=stage.reserve, resume=resume,
                            on_session=lambda done, total, day: journal.progress(done, total),
                            on_checkpoint=lambda frozen: atomic_write_bytes(checkpoint_path, frozen.model_dump_json().encode()))
                    journal.terminal("complete", inputs=frozen.model_dump(mode="json"))
            except PreparationConflict:
                raise
            except Exception as exc:
                if journal is None:
                    raise
                if isinstance(exc, (service.RunCancelled, service.RunStopped, service.CheckStopped, service.DownloadStopped)):
                    journal.terminal("cancelled")
                else:
                    failure = service.failure_of(exc)
                    journal.terminal("failed", code=failure.code, reason=failure.reason)
            finally:
                if journal is not None:
                    journal.deactivate()

    except CollectorBusy:
        # A reconnect attaches to the existing producer; never a second fetch.
        return


def start_preparation(req: OptionsBacktestJobRequest, identity: str) -> Future[None]:
    finished: Future[None] = Future()

    def collect() -> None:
        try:
            ensure_preparation(req, identity)
        except Exception as exc:
            finished.set_exception(exc)
        else:
            finished.set_result(None)

    threading.Thread(target=collect, name=f"options-prepare-{req.job_id[:8]}", daemon=True).start()
    return finished
