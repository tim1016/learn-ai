"""Python-owned Recency Chart reads and mutations (PRD #1927).

``GET /trades`` serves what the chart draws (trades overlapping the window
with a live membership), ``GET /hero`` the visible-window winners (trades
that *entered* inside the window), and the four soft-delete / restore verbs
replace the GraphQL mutations. Storage is the four tables adopted from EF
(ADR 0057); the numerical selection stays in ``app.research.recency.stats``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.research.persistence.db import with_connection
from app.research.recency import repository as repo
from app.research.recency.stats import select_window_heroes
from app.schemas.recency import (
    RecencyHeroResponse,
    RecencyHeroResponseItem,
    RecencyLaunchMutationResponse,
    RecencyRunMutationResponse,
    RecencyTradeResponse,
)
from app.utils.session_anchors import INT64_MS_MAX

router = APIRouter()


def _window(from_ms: int, to_ms: int) -> None:
    if from_ms > to_ms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from_ms must be less than or equal to to_ms")


@router.get("/trades", response_model=list[RecencyTradeResponse])
async def list_recency_trades(
    from_ms: int = Query(ge=0, le=INT64_MS_MAX),
    to_ms: int = Query(ge=0, le=INT64_MS_MAX),
    symbols: list[str] | None = Query(None),
    strategies: list[str] | None = Query(None),
) -> list[RecencyTradeResponse]:
    """Trades overlapping the window that at least one live run still vouches for."""
    _window(from_ms, to_ms)
    views = await with_connection(repo.list_trades, from_ms=from_ms, to_ms=to_ms, symbols=symbols, strategies=strategies)
    return [RecencyTradeResponse.model_validate(view) for view in views]


@router.get("/hero", response_model=RecencyHeroResponse)
async def recency_heroes(
    from_ms: int = Query(ge=0, le=INT64_MS_MAX),
    to_ms: int = Query(ge=0, le=INT64_MS_MAX),
    symbols: list[str] | None = Query(None),
    strategies: list[str] | None = Query(None),
) -> RecencyHeroResponse:
    """The highest net-PnL combination per symbol/strategy among trades that entered inside the window."""
    _window(from_ms, to_ms)
    candidates = await with_connection(repo.hero_candidates, from_ms=from_ms, to_ms=to_ms, symbols=symbols, strategies=strategies)
    selections = select_window_heroes(candidates, from_ms, to_ms)
    return RecencyHeroResponse(heroes=[RecencyHeroResponseItem.from_engine_result(selection) for selection in selections])


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code, "message": message})


@router.post("/runs/{run_id}/soft-delete", response_model=RecencyRunMutationResponse)
async def soft_delete_recency_run(run_id: int) -> RecencyRunMutationResponse:
    if not await with_connection(repo.set_run_deleted, run_id, deleted=True):
        raise _not_found("RECENCY_RUN_NOT_FOUND", f"RecencyRun {run_id} not found")
    return RecencyRunMutationResponse(recency_run_id=run_id)


@router.post("/runs/{run_id}/restore", response_model=RecencyRunMutationResponse)
async def restore_recency_run(run_id: int) -> RecencyRunMutationResponse:
    if not await with_connection(repo.set_run_deleted, run_id, deleted=False):
        raise _not_found("RECENCY_RUN_NOT_FOUND", f"RecencyRun {run_id} not found")
    return RecencyRunMutationResponse(recency_run_id=run_id)


@router.post("/launches/{launch_id}/soft-delete", response_model=RecencyLaunchMutationResponse)
async def soft_delete_recency_launch(launch_id: str) -> RecencyLaunchMutationResponse:
    if not await with_connection(repo.set_launch_deleted, launch_id, deleted=True):
        raise _not_found("RECENCY_LAUNCH_NOT_FOUND", f"RecencyLaunch {launch_id} not found")
    return RecencyLaunchMutationResponse(launch_id=launch_id)


@router.post("/launches/{launch_id}/restore", response_model=RecencyLaunchMutationResponse)
async def restore_recency_launch(launch_id: str) -> RecencyLaunchMutationResponse:
    if not await with_connection(repo.set_launch_deleted, launch_id, deleted=False):
        raise _not_found("RECENCY_LAUNCH_NOT_FOUND", f"RecencyLaunch {launch_id} not found")
    return RecencyLaunchMutationResponse(launch_id=launch_id)
