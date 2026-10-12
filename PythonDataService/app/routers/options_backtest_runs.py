"""The Options Backtest page's runs (#3030).

``router`` is Check data (``POST /check-data``), the history (``GET /runs``), one run (``GET /runs/{id}``, without its
trades) and ``DELETE /runs/{id}``. ``jobs_router`` carries ``POST /api/jobs-internal/options-backtest``, dispatched by the .NET jobs boundary with a
minted ``job_id``: it validates the request (a client mistake is a 400 and never a job), makes the run, ``running``,
and starts a worker thread that checks the data, fetches what is missing, replays and saves it
(``app/options_backtest/service.py``). Cancelling is the jobs boundary's: a flag the worker polls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Response, status

from app.data_lake.catalog_client import CatalogUnavailableError
from app.jobs.phases import friendly as friendly_phase
from app.jobs.progress import CancellationCheck, JobCancelled, ProgressEmitter
from app.jobs.runner import run_in_thread
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest import saved_runs, saved_setups, service
from app.options_backtest.data_check import check_data
from app.options_backtest.lake_check import LakeCheckData
from app.options_backtest.run import RunResult
from app.options_backtest.saved_runs import DbRunStore, DeleteOutcome, DuplicateJob, NewRun, RunFailure
from app.options_backtest.saved_setups import SavedSetup
from app.options_backtest.service import INTERNAL_ERROR, RunCancelled, RunFailedError, RunHooks, RunRefused
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.schemas.options_backtest import OptionsBacktestRefusalBody
from app.schemas.options_backtest_runs import (
    JOB_ID_MAX_LENGTH,
    JOB_ID_PATTERN,
    RUN_ID_MAX,
    OptionsBacktestCheckDataRequest,
    OptionsBacktestDataCheckResponse,
    OptionsBacktestJobAcceptedResponse,
    OptionsBacktestJobRequest,
    OptionsBacktestJobResult,
    OptionsBacktestRunDetailResponse,
    OptionsBacktestRunListItemResponse,
    exact,
)
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

router = APIRouter()
jobs_router = APIRouter()

_REFUSED = {status.HTTP_400_BAD_REQUEST: {"model": OptionsBacktestRefusalBody}}
_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": OptionsBacktestRefusalBody}}

RunId = Annotated[int, Path(ge=1, le=RUN_ID_MAX, description="The run's id, from the history.")]


def run_store() -> DbRunStore:
    """Where runs are saved. A function so a test can put another store in its place."""
    return DbRunStore()


async def saved_setup(name: str) -> SavedSetup | None:
    """The setup saved or shipped under ``name``, or ``None``. A function so a test can put another in its place."""
    return await with_connection(saved_setups.get_setup, name)


async def _name_the_run_may_carry(req: OptionsBacktestJobRequest) -> str | None:
    """The name a run is recorded under: the one the caller sent, when a saved setup or a shipped example of that name
    exists and is the setup the run was asked with, compared as validated setups and not as text. Otherwise none: a run
    sent under a stale label is still a valid run, so this is no refusal."""
    if req.setup_name is None:
        return None
    named = await saved_setup(req.setup_name)
    return req.setup_name if named is not None and named.setup == req.setup else None


def _refusal(exc: RunRefused) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"code": exc.code, "message": exc.message})


def _sessions_of(start_ms: int, end_ms: int) -> tuple[date, ...]:
    """The sessions of a request's range, refusing one that reaches a session opening before the epoch.

    Every instant on the wire and in the tables is a count of milliseconds from the epoch and never below it, and a
    session on 1969-12-31 opens at a negative one. It is a mistake of the request, refused before anything is read.
    """
    sessions = service.sessions_of_range(start_ms, end_ms)
    if session_window_for_date(sessions[0]).open_ms_utc < 0:
        raise RunRefused(
            "OPTIONS_BACKTEST_RANGE_TOO_EARLY",
            "The range reaches a trading day before 1970, which a run cannot be asked for. Start the range later.",
        )
    return sessions


# ── Check data, the history, one run, delete ─────────────────────────────


@router.post(
    "/check-data",
    response_model=OptionsBacktestDataCheckResponse,
    responses={**_REFUSED, status.HTTP_503_SERVICE_UNAVAILABLE: {"model": OptionsBacktestRefusalBody}},
)
async def check_the_data(body: OptionsBacktestCheckDataRequest) -> OptionsBacktestDataCheckResponse:
    """Which sessions of the range can be tested, from what is already stored. Nothing is fetched to answer.

    Reads files and asks the lake's catalog, so it runs in a worker thread. A range that cannot be run is a 400.
    """
    try:
        sessions = await asyncio.to_thread(_sessions_of, body.start_ms, body.end_ms)
    except RunRefused as exc:
        raise _refusal(exc) from exc
    try:
        checked = await asyncio.to_thread(
            check_data, body.setup, sessions, now_ms=now_ms_utc(), data=LakeCheckData(body.setup.underlying)
        )
    except (CatalogUnavailableError, CallerStoppedWaitingError) as exc:
        # A catalog that does not answer in time is as out of reach as one that cannot be connected to.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE",
                "message": "The data catalog could not be reached, so the data cannot be checked right now.",
            },
        ) from exc
    return OptionsBacktestDataCheckResponse.from_check(checked)


@router.get("/runs", response_model=list[OptionsBacktestRunListItemResponse])
async def list_the_runs(
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    job_id: Annotated[
        str | None,
        Query(
            max_length=JOB_ID_MAX_LENGTH,
            pattern=JOB_ID_PATTERN,
            description="The job that made the run, to find the run just started.",
        ),
    ] = None,
) -> list[OptionsBacktestRunListItemResponse]:
    """The history, newest first, each run with its headline figures. A running run whose job is gone reads ``interrupted``."""
    records = await with_connection(saved_runs.list_runs, is_live=lifecycle.job_is_live, limit=limit, job_id=job_id)
    return [OptionsBacktestRunListItemResponse.from_record(record) for record in records]


@router.get("/runs/{run_id}", response_model=OptionsBacktestRunDetailResponse, responses=_NOT_FOUND)
async def get_the_run(run_id: RunId) -> OptionsBacktestRunDetailResponse:
    """One run: its status, the setup it ran, the banner facts, and when it is complete every figure. Not its trades."""
    stored = await with_connection(saved_runs.get_run, run_id, is_live=lifecycle.job_is_live)
    if stored is None:
        raise _no_such_run(run_id)
    return OptionsBacktestRunDetailResponse.from_stored(stored)


@router.delete(
    "/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={**_NOT_FOUND, status.HTTP_409_CONFLICT: {"model": OptionsBacktestRefusalBody}},
)
async def delete_the_run(run_id: RunId) -> Response:
    """Delete a run with its sessions and trades. None by that id is a 404; a run that is still running is a 409."""
    outcome = await with_connection(saved_runs.delete_run, run_id, is_live=lifecycle.job_is_live)
    if outcome is DeleteOutcome.NOT_FOUND:
        raise _no_such_run(run_id)
    if outcome is DeleteOutcome.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "OPTIONS_BACKTEST_RUN_IS_RUNNING",
                "message": f"Run {run_id} is still running. Cancel it first, or wait for it to end.",
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _no_such_run(run_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "OPTIONS_BACKTEST_RUN_NOT_FOUND", "message": f"No run has the id {run_id}."},
    )


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
        sessions = await asyncio.to_thread(_sessions_of, req.start_ms, req.end_ms)
    except RunRefused as exc:
        raise _refusal(exc) from exc
    store = run_store()
    setup_name = await _name_the_run_may_carry(req)
    try:
        run_id = await store.create(
            NewRun(
                job_id=req.job_id,
                created_at_ms=now_ms_utc(),
                setup=req.setup,
                setup_name=setup_name,
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
    run_id: int, req: OptionsBacktestJobRequest, sessions: tuple[date, ...], store: service.RunStore
) -> Callable[[ProgressEmitter, CancellationCheck], dict[str, Any] | None]:
    """The job's work: the service behind the runner's emit-and-cancel checks, progress counted in sessions.

    A run that ends in a way the service explains is not an error of the job. A cancelled one ends the job cancelled
    (the runner logs that at info). A failed one, whose reason the service has composed and recorded, ends the job
    failed with the run's own code and reason and one info line, and no traceback: the work has then sent its own
    terminal event, so it returns ``None``. What is left for the runner's error path, with its traceback, is an
    exception nobody expected (the run's ``INTERNAL_ERROR`` among them).
    """

    def work(emit: ProgressEmitter, cancel: CancellationCheck) -> dict[str, Any] | None:
        hooks = RunHooks(
            on_phase=lambda phase: emit.phase(phase, friendly=friendly_phase("options_backtest", phase)),
            on_progress=lambda done, total: emit.progress(done, total, unit="sessions"),
            on_log=emit.log,
            should_stop=cancel.should_cancel,
        )
        try:
            result = service.execute_run(run_id=run_id, setup=req.setup, sessions=sessions, store=store, hooks=hooks)
        except RunCancelled as exc:
            raise JobCancelled(str(exc)) from exc
        except RunFailedError as exc:
            if exc.failure.code == INTERNAL_ERROR:
                raise
            logger.info(
                "options backtest run %d failed: %s",
                run_id,
                exc.failure.code,
                extra={"action": "options_backtest_run_failed", "run_id": run_id, "code": exc.failure.code},
            )
            emit.failed(code=exc.failure.code, message=exc.failure.reason)
            return None
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
