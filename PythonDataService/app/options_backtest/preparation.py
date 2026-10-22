"""API acquisition as an ordinary internal job, with verified durable freeze progress."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.config import active_root_id, settings
from app.data_lake.root_identity import LakeRootIdentityError, read_marker, resolve_root_context
from app.data_lake.work_progress import observe_work
from app.jobs.external_work import WorkReceipts
from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter, get_redis, lease_key
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest import preparation_binding, service
from app.options_backtest.execution_identity import backtest_execution_identity
from app.options_backtest.frozen_inputs import FrozenRunInputs, freeze_run_inputs
from app.options_backtest.input_staging import InputStaging, job_root
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.options_golden_search.protocol import point_hash
from app.research.persistence import lifecycle
from app.research.persistence.db import run_sync, with_connection
from app.research.runs.hashing import hash_payload
from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

PREPARATION_JOB_TYPE = "options_backtest_preparation"
# The verified checkpoint file is the only carrier of the inputs; the job's own
# result just lets the ordinary lifecycle reach "completed".
COMPLETED_RESULT: dict[str, Any] = {"complete": True}


class PreparationConflict(ValueError):
    """The checkpoint binds another request, root or decoder generation."""


class PreparationCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    identity: str
    inputs: FrozenRunInputs
    # Set on the complete checkpoint only: the writer's final observation event
    # follows that file, so a parent that returns on the file still gets every
    # closed span (the benchmark requires input_freeze). Earlier children's
    # spans travel on their own job.observations events.
    timings: tuple[PhaseTiming, ...] = ()
    complete: bool = False


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


def read_checkpoint(root: Path, identity: str) -> PreparationCheckpoint | None:
    try:
        checkpoint = PreparationCheckpoint.model_validate_json((root / "freeze-checkpoint.json").read_bytes())
    except FileNotFoundError:
        return None
    if checkpoint.identity != identity or checkpoint.inputs.root != str(root):
        raise PreparationConflict("the freeze checkpoint binds another request, root or decoder generation")
    return checkpoint


def verify_completed(req: OptionsBacktestJobRequest, checkpoint: PreparationCheckpoint) -> FrozenRunInputs:
    frozen = checkpoint.inputs
    if (checkpoint.identity != runtime_preparation_identity(req)
            or preparation_identity(req, source_root_id=UUID(frozen.source_root_id)) != checkpoint.identity
            or frozen.setup_digest != point_hash(req.setup) or frozen.underlying != req.setup.underlying):
        raise PreparationConflict("completed freeze checkpoint belongs to another input identity")
    anchors = [session_window_for_date(day).open_ms_utc for day in service.sessions_of_range(req.start_ms, req.end_ms)]
    if not checkpoint.complete or list(frozen.sessions) != anchors:
        raise ValueError("completed preparation does not contain the exact requested sessions")
    if frozen.root != str(job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)):
        raise ValueError("prepared input root differs from the configured shared root")
    frozen.verify_blobs()
    return frozen


class _Owner:
    def __init__(self, request: OptionsBacktestPreparationRequest, cancel: CancellationCheck) -> None:
        self.request, self.cancel = request, cancel
        self.parent_cancel = CancellationCheck(request.parent.job_id, renews_lease=False)
        self._checked = 0.0
        self.receipts: WorkReceipts

    def check(self, *, force: bool = False, allow_cancelled: bool = False) -> None:
        if not allow_cancelled:
            self.cancel.raise_if_cancelled()
            self.parent_cancel.raise_if_cancelled()
        if force or time.monotonic() - self._checked >= 0.5:
            request = self.request
            if not allow_cancelled and (not get_redis().exists(lease_key(request.job_id))
                                        or lifecycle.job_is_live(request.parent.job_id) is False):
                raise JobCancelled("the preparation worker or parent lease expired")
            run_sync(with_connection(preparation_binding.require_binding, request.run_id, request.parent.job_id,
                                     request.generation, request.identity, request.job_id))
            self._checked = time.monotonic()

    def stop(self) -> bool:
        """The acquisition code's stop hook: it raises the publisher's deferred failure (cancellation, an expired
        lease or a lost fence) so its caller sees the cause, and never answers True."""
        self.receipts.check()
        return False


def collect_preparation(request: OptionsBacktestPreparationRequest, emit: ProgressEmitter,
                        cancel: CancellationCheck) -> dict[str, Any] | None:
    """Publish a canonical terminal only after receipt drain and collector release."""
    try:
        return _collect_preparation(request, emit, cancel)
    except JobCancelled:
        raise
    except Exception as exc:
        failure = service.failure_of(exc)
        emit.failed(code=failure.code, message=failure.reason)
        return None


def _collect_preparation(request: OptionsBacktestPreparationRequest, emit: ProgressEmitter,
                        cancel: CancellationCheck) -> dict[str, Any] | None:
    """Freeze under the stable collection lock; shared jobs owns all terminal publication."""
    req = request.parent
    owner = _Owner(request, cancel)
    ledger = RunTimings()
    with InputStaging(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id) as stage:
        receipts = WorkReceipts(emit, lambda: owner.check(force=True))
        owner.receipts = receipts
        failure: BaseException | None = None
        try:
            owner.check()
            checkpoint = read_checkpoint(stage.root, request.identity)
            if checkpoint is not None and checkpoint.complete:
                verify_completed(req, checkpoint)
                return COMPLETED_RESULT
            resume = None if checkpoint is None else checkpoint.inputs
            stage.retain_verified(frozenset() if resume is None else resume.blob_digests)
            sessions = service.sessions_of_range(req.start_ms, req.end_ms)
            def observations() -> dict[str, Any]:
                return {"identity": request.identity, "phase_timings": ledger.wire()}

            def phase(name: str) -> None:
                receipts.observations(observations())
                receipts.phase(name)

            hooks = service.RunHooks(phase, receipts.progress, lambda message: None, owner.stop)

            def save(frozen: FrozenRunInputs, *, complete: bool = False) -> None:
                owner.check(force=True)
                checkpoint = PreparationCheckpoint(identity=request.identity, inputs=frozen,
                                                   timings=ledger.rows if complete else (), complete=complete)
                atomic_write_bytes(stage.root / "freeze-checkpoint.json", checkpoint.model_dump_json().encode())

            with observe_work(receipts.work):
                service.prepare_run_data(req.setup, sessions, hooks, now_ms_utc, timings=ledger)
                phase("freezing_inputs")
                with ledger.measure("input_freeze"):
                    frozen = freeze_run_inputs(req.setup, sessions, LakeRunData(req.setup.underlying), stage.root,
                        should_stop=owner.stop, reserve_bytes=stage.reserve, resume=resume,
                        on_session=lambda done, total, day: receipts.progress(done, total), on_checkpoint=save)
                save(frozen, complete=True)
                return COMPLETED_RESULT
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Background bounded I/O may finish after cancellation. Drain its
            # receipt callback before cleanup can release the reservation.
            drain_failure: BaseException | None = None
            try:
                receipts.deactivate()
            except BaseException as exc:
                drain_failure = exc
                if failure is not None:
                    logger.warning("receipt drain failed after preparation stopped", exc_info=True,
                                   extra={"job_id": request.job_id, "action": "preparation_receipt_drain_failed"})
            if ledger.rows:
                try:
                    # Cancellation still reports closed spans. A lost parent
                    # binding or generation authorizes no further publication.
                    owner.check(force=True, allow_cancelled=True)
                    emit.emit_event("job.observations", {"identity": request.identity, "phase_timings": ledger.wire()})
                except Exception:
                    logger.warning("preparation observations could not be published", exc_info=True,
                                   extra={"job_id": request.job_id, "action": "preparation_observations_failed"})
            if failure is None and drain_failure is not None:
                raise drain_failure
