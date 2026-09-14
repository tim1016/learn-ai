"""Default-off, Compose-only ASGI qualification hooks for fleet lane capacity.

These hooks do not bind a provider or expose an execution operation.  They are
registered only for an enrolled clerk in the random namespace minted by the
host qualification ceremony, and every request must carry that ceremony's
in-memory secret.
"""

from __future__ import annotations

import asyncio
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

_PREFIX = "/internal/fleet-qualification"
_NAMESPACE_PREFIX = "compose:fleetqualification"


def qualification_router(
    *, role: str, namespace: str, secret: str, broker_url: str, market_data_url: str
) -> APIRouter | None:
    """Build the non-production hook surface only for a qualifying clerk role."""
    if (
        role != "clerk_agent"
        or not namespace.startswith(_NAMESPACE_PREFIX)
        or not secret
        or not broker_url
        or not market_data_url
    ):
        return None
    router = APIRouter(prefix=_PREFIX, include_in_schema=False)

    def require_secret(value: str | None) -> None:
        if value != secret:
            raise HTTPException(status_code=404, detail="Not found")

    async def dependency(url: str, source: str) -> JSONResponse:
        """Use an inert qualification endpoint without exposing its location."""
        def read() -> int:
            request = urllib.request.Request(f"{url}/read")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=2) as response:
                return response.status

        try:
            status = await asyncio.to_thread(read)
        except (OSError, urllib.error.URLError):
            return JSONResponse(status_code=503, content={"reason": "qualification_upstream_unavailable"})
        return JSONResponse(status_code=status, content={"source": source})

    @router.get("/dependency/broker")
    async def broker_dependency(x_fleet_qualification_secret: str | None = Header(default=None)) -> JSONResponse:
        """Exercise the running clerk ASGI process's qualification-only upstream seam."""
        require_secret(x_fleet_qualification_secret)

        return await dependency(broker_url, "qualification_broker")

    @router.get("/dependency/market-data")
    async def market_dependency(x_fleet_qualification_secret: str | None = Header(default=None)) -> JSONResponse:
        """Exercise the real clerk ASGI market-data seam without provider registration."""
        require_secret(x_fleet_qualification_secret)
        return await dependency(market_data_url, "qualification_market_data")

    @router.get("/hold/request")
    async def hold_request(x_fleet_qualification_secret: str | None = Header(default=None)) -> JSONResponse:
        """Hold one ordinary ASGI request long enough for deployed admission to contend."""
        require_secret(x_fleet_qualification_secret)
        await asyncio.sleep(1)
        return JSONResponse({"held": "request"})

    @router.get("/hold/stream")
    async def hold_stream(x_fleet_qualification_secret: str | None = Header(default=None)) -> StreamingResponse:
        """Hold one SSE response long enough for the deployed stream pool to contend."""
        require_secret(x_fleet_qualification_secret)

        async def events() -> AsyncIterator[bytes]:
            await asyncio.sleep(1)
            yield b"data: qualification\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return router


def qualification_router_from_environment(role: str, namespace: str) -> APIRouter | None:
    """Resolve gated values without making fake upstreams a runtime registry concern."""
    return qualification_router(
        role=role,
        namespace=namespace,
        secret=os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", ""),
        broker_url=os.environ.get("FLEET_FAKE_BROKER_URL", ""),
        market_data_url=os.environ.get("FLEET_FAKE_MARKET_DATA_URL", ""),
    )


__all__ = ["qualification_router", "qualification_router_from_environment"]
