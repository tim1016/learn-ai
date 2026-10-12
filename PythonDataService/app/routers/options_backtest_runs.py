"""The Options Backtest page's runs (#3030).

``jobs_router`` carries ``POST /api/jobs-internal/options-backtest``, dispatched by the .NET jobs boundary with a
minted ``job_id``: it validates the request (a client mistake is a 400 and never a job), makes the run, ``running``,
and starts a worker thread that checks the data, fetches what is missing, replays and saves it
(``app/options_backtest/service.py``). Cancelling is the jobs boundary's: a flag the worker polls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest import service
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import DbRunStore, DuplicateJob, NewRun, RunFailure
from app.options_backtest.service import RunCancelled, RunHooks, RunRefused
from app.schemas.options_backtest import OptionsBacktestRefusalBody
from app.schemas.options_backtest_runs import (
    OptionsBacktestJobAcceptedResponse,
    OptionsBacktestJobRequest,
    OptionsBacktestJobResult,
    exact,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

router = APIRouter()
jobs_router = APIRouter()

_REFUSED = {status.HTTP_400_BAD_REQUEST: {"model": OptionsBacktestRefusalBody}}


def run_store() -> DbRunStore:
    """Where runs are saved. A function so a test can put another store in its place."""
    return DbRunStore()


def _refusal(exc: RunRefused) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": exc.message})


# ── Jobs boundary ────────────────────────────────────────────────────────


@jobs_router.post(
    "/options-backtest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OptionsBacktestJobAcceptedResponse,
    responses={**_REFUSED, status.HTTP_409_CONFLICT: {"model": OptionsBacktestRefusalBody}},
)
async def start_options_backtest_job(req: OptionsBacktestJobRequest) -> OptionsBacktestJobAcceptedResponse:
    """Accept a run: 202 once its row is made and a worker holds it. A bad range is a 400 and no job."""
    try:
        # A calendar quarter not yet read is built on first use: off the event loop.
        sessions = await asyncio.to_thread(service.sessions_of_range, req.start_ms, req.end_ms)
    except RunRefused as exc:
        raise _refusal(exc) from exc
    store = run_store()
    try:
        run_id = await store.create(
            NewRun(
                job_id=req.job_id,
                created_at_ms=now_ms_utc(),
                setup=req.setup,
                setup_name=req.setup_name,
                first_session_open_ms=session_window_for_date(sessions[0]).open_ms_utc,
                last_session_open_ms=session_window_for_date(sessions[-1]).open_ms_utc,
                sessions_requested=len(sessions),
            )
        )
    except DuplicateJob as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "OPTIONS_BACKTEST_JOB_ALREADY_HAS_A_RUN", "message": str(exc)},
        ) from exc
    try:
        run_in_thread(
            req.job_id,
            _worker(run_id, req, sessions, store),
            thread_name=f"options-backtest-{req.job_id[:8]}",
            cancel_check_every_n=1,
        )
    except Exception:
        # No worker holds the run, and nothing else would ever end it.
        failure = RunFailure("JOB_NOT_STARTED", "The run could not be started. Nothing was fetched or run.")
        await asyncio.to_thread(store.fail, run_id, failure, finished_at_ms=now_ms_utc())
        raise
    return OptionsBacktestJobAcceptedResponse(job_id=req.job_id, run_id=run_id, status="queued")


def _worker(
    run_id: int, req: OptionsBacktestJobRequest, sessions: tuple[Any, ...], store: Any
) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any]]:
    """The job's work: the service behind the runner's emit-and-cancel checks, progress counted in sessions."""

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any]:
        hooks = RunHooks(
            on_phase=emit.phase,
            on_progress=lambda done, total: emit.progress(done, total, unit="sessions"),
            on_log=emit.log,
            should_stop=cancel.should_cancel,
        )
        try:
            result = service.execute_run(run_id=run_id, setup=req.setup, sessions=sessions, store=store, hooks=hooks)
        except RunCancelled as exc:
            raise JobCancelled(str(exc)) from exc
        return _job_result(run_id, result)

    return work


def _job_result(run_id: int, result: RunResult) -> dict[str, Any]:
    summary = result.summary
    return OptionsBacktestJobResult(
        run_id=run_id,
        status="complete",
        sessions_tested=summary.sessions_tested,
        trades=summary.trades,
        net_profit_usd=exact(summary.net_profit_usd),
    ).model_dump(mode="json")
