"""HTTP transport for deterministic offline-hours bot replay."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.offline_replay import (
    OfflineReplayCatalogResponse,
    OfflineReplayCommandRequest,
    OfflineReplayCreateRequest,
    OfflineReplaySessionListResponse,
    OfflineReplaySessionResponse,
)
from app.services.offline_replay_service import (
    OfflineReplayConflictError,
    OfflineReplayNotFoundError,
    OfflineReplayService,
    get_offline_replay_service,
)

router = APIRouter()
ReplayService = Annotated[OfflineReplayService, Depends(get_offline_replay_service)]


@router.get(
    "/catalog",
    response_model=OfflineReplayCatalogResponse,
    summary="List recent completed NYSE sessions available for replay",
)
async def catalog(service: ReplayService) -> OfflineReplayCatalogResponse:
    return await service.catalog()


@router.get(
    "/sessions",
    response_model=OfflineReplaySessionListResponse,
    summary="List active and archived offline replay sessions",
)
async def list_sessions(service: ReplayService) -> OfflineReplaySessionListResponse:
    return service.list_sessions()


@router.post(
    "/sessions",
    response_model=OfflineReplaySessionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Launch synchronized one-bot-per-symbol offline replay",
)
async def create_session(
    request: OfflineReplayCreateRequest,
    service: ReplayService,
) -> OfflineReplaySessionResponse:
    try:
        return await service.create_session(request)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get(
    "/sessions/{session_id}",
    response_model=OfflineReplaySessionResponse,
    summary="Read the latest durable replay projection",
)
async def get_session(
    session_id: str,
    service: ReplayService,
) -> OfflineReplaySessionResponse:
    try:
        return service.get_session(session_id)
    except OfflineReplayNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="offline replay session was not found",
        ) from exc


@router.post(
    "/sessions/{session_id}/commands",
    response_model=OfflineReplaySessionResponse,
    summary="Pause, resume, step, change speed, or stop a replay",
)
async def command(
    session_id: str,
    request: OfflineReplayCommandRequest,
    service: ReplayService,
) -> OfflineReplaySessionResponse:
    try:
        return await service.command(session_id, request)
    except OfflineReplayNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="offline replay session was not found",
        ) from exc
    except OfflineReplayConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
