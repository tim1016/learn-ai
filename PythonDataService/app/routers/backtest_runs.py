"""Python-owned backtest-run reads and verbs (PRD #1929).

``GET /`` serves the run-history table (newest first, optionally one
engine's), ``GET /{id}`` the run report with its newest five hundred trades
and parity verdicts, ``PATCH /{id}/notes`` the researcher's notes, and
``DELETE /{id}`` a hard delete that refuses while a live Recency Chart run
still points at the run. These replace the .NET ``backtestRuns`` /
``backtestRun`` GraphQL queries, the ``updateBacktestRunNotes`` mutation and
the ``/api/studies`` REST surface; the Relay connection is not reproduced
because the history table requests one fixed page and never pages.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.research.backtest_runs import repository as repo
from app.research.persistence.db import with_connection
from app.schemas.backtest_runs import (
    BacktestRunDetailResponse,
    BacktestRunNotesRequest,
    BacktestRunNotesResponse,
    BacktestRunSummaryResponse,
    Engine,
)

router = APIRouter()

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 500


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
    return [BacktestRunSummaryResponse.from_repository(row) for row in rows]


@router.get("/{run_id}", response_model=BacktestRunDetailResponse)
async def get_backtest_run(run_id: int) -> BacktestRunDetailResponse:
    """One run with its report evidence; the trade list says when it is truncated."""
    run = await with_connection(repo.get_run, run_id)
    if run is None:
        raise _not_found(run_id)
    return BacktestRunDetailResponse.from_repository(run)


@router.patch("/{run_id}/notes", response_model=BacktestRunNotesResponse)
async def update_backtest_run_notes(run_id: int, body: BacktestRunNotesRequest) -> BacktestRunNotesResponse:
    if not await with_connection(repo.update_notes, run_id, body.notes):
        raise _not_found(run_id)
    return BacktestRunNotesResponse(id=run_id, notes=body.notes)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_backtest_run(run_id: int) -> Response:
    """Hard-delete a run; a run backing a live Recency Chart run must go through Recency soft-delete."""
    outcome = await with_connection(repo.delete_run, run_id)
    if outcome == "not_found":
        raise _not_found(run_id)
    if outcome == "recency_member":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RECENCY_MEMBER",
                "message": (
                    f"Backtest run {run_id} is a live Recency Chart run member; "
                    "soft-delete the Recency run instead of deleting the run directly."
                ),
            },
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
