"""The research parent owns backtest execution; data preparation stays on API (#3423)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from typing import Any

from app.config import settings
from app.jobs.checkpoints import GatedLease
from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.options_backtest import service
from app.options_backtest.dispatch import replay_frozen_run
from app.options_backtest.execution_identity import backtest_execution_identity
from app.options_backtest.input_staging import release_job_inputs
from app.options_backtest.preparation_jobs import prepare_over_api
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import RunFailure
from app.options_backtest.timings import RunTimings
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.persistence import ownership
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.timestamps import now_ms_utc


def execute_runtime_run(*, run_id: int, req: OptionsBacktestJobRequest, sessions: tuple[date, ...],
    store: service.RunStore, emit: ProgressEmitter) -> RunResult:
    """One generation-fenced parent saves once, only after the complete ladder."""
    authority = ownership.process_authority()
    if authority is None or not authority.connected:
        raise ownership.GenerationFencedError("Options Backtest requires a live research runtime authority")
    lease = GatedLease(req.job_id, task_id=f"options-backtest:{run_id}", authority=authority)
    timings = RunTimings()
    timings.start()
    try:
        lease.phase("preparation")
        frozen = asyncio.run(prepare_over_api(req, run_id=run_id, emit=emit, lease=lease, timings=timings))
        lease.fenced_cancel()
        emit.phase("waiting_for_engine")
        context = WorkerDispatchContext(strategy_key="options_backtest", execution={}, manifest={},
            identity=backtest_execution_identity(), job_id=req.job_id, generation=authority.boot_generation,
            on_checkpoint=lease.child_checkpoint, cancel_check=lease.fenced_cancel)
        result = replay_frozen_run(req.setup, sessions, frozen, context=context,
            workers=settings.OPTIONS_SEARCH_WORKER_COUNT, timings=timings,
            cold_cache=req.benchmark_cache_mode == "cold")
        lease.fenced_cancel()
        lease.phase("persistence")
        emit.phase("saving")
        with timings.measure("save"):
            store.complete(run_id, result, finished_at_ms=now_ms_utc())
        service.record_observations(store, run_id, timings)
        return result
    except (JobCancelled, service.RunCancelled):
        store.cancel(run_id, finished_at_ms=now_ms_utc())
        service.record_observations(store, run_id, timings)
        raise
    except ownership.GenerationFencedError:
        # A successor owns lifecycle recovery. A stale coordinator writes nothing.
        raise
    except CallerStoppedWaitingError as exc:
        # An uncertain save is still running on the writer loop. Never race
        # it with a failure mutation; its durable row remains authoritative.
        raise service.RunFailedError(RunFailure(service.SAVE_TIMED_OUT,
            "Saving has not been confirmed. Reopen this run to reconcile its durable status.")) from exc
    except Exception as exc:
        failure = exc.failure if isinstance(exc, service.RunFailedError) else service.failure_of(exc)
        store.fail(run_id, failure, finished_at_ms=now_ms_utc())
        service.record_observations(store, run_id, timings)
        raise service.RunFailedError(failure) from exc
    finally:
        release_job_inputs(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)


def runtime_worker(run_id: int, req: OptionsBacktestJobRequest, sessions: tuple[date, ...],
                   store: service.RunStore, project: Callable[[int, RunResult], dict[str, Any]]) -> Callable[..., Any]:
    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any] | None:
        try:
            return project(run_id, execute_runtime_run(run_id=run_id, req=req, sessions=sessions,
                                                      store=store, emit=emit))
        except service.RunFailedError as exc:
            emit.failed(code=exc.failure.code, message=exc.failure.reason)
            return None
    return work
