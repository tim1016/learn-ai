"""Broker-v2 aggregated bot gallery routes (transport only).

``/api/brokers/{broker}/accounts/{account_id}/gallery/...`` — the gallery
wall's REST bootstrap (``snapshot``) and SSE channel (``stream``) for the
live 20-bot candlestick wall (one account, every non-retired bot — running
and stopped/off-duty alike — + their shared per-symbol bars). The router
validates/parses the HTTP request and delegates all composition to
``GalleryHub`` (``app.services.broker_v2_panel.gallery_hub``) — no business
logic lives here, mirroring the router-freeze discipline of
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
iteration calls ``build_update`` against the last-emitted bar per symbol.
``GalleryHub.build_update`` re-projects every shown (non-retired) bot into
``bots_delta`` on every call (no per-bot dirty-tracking yet — see its
module docstring), so in practice an ``update`` frame is emitted on
essentially every ~1s poll while the account has at least one non-retired
bot — running or stopped; only the bar deltas are genuinely incremental.
The ``: keepalive`` comment (at most every ~15s) only fires once the
account has zero non-retired bots (a bot no longer drops out of
``bots_delta`` merely by stopping). The ``cursor`` query parameter and
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

from app.broker.alpaca.clerk.fills import FillRecord
from app.schemas.broker_v2_gallery import GalleryLiveSnapshot, GallerySymbolBars
from app.schemas.broker_v2_panel import ChartFillMarker
from app.services.broker_v2_panel import panel_chart_data_source, panel_data_source
from app.services.broker_v2_panel.gallery_hub import GalleryFillSource, GalleryHub
from app.services.broker_v2_panel.panel_data_source import PanelUnavailableError, UnknownBotError
from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-v2-gallery"])

_KEEPALIVE_INTERVAL_S = 15.0
_POLL_INTERVAL_S = 1.0

_HUB_CACHE: dict[tuple[str, str], GalleryHub] = {}


class _PanelChartFillSource:
    """Adapts ``panel_chart_data_source.resolve_symbol_and_fills`` to the
    ``GalleryFillSource`` contract ``GalleryHub`` expects.

    Reuses the exact SQLite-vs-legacy fill authority branch the single-bot
    detail chart's ``get_live_chart`` uses (CLAUDE.md single-source-of-truth
    rule) but never lets one bot's unavailable fill evidence — a SQLite
    revision race, a bot too new to have a projection yet — fail the whole
    gallery snapshot for every other bot; it logs and degrades to no markers
    for that bot instead.
    """

    async def resolve_symbol_and_fills(
        self, broker: str, account_id: str, sid: str, *, now_ms: int
    ) -> tuple[str, tuple[FillRecord, ...]]:
        try:
            return await panel_chart_data_source.resolve_symbol_and_fills(
                broker, account_id, sid, now_ms=now_ms
            )
        except (PanelUnavailableError, UnknownBotError) as exc:
            logger.warning(
                "[GALLERY] fill evidence unavailable for bot; rendering no markers",
                extra={"broker": broker, "account_id": account_id, "sid": sid, "error": str(exc)},
            )
            return "", ()


_FILL_SOURCE: GalleryFillSource = _PanelChartFillSource()


async def get_gallery_hub(broker: str, account_id: str) -> GalleryHub:
    """Return the per-``(broker, account_id)`` cached ``GalleryHub``.

    See the module docstring for the cache's known limitation (no
    ref-counting/eviction). A FastAPI dependency so tests can override it via
    ``app.dependency_overrides`` to inject a hub built from fakes.

    ``async def`` (not a plain sync helper) so FastAPI awaits this directly
    on the event loop instead of running it in a threadpool
    (``run_in_threadpool``) — the get-or-create check-then-set below has no
    ``await`` in it, so keeping it on the event loop makes it atomic. A
    sync ``def`` here would let two concurrent requests for the same
    ``(broker, account_id)`` (e.g. a gallery page firing ``/snapshot`` and
    ``/stream`` at once) race on separate threads, each missing the cache
    and constructing its own ``GalleryHub`` with an independent
    ``surface_version``/``_last_sids`` — exactly the bug
    ``get_or_start_live_projection_hub`` (``live_projection.py``) already
    avoids the same way.
    """
    key = (broker, account_id)
    hub = _HUB_CACHE.get(key)
    if hub is None:
        hub = GalleryHub(
            broker=broker,
            account_id=account_id,
            catalog_source=panel_data_source,
            aggregator=LIVE_BAR_AGGREGATOR,
            fill_source=_FILL_SOURCE,
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


def _latest_marker_ms(markers: dict[str, list[ChartFillMarker]]) -> dict[str, int]:
    """Latest ``filled_at_ms`` per sid among the markers in one call, for
    seeding/advancing this stream's own ``since_marker_ms`` cursor (mirrors
    ``_latest_bar_start_ms``). Deliberately local to the generator, not hub
    state — see ``GalleryHub.build_update``'s cross-client race note."""
    return {
        sid: max(marker.filled_at_ms for marker in sid_markers)
        for sid, sid_markers in markers.items()
        if sid_markers
    }


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
    # This stream's own last-delivered fill cursor per sid, seeded from the
    # snapshot's markers (already fully delivered) so the first update never
    # resends them. Deliberately local to this generator for the same
    # cross-client reason as ``known_sids`` below (see
    # ``GalleryHub.build_update``'s docstring).
    since_marker_ms = _latest_marker_ms(snapshot.markers)
    # This stream's own last-observed shown (non-retired) roster, passed to
    # every ``build_update`` call. Deliberately local to this generator (one
    # per SSE connection) rather than read off ``hub`` — the same account's
    # ``GalleryHub`` is shared across every concurrent client (reconnects,
    # multiple tabs), so a hub-wide baseline would let the first client's
    # poll consume a bot's departure from the catalog and leave every other
    # client's ``removed_sids`` empty for it.
    known_sids = {bot.sid for bot in snapshot.bots}
    last_emit = time.monotonic()
    # No subscription/queue to release on exit: this is a poll loop, not a
    # pub/sub subscriber, so there is nothing to leak when the client
    # disconnects and the ASGI server closes this generator — the loop simply
    # stops at its next ``await``.
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        update = await hub.build_update(
            since_bar_ms, known_sids=known_sids, since_marker_ms=since_marker_ms
        )
        since_bar_ms.update(_latest_bar_start_ms(update.symbols))
        since_marker_ms.update(_latest_marker_ms(update.markers_delta))
        # ``bots_delta`` is always the full shown (non-retired) roster (see
        # the hub's docstring), so it doubles as this stream's next
        # known-roster baseline with no extra bookkeeping.
        known_sids = {bot.sid for bot in update.bots_delta}
        has_new_bars = any(entry.bars for entry in update.symbols)
        # ``bots_delta`` is always the full shown roster (GalleryHub has no
        # per-bot dirty-tracking yet), so this is effectively "any bot is
        # shown" rather than "a bot actually changed" — an update frame goes
        # out on essentially every poll while the account has at least one
        # non-retired bot. Only ``has_new_bars``/``removed_sids`` are
        # genuinely incremental. Keepalive only fires once ``bots_delta`` is
        # empty (the account has zero non-retired bots).
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
