"""Read-only broker session mirror endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.schemas.broker_session import (
    BrokerSessionEventPage,
    BrokerSessionEventPurgeRequest,
    BrokerSessionEventPurgeResult,
    BrokerSessionHistoryPage,
    BrokerSessionHistoryPurgeRequest,
    BrokerSessionHistoryPurgeResult,
    BrokerSessionMirrorSnapshot,
)
from app.services.broker_session_events import (
    BrokerSessionEventService,
    get_broker_session_event_service,
)
from app.services.broker_session_history import (
    BrokerSessionHistoryService,
    get_broker_session_history_service,
)
from app.services.broker_session_mirror import (
    BrokerSessionMirrorService,
    get_broker_session_mirror_service,
)

router = APIRouter(prefix="/api/broker/session-mirror", tags=["broker-session-mirror"])
logger = logging.getLogger(__name__)


@router.get("", response_model=BrokerSessionMirrorSnapshot)
async def broker_session_snapshot(
    service: BrokerSessionMirrorService = Depends(get_broker_session_mirror_service),
) -> BrokerSessionMirrorSnapshot:
    """Return one roster snapshot; the mirror page owns history appends."""

    return await service.snapshot(record_history=True)


@router.get("/events", response_model=BrokerSessionEventPage)
async def broker_session_events(
    client_id: int | None = Query(default=None, ge=0),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    service: BrokerSessionEventService = Depends(get_broker_session_event_service),
) -> BrokerSessionEventPage:
    """Return classified broker-session diagnostic events."""

    return service.events(client_id=client_id, after_seq=after_seq, limit=limit)


@router.post("/events/purge", response_model=BrokerSessionEventPurgeResult)
async def broker_session_events_purge(
    request: BrokerSessionEventPurgeRequest,
    service: BrokerSessionEventService = Depends(get_broker_session_event_service),
) -> BrokerSessionEventPurgeResult:
    """Purge broker-session diagnostic events only."""

    return service.purge(request)


@router.get("/history", response_model=BrokerSessionHistoryPage)
async def broker_session_history(
    limit: int = Query(default=100, ge=1, le=500),
    service: BrokerSessionHistoryService = Depends(get_broker_session_history_service),
) -> BrokerSessionHistoryPage:
    """Return recent broker-session roster snapshots."""

    return service.history(limit=limit)


@router.post("/history/purge", response_model=BrokerSessionHistoryPurgeResult)
async def broker_session_history_purge(
    request: BrokerSessionHistoryPurgeRequest,
    service: BrokerSessionHistoryService = Depends(get_broker_session_history_service),
) -> BrokerSessionHistoryPurgeResult:
    """Purge broker-session roster history diagnostics only."""

    return service.purge(request)


@router.get("/stream")
async def broker_session_stream(
    interval_ms: int = Query(default=2000, ge=500, le=60_000),
    service: BrokerSessionMirrorService = Depends(get_broker_session_mirror_service),
) -> StreamingResponse:
    """SSE stream of roster snapshots."""

    async def event_source():
        try:
            while True:
                try:
                    snapshot = await service.snapshot(record_history=True)
                except Exception:
                    logger.exception("broker session snapshot stream failed")
                    yield "event: error\ndata: {}\n\n"
                else:
                    yield f"event: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
                await asyncio.sleep(interval_ms / 1000)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/stream")
async def broker_session_events_stream(
    client_id: int | None = Query(default=None, ge=0),
    since_seq: int = Query(default=0, ge=0),
    poll_ms: int = Query(default=2000, ge=500, le=60_000),
    service: BrokerSessionEventService = Depends(get_broker_session_event_service),
) -> StreamingResponse:
    """SSE stream of classified broker-session diagnostic events."""

    async def event_source():
        last_seq = since_seq
        try:
            while True:
                try:
                    page = service.events(
                        client_id=client_id,
                        after_seq=last_seq,
                        limit=500,
                    )
                except Exception:
                    logger.exception("broker session event stream failed")
                    yield "event: error\ndata: {}\n\n"
                    await asyncio.sleep(poll_ms / 1000)
                    continue
                for row in page.rows:
                    yield f"event: broker_event\ndata: {row.model_dump_json()}\n\n"
                    last_seq = row.seq
                if page.next_seq is not None:
                    continue
                await asyncio.sleep(poll_ms / 1000)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
