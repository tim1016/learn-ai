"""Read-only broker session mirror endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.schemas.broker_session import BrokerSessionMirrorSnapshot
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
