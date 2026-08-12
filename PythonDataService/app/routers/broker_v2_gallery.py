"""Broker-v2 aggregated bot gallery routes (transport only).

``/api/brokers/{broker}/accounts/{account_id}/gallery/...`` — the gallery
wall's REST bootstrap (``snapshot``) and SSE channel (``stream``) for the
live 20-bot candlestick wall (one account, all running bots + their shared
per-symbol bars). The router validates/parses the HTTP request and delegates
all composition to ``GalleryHub`` (``app.services.broker_v2_panel.gallery_hub``)
— no business logic lives here, mirroring the router-freeze discipline of
``broker_v2_panel.py``.

``get_gallery_hub`` is a FastAPI dependency (not a plain helper) specifically
so tests can swap in a hub built from fakes via ``app.dependency_overrides``
(see ``tests/routers/test_broker_v2_gallery.py``). Production wiring passes
the real ``panel_data_source`` module and the ``LIVE_BAR_AGGREGATOR``
singleton — the same production seams ``broker_v2_panel.py`` and
``live_bar_aggregator.py`` already expose.

KNOWN LIMITATION: the module-level ``_HUB_CACHE`` dict is a simple
per-``(broker, account_id)`` cache with no ref-counting or teardown — a hub,
once built, lives for the process lifetime. Full lifecycle management
(eviction, ref-counted subscriber teardown like ``live_projection.py``'s hub
cache) is out of scope for this task.

The stream is poll-driven (``GalleryHub`` has no pub/sub producer): each
iteration calls ``build_update`` against the last-emitted bar per symbol and
emits an ``update`` frame only when something actually changed, else a
``: keepalive`` comment at most every ~15s. The ``cursor`` query parameter and
``reset`` event mirror ``broker_v2_panel.py``'s ``/live-stream`` reconnect
handling: a reconnecting client's remembered epoch is compared once against
the hub's current epoch before entering the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.schemas.broker_v2_gallery import GalleryLiveSnapshot, GallerySymbolBars
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.gallery_hub import GalleryHub
from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-v2-gallery"])

_KEEPALIVE_INTERVAL_S = 15.0
_POLL_INTERVAL_S = 1.0

_HUB_CACHE: dict[tuple[str, str], GalleryHub] = {}


def get_gallery_hub(broker: str, account_id: str) -> GalleryHub:
    """Return the per-``(broker, account_id)`` cached ``GalleryHub``.

    See the module docstring for the cache's known limitation (no
    ref-counting/eviction). A FastAPI dependency so tests can override it via
    ``app.dependency_overrides`` to inject a hub built from fakes.
    """
    key = (broker, account_id)
    hub = _HUB_CACHE.get(key)
    if hub is None:
        hub = GalleryHub(
            broker=broker,
            account_id=account_id,
            catalog_source=panel_data_source,
            aggregator=LIVE_BAR_AGGREGATOR,
        )
        _HUB_CACHE[key] = hub
    return hub


@router.get(
    "/{broker}/accounts/{account_id}/gallery/snapshot",
    response_model=GalleryLiveSnapshot,
    summary="Versioned REST bootstrap for the aggregated bot gallery wall",
)
async def get_gallery_snapshot(hub: GalleryHub = Depends(get_gallery_hub)) -> GalleryLiveSnapshot:
    return await hub.build_snapshot()


def _latest_bar_start_ms(symbol_bars: list[GallerySymbolBars]) -> dict[str, int]:
    """Latest ``start_ms`` per symbol (bars are ``start_ms``-ascending), for
    seeding/advancing the per-symbol ``since_bar_ms`` cursor."""
    return {entry.symbol: entry.bars[-1].start_ms for entry in symbol_bars if entry.bars}


async def _gallery_event_source(hub: GalleryHub, *, cursor: str | None) -> AsyncIterator[str]:
    snapshot = await hub.build_snapshot()
    epoch = snapshot.stream_epoch
    current_id = f"{epoch}:{snapshot.surface_version}"
    requested_epoch = cursor.rsplit(":", 1)[0] if cursor and ":" in cursor else None
    if cursor is not None and requested_epoch != epoch:
        payload = json.dumps({"reason": "epoch_changed", "cursor": current_id})
        yield f"event: reset\ndata: {payload}\n\n"
    yield f"id: {current_id}\nevent: snapshot\ndata: {snapshot.model_dump_json()}\n\n"

    since_bar_ms = _latest_bar_start_ms(snapshot.symbols)
    last_emit = time.monotonic()
    # No subscription/queue to release on exit: this is a poll loop, not a
    # pub/sub subscriber, so there is nothing to leak when the client
    # disconnects and the ASGI server closes this generator — the loop simply
    # stops at its next ``await``.
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        update = await hub.build_update(since_bar_ms)
        since_bar_ms.update(_latest_bar_start_ms(update.symbols))
        has_new_bars = any(entry.bars for entry in update.symbols)
        changed = has_new_bars or bool(update.bots_delta) or bool(update.removed_sids)
        now = time.monotonic()
        if changed:
            event_id = f"{epoch}:{update.surface_version}"
            yield f"id: {event_id}\nevent: update\ndata: {update.model_dump_json()}\n\n"
            last_emit = now
        elif now - last_emit >= _KEEPALIVE_INTERVAL_S:
            yield ": keepalive\n\n"
            last_emit = now


@router.get(
    "/{broker}/accounts/{account_id}/gallery/stream",
    summary="Poll-driven SSE stream of aggregated bot gallery updates",
)
async def stream_gallery(
    cursor: str | None = Query(default=None, max_length=128),
    hub: GalleryHub = Depends(get_gallery_hub),
) -> StreamingResponse:
    return StreamingResponse(
        _gallery_event_source(hub, cursor=cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
