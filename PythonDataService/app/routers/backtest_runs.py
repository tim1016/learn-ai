"""Python-owned backtest-run reads and verbs (PRD #1929).

``GET /`` serves the run-history table (newest first, optionally one
engine's), ``GET /{id}`` the run report with its newest five hundred trades
and parity verdicts, ``GET /{id}/strategy-view`` the run's own decision
candles (#2639 D13), and ``PATCH /{id}/notes`` the researcher's notes.
These replace the .NET ``backtestRuns`` /
``backtestRun`` GraphQL queries, the ``updateBacktestRunNotes`` mutation and
the ``/api/studies`` REST surface; the Relay connection is not reproduced
because the history table requests one fixed page and never pages.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Query, status

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.repository import Engine, RunDetail
from app.research.persistence.db import with_connection
from app.schemas.backtest_runs import (
    BacktestRunDetailResponse,
    BacktestRunNotesRequest,
    BacktestRunNotesResponse,
    BacktestRunSummaryResponse,
    BacktestRunViewRefusalBody,
)
from app.schemas.strategy_view import StrategyViewResponse
from app.services.backtest_run_strategy_view import build_backtest_run_strategy_view
from app.services.engine_backtest_service import SavedRunNotReplayable

router = APIRouter()

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 500

# A strategy view replays a whole backtest: one replay per run at a time, so a
# read abandoned mid-replay and asked again joins it rather than queueing
# another, and the last few views kept, keyed by what the replay was checked
# against.
_VIEW_CACHE_SIZE = 8
_view_replays: dict[tuple[object, ...], asyncio.Future[StrategyViewResponse]] = {}
_view_cache: OrderedDict[tuple[object, ...], StrategyViewResponse] = OrderedDict()


def _not_found(run_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "BACKTEST_RUN_NOT_FOUND", "message": f"Backtest run {run_id} not found"},
    )


@router.get("", response_model=list[BacktestRunSummaryResponse])
async def list_backtest_runs(
    engine: Engine | None = Query(None),
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
) -> list[BacktestRunSummaryResponse]:
    """Run history, newest first."""
    rows = await with_connection(repo.list_runs, engine=engine, limit=limit)
    return [BacktestRunSummaryResponse.model_validate(row) for row in rows]


@router.get("/{run_id}", response_model=BacktestRunDetailResponse)
async def get_backtest_run(run_id: int) -> BacktestRunDetailResponse:
    """One run with its report evidence; the trade list says when it is truncated."""
    run = await with_connection(repo.get_run, run_id)
    if run is None:
        raise _not_found(run_id)
    return BacktestRunDetailResponse.model_validate(run)


@router.get(
    "/{run_id}/strategy-view",
    response_model=StrategyViewResponse,
    responses={status.HTTP_409_CONFLICT: {"model": BacktestRunViewRefusalBody}},
)
async def get_backtest_run_strategy_view(run_id: int) -> StrategyViewResponse:
    """The run's strategy view, replayed from its own record; 409 says why when it cannot be replayed exactly."""
    # Every trade, not the report's newest: the replay is held to each one.
    run = await with_connection(repo.get_run, run_id, trade_limit=None)
    if run is None:
        raise _not_found(run_id)
    try:
        return await _replayed_view(run)
    except SavedRunNotReplayable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STRATEGY_VIEW_NOT_REPLAYABLE", "message": str(exc)},
        ) from exc


@router.patch("/{run_id}/notes", response_model=BacktestRunNotesResponse)
async def update_backtest_run_notes(run_id: int, body: BacktestRunNotesRequest) -> BacktestRunNotesResponse:
    if not await with_connection(repo.update_notes, run_id, body.notes):
        raise _not_found(run_id)
    return BacktestRunNotesResponse(id=run_id, notes=body.notes)


async def _replayed_view(run: RunDetail) -> StrategyViewResponse:
    """The run's view: kept, joined while it replays, or replayed once in a worker thread."""
    key = (run.id, run.program_version, run.evidence_provenance_json)
    if (kept := _view_cache.get(key)) is not None:
        _view_cache.move_to_end(key)
        return kept
    replay = _view_replays.get(key)
    if replay is None:
        replay = asyncio.ensure_future(asyncio.to_thread(build_backtest_run_strategy_view, run))
        _view_replays[key] = replay
        replay.add_done_callback(lambda done: _settle_replay(key, done))
    # Shielded: a reader that goes away leaves the replay for the next one to join.
    return await asyncio.shield(replay)


def _settle_replay(key: tuple[object, ...], done: asyncio.Future[StrategyViewResponse]) -> None:
    _view_replays.pop(key, None)
    if done.cancelled() or done.exception() is not None:
        return
    _view_cache[key] = done.result()
    while len(_view_cache) > _VIEW_CACHE_SIZE:
        _view_cache.popitem(last=False)
