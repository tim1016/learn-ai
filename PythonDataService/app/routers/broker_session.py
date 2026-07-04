"""Read-only broker session mirror endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.schemas.broker_session import (
    BrokerSessionEventPage,
    BrokerSessionMirrorSnapshot,
)
from app.services.broker_session_events import (
    BrokerSessionEventService,
    get_broker_session_event_service,
)
from app.services.broker_session_mirror import (
    BrokerSessionMirrorService,
    get_broker_session_mirror_service,
)

router = APIRouter(prefix="/api/broker/session-mirror", tags=["broker-session-mirror"])


@router.get("", response_model=BrokerSessionMirrorSnapshot)
async def broker_session_snapshot(
    service: BrokerSessionMirrorService = Depends(get_broker_session_mirror_service),
) -> BrokerSessionMirrorSnapshot:
    """Return one read-only roster snapshot."""

    return await service.snapshot()


@router.get("/events", response_model=BrokerSessionEventPage)
async def broker_session_events(
    client_id: int | None = Query(default=None, ge=0),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    service: BrokerSessionEventService = Depends(get_broker_session_event_service),
) -> BrokerSessionEventPage:
    """Return classified broker-session diagnostic events."""

    return service.events(client_id=client_id, after_seq=after_seq, limit=limit)


@router.get("/stream")
async def broker_session_stream(
    interval_ms: int = Query(default=2000, ge=500, le=60_000),
    service: BrokerSessionMirrorService = Depends(get_broker_session_mirror_service),
) -> StreamingResponse:
    """SSE stream of roster snapshots."""

    async def event_source():
        try:
            while True:
                snapshot = await service.snapshot()
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
                page = service.events(
                    client_id=client_id,
                    after_seq=last_seq,
                    limit=500,
                )
                for row in page.rows:
                    yield f"event: broker_event\ndata: {row.model_dump_json()}\n\n"
                    last_seq = row.seq
                await asyncio.sleep(poll_ms / 1000)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
