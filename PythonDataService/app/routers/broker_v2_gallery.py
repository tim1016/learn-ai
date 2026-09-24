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

Every frame fits the fleet coordinator's per-event cap (#2328): a frame
whose inline bars would exceed it sends them first as ``bars`` pages (see
``_capped_frames``). A frame that cannot fit even then ends the stream with
a ``refused`` event, never an oversized frame for the coordinator to abort.
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
from app.broker.fleet.internal_http import DEFAULT_MAX_EVENT_BYTES
from app.schemas.broker_v2_gallery import (
    GalleryBarsPage,
    GalleryLiveSnapshot,
    GalleryLiveUpdate,
    GallerySymbolBars,
)
from app.schemas.broker_v2_panel import ChartFillMarker, PanelAction
from app.services.broker_v2_panel import panel_chart_data_source, panel_data_source
from app.services.broker_v2_panel.gallery_hub import (
    GalleryFillSource,
    GalleryHub,
    GalleryPrimaryActionSource,
)
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError, UnknownBotError
from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brokers", tags=["broker-v2-gallery"])

_KEEPALIVE_INTERVAL_S = 15.0
_POLL_INTERVAL_S = 1.0
# Just under one poll interval: every connected client sees fresh data at
# least once per its own ~1s poll, while N clients polling within the same
# window collapse to one real catalog fetch + one real fill-source fan-out
# instead of N of each (GalleryHub.__init__'s `io_cache_ttl_ms` docstring).
_GALLERY_IO_CACHE_TTL_MS = 800
# The fleet coordinator relays this stream and aborts it on any single event
# over its per-event cap, after the 200 has gone out (#2328). No frame this
# lane emits may exceed it.
_MAX_EVENT_BYTES = DEFAULT_MAX_EVENT_BYTES
# Bytes held back from that cap for framing added after this router: the
# lane's ``FleetIdentityMiddleware`` (``agent_identity._FrameInjector``)
# appends ``x-fleet-*`` provenance lines to every event (~115 B for a
# typical clerk id, epoch and generation). 8 KB covers any realistic
# identity with a wide margin, so a frame that fits here still fits there.
_FLEET_FRAMING_RESERVE_BYTES = 8_192
# Bars per ``bars`` page. A serialized 5 s ``ChartBar`` is ~145 B, so a page
# is ~145 KB: far under the cap, whatever the ring-buffer depth.
_BARS_PER_PAGE = 1_000

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


class _PanelPrimaryActionSource:
    """Resolve the full panel's request-specific Resume admission action.

    The catalog intentionally publishes no Resume row action, so the gallery
    must consult this authoritative projection before enabling a stopped bot's
    quick action.
    """

    async def resolve_resume_action(
        self, broker: str, account_id: str, sid: str
    ) -> PanelAction | None:
        try:
            panel = await panel_data_source.get_panel(broker, account_id, sid)
        except (PanelUnavailableError, UnknownBotError) as exc:
            logger.warning(
                "[GALLERY] Resume admission unavailable for bot; disabling quick action",
                extra={"broker": broker, "account_id": account_id, "sid": sid, "error": str(exc)},
            )
            return None
        return next((action for action in panel.actions if action.action_id == "resume"), None)


_PRIMARY_ACTION_SOURCE: GalleryPrimaryActionSource = _PanelPrimaryActionSource()


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
            resolution="5s",
            fill_source=_FILL_SOURCE,
            primary_action_source=_PRIMARY_ACTION_SOURCE,
            io_cache_ttl_ms=_GALLERY_IO_CACHE_TTL_MS,
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


def _marker_event_keys(markers: dict[str, list[ChartFillMarker]]) -> dict[str, set[str]]:
    """Per-sid set of ``event_key``s among the markers in one call, for
    seeding/advancing this stream's own delivered-fills cursor (mirrors
    ``_latest_bar_start_ms``). Deliberately local to the generator, not hub
    state — see ``GalleryHub.build_update``'s cross-client race note.

    Callers must UNION this into their accumulated cursor, never replace it
    — a sid's delivered set has to keep growing across polls, or a fill
    delivered two polls ago would fall out of "seen" the moment a poll
    without that sid's keys arrives, and get resent."""
    return {sid: {marker.event_key for marker in sid_markers} for sid, sid_markers in markers.items() if sid_markers}


def _frame_budget_bytes() -> int:
    """The most bytes one frame may carry when it leaves this router: the
    coordinator's cap less the lane's framing reserve."""
    return _MAX_EVENT_BYTES - _FLEET_FRAMING_RESERVE_BYTES


class _OversizedFrameError(Exception):
    """A frame is over the frame budget even with its bars paged out."""

    def __init__(self, event: str, size: int, budget: int) -> None:
        super().__init__(f"gallery {event} frame is {size} bytes; the budget is {budget}")
        self.event = event
        self.size = size
        self.budget = budget


def _sse_frame(event: str, data: str, *, event_id: str | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event}\ndata: {data}\n\n"


def _capped_frames(
    event: str, event_id: str, payload: GalleryLiveSnapshot | GalleryLiveUpdate
) -> list[str]:
    """``payload`` as SSE frames that each fit ``_frame_budget_bytes`` (#2328).

    A frame that already fits goes out whole. Otherwise every symbol's bars
    move into ``bars`` pages (``GalleryBarsPage``) sent first, and the frame
    itself follows with each symbol's ``bars`` empty and its
    ``paged_bar_count`` set, so the client can prove it staged every page.
    A frame still over the budget after that raises ``_OversizedFrameError``:
    it is never handed to the coordinator to be cut off mid-stream.
    """
    budget = _frame_budget_bytes()
    whole = _sse_frame(event, payload.model_dump_json(), event_id=event_id)
    if len(whole.encode()) <= budget:
        return [whole]
    pages = [
        _sse_frame(
            "bars",
            GalleryBarsPage(
                surface_version=payload.surface_version,
                symbol=entry.symbol,
                bars=entry.bars[start : start + _BARS_PER_PAGE],
            ).model_dump_json(),
        )
        for entry in payload.symbols
        for start in range(0, len(entry.bars), _BARS_PER_PAGE)
    ]
    head = payload.model_copy(
        update={
            "symbols": [
                GallerySymbolBars(symbol=entry.symbol, paged_bar_count=len(entry.bars))
                for entry in payload.symbols
            ]
        }
    )
    frames = [*pages, _sse_frame(event, head.model_dump_json(), event_id=event_id)]
    for frame in frames:
        size = len(frame.encode())
        if size > budget:
            raise _OversizedFrameError(event, size, budget)
    return frames


async def _gallery_event_source(hub: GalleryHub, *, cursor: str | None) -> AsyncIterator[str]:
    """The gallery stream, ended by a named ``refused`` event when a frame
    cannot fit the coordinator's cap. The client stops reconnecting and says
    the wall is not live; it never sees an open stream that carries no
    data (#2328)."""
    try:
        async for frame in _gallery_frames(hub, cursor=cursor):
            yield frame
    except _OversizedFrameError as exc:
        logger.error(
            "[GALLERY] stream frame exceeds the fleet event cap; refusing the stream",
            extra={"event": exc.event, "bytes": exc.size, "max_bytes": exc.budget},
        )
        payload = json.dumps(
            {
                "reason": "frame_too_large",
                "event": exc.event,
                "bytes": exc.size,
                "max_bytes": exc.budget,
            }
        )
        yield _sse_frame("refused", payload)


async def _gallery_frames(hub: GalleryHub, *, cursor: str | None) -> AsyncIterator[str]:
    snapshot = await hub.build_snapshot()
    epoch = snapshot.stream_epoch
    current_id = f"{epoch}:{snapshot.surface_version}"
    requested_epoch = cursor.rsplit(":", 1)[0] if cursor and ":" in cursor else None
    if cursor is not None and requested_epoch != epoch:
        payload = json.dumps({"reason": "epoch_changed", "cursor": current_id})
        yield f"event: reset\ndata: {payload}\n\n"
    for frame in _capped_frames("snapshot", current_id, snapshot):
        yield frame

    since_bar_ms = _latest_bar_start_ms(snapshot.symbols)
    # This stream's own delivered-fills cursor per sid — a set of event_keys,
    # not a scalar timestamp (two fills can share a millisecond; see
    # ``gallery_hub._markers_delta``) — seeded from the snapshot's markers
    # (already fully delivered) so the first update never resends them.
    # Deliberately local to this generator for the same cross-client reason
    # as ``known_sids`` below (see ``GalleryHub.build_update``'s docstring).
    since_marker_keys = _marker_event_keys(snapshot.markers)
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
            since_bar_ms, known_sids=known_sids, since_marker_keys=since_marker_keys
        )
        since_bar_ms.update(_latest_bar_start_ms(update.symbols))
        for sid, keys in _marker_event_keys(update.markers_delta).items():
            since_marker_keys.setdefault(sid, set()).update(keys)
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
            for frame in _capped_frames("update", f"{epoch}:{update.surface_version}", update):
                yield frame
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
