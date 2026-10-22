"""Standalone read-only feed process, deliberately separate from the Clerk app."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from app.broker.ibkr.live.hub import FeedHub
from app.broker.ibkr.live.protocol import FeedUnavailable, Frame, SourceHealth, Subscription
from app.utils.timestamps import now_ms_utc


def create_router(hub: FeedHub, token: str, source_health: Callable[[], SourceHealth]) -> APIRouter:
    """The only operations are health and read-only subscriptions."""
    if not token:
        raise ValueError("IBKR_SHARED_FEED_TOKEN must be set")
    router = APIRouter()

    async def authorize(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=403, detail="Shared feed authentication refused")

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/source-health", response_model=SourceHealth, dependencies=[Depends(authorize)])
    async def source_status() -> SourceHealth:
        return source_health()

    @router.post("/stream", dependencies=[Depends(authorize)])
    async def stream(subscription: Subscription) -> StreamingResponse:
        async def encode() -> AsyncIterator[str]:
            try:
                async with aclosing(hub.stream(subscription)) as frames:
                    async for frame in frames:
                        yield frame.model_dump_json() + "\n"
            except FeedUnavailable as exc:
                yield (
                    Frame(
                        kind="error", epoch=None, received_at_ms=now_ms_utc(), reason=str(exc), cause=exc.cause
                    ).model_dump_json()
                    + "\n"
                )

        return StreamingResponse(encode(), media_type="application/x-ndjson")

    return router


def create_app(hub: FeedHub, token: str, source_health: Callable[[], SourceHealth]) -> FastAPI:
    app = FastAPI()
    app.include_router(create_router(hub, token, source_health))
    return app


def production_app() -> FastAPI:
    from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
    from app.broker.ibkr.client import IbkrClient
    from app.broker.ibkr.live.native import IbkrSource

    token = os.environ.get("IBKR_SHARED_FEED_TOKEN", "")
    client = IbkrClient()
    source = IbkrSource(client)
    hub = FeedHub(source)
    # Route/auth construction validates configuration before opening a socket.
    router = create_router(hub, token, source.health)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client.set_desired_connected(True)
        monitor = AutoReconnectMonitor(client)
        monitor.start()
        try:
            yield
        finally:
            await hub.close()
            await monitor.stop()
            await client.disconnect()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app
