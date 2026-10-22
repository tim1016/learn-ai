"""Standalone read-only feed process, deliberately separate from the Clerk app."""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from app.marketdata.shared.hub import FeedHub
from app.marketdata.shared.protocol import FeedUnavailable, Frame, SourceHealth, Subscription
from app.utils.timestamps import now_ms_utc


def create_app(hub: FeedHub, token: str, source_health: Callable[[], SourceHealth] | None = None) -> FastAPI:
    """The only operations are health and read-only subscriptions."""
    if not token:
        raise ValueError("IBKR_SHARED_FEED_TOKEN must be set")
    app = FastAPI()

    async def authorize(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=403, detail="Shared feed authentication refused")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/source-health", response_model=SourceHealth, dependencies=[Depends(authorize)])
    async def source_status() -> SourceHealth:
        if source_health is None:
            raise HTTPException(status_code=503, detail="Source health is not installed")
        return source_health()

    @app.post("/stream", dependencies=[Depends(authorize)])
    async def stream(subscription: Subscription) -> StreamingResponse:
        async def encode() -> AsyncIterator[str]:
            try:
                async with aclosing(hub.stream(subscription)) as frames:
                    async for frame in frames:
                        yield frame.model_dump_json() + "\n"
            except FeedUnavailable as exc:
                yield (
                    Frame(kind="error", epoch="", received_at_ms=now_ms_utc(), reason=str(exc)).model_dump_json() + "\n"
                )

        return StreamingResponse(encode(), media_type="application/x-ndjson")

    return app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.broker.ibkr.auto_reconnect_monitor import AutoReconnectMonitor
    from app.broker.ibkr.client import IbkrClient
    from app.marketdata.shared.native import IbkrSource

    token = os.environ.get("IBKR_SHARED_FEED_TOKEN", "")
    client = IbkrClient()
    source = IbkrSource(client)
    hub = FeedHub(source)
    # Validate auth before opening the read-only broker connection.
    routes = create_app(hub, token, source.health)
    app.router.routes.extend(routes.router.routes)
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
