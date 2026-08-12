"""GalleryHub — snapshot composition for the broker-v2 bot gallery.

Aggregates the running-bot catalog (``panel_data_source.get_catalog`` in
production) and per-symbol live bars (``LIVE_BAR_AGGREGATOR`` in production)
into one versioned ``GalleryLiveSnapshot`` for the gallery wall's REST
bootstrap and SSE channel. Bar mapping reuses the existing live-pane
conversion in ``chart_projection_service.aggregator_bars_to_chart_bars`` — no
new bar→``ChartBar`` mapping is introduced here.

``catalog_source`` and ``aggregator`` are constructor-injected so unit tests
exercise this module against fakes instead of the production singletons;
router wiring (a later task) passes the real ``panel_data_source`` module and
``LIVE_BAR_AGGREGATOR``.

KNOWN LIMITATION: ``build_snapshot`` sets ``markers={}``. Per-bot fill-marker
population is deferred to a later task — do not populate it here.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from app.schemas.broker_v2_gallery import (
    GalleryBotDelta,
    GalleryBotView,
    GalleryLiveSnapshot,
    GalleryLiveUpdate,
    GalleryPrimaryAction,
    GallerySymbolBars,
)
from app.schemas.broker_v2_panel import BotCatalogView
from app.services.broker_v2_panel.chart_projection_service import (
    aggregator_bars_to_chart_bars,
)
from app.utils.timestamps import now_ms_utc

# Per-process nonce so a fresh hub after a data-plane restart never produces
# an epoch byte-identical to the prior process's. Without this, a
# reconnecting client's stale high cursor (e.g. version 300) would compare
# equal to the new process's low-numbered epoch (both deterministically
# "broker:account_id") and the router would never emit `event: reset`,
# leaving the store's monotonic version guard silently dropping every
# post-restart frame until the counter climbs back. Mirrors the reference
# `SurfaceHub`'s per-process nonce.
_PROCESS_NONCE = uuid4().hex


def running_symbols(catalog: list[BotCatalogView]) -> list[str]:
    """Distinct symbols among running bots, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for row in catalog:
        if getattr(row, "running", False) and row.symbol not in seen:
            seen.add(row.symbol)
            out.append(row.symbol)
    return out


def _latest_bar_end_ms(symbol_bars: list[GallerySymbolBars]) -> dict[str, int]:
    """Latest ``end_ms`` per symbol (bars are ``start_ms``-ascending) for
    ``GalleryBotView.last_bar_at_ms``. Symbols with no bars in this call are
    omitted, so the bot projection falls back to ``None``."""
    return {entry.symbol: entry.bars[-1].end_ms for entry in symbol_bars if entry.bars}


class GalleryCatalogSource(Protocol):
    """Production implementation: ``app.services.broker_v2_panel.panel_data_source``."""

    async def get_catalog(self, broker: str, account_id: str) -> list[BotCatalogView]: ...


class GalleryBarAggregator(Protocol):
    """Production implementation: ``LIVE_BAR_AGGREGATOR`` (live_bar_aggregator.py).

    ``ensure_subscribed`` is ``async`` on the real aggregator (it may start a
    background task) — every existing call site awaits it
    (``panel_chart_data_source.py``, ``routers/broker.py``). ``snapshot`` is a
    synchronous read of the in-memory ring buffer and is never awaited.
    """

    async def ensure_subscribed(self, symbol: str) -> object: ...

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[object]: ...


class GalleryHub:
    """Composes one versioned snapshot of the running bot gallery for one account."""

    def __init__(
        self,
        *,
        broker: str,
        account_id: str,
        catalog_source: GalleryCatalogSource,
        aggregator: GalleryBarAggregator,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._catalog_source = catalog_source
        self._aggregator = aggregator
        self._epoch = f"{broker}:{account_id}:{_PROCESS_NONCE}"
        self._version = 0
        self._last_sids: set[str] = set()

    def _primary_action(self, row: BotCatalogView) -> GalleryPrimaryAction:
        # running -> Stop, otherwise Resume. Enablement/disabled_reason nuance
        # (mirroring row_action) is deferred to a later task.
        running = getattr(row, "running", False)
        return GalleryPrimaryAction(
            action_id="stop" if running else "resume",
            label="Stop" if running else "Resume",
            enabled=True,
            disabled_reason=None,
        )

    def _project_bot(
        self,
        row: BotCatalogView,
        *,
        model: type[GalleryBotView] = GalleryBotView,
        latest_bar_ms: dict[str, int] | None = None,
    ) -> GalleryBotView:
        """Project one catalog row into ``model`` (``GalleryBotView`` or its ``GalleryBotDelta`` subtype).

        ``last_bar_at_ms`` is looked up by ``row.symbol`` in ``latest_bar_ms``
        (the per-symbol latest bar ``end_ms`` computed by the caller from the
        same call's fetched bars) — ``None`` when that symbol has no bars.
        """
        return model(
            sid=row.strategy_instance_id,
            symbol=row.symbol,
            label=getattr(row, "strategy_label", ""),
            running=getattr(row, "running", False),
            phase=getattr(row, "phase", ""),
            desired_state=getattr(row, "desired_state", ""),
            needs_attention=getattr(row, "needs_attention", False),
            realized_pnl_today=getattr(row, "realized_pnl_today", 0.0) or 0.0,
            open_pnl=getattr(row, "open_pnl", 0.0) or 0.0,
            fills_today=getattr(row, "fills_today", 0) or 0,
            last_bar_at_ms=(latest_bar_ms or {}).get(row.symbol),
            primary_action=self._primary_action(row),
        )

    async def build_snapshot(self) -> GalleryLiveSnapshot:
        """Build one versioned snapshot: running bots + deduped per-symbol bars.

        Subscribes each running symbol exactly once (``running_symbols`` dedup)
        and snapshots the aggregator's ring buffer for it. ``markers`` is left
        empty — see module docstring.
        """
        catalog = await self._catalog_source.get_catalog(self._broker, self._account_id)
        running = [row for row in catalog if getattr(row, "running", False)]
        symbols = running_symbols(catalog)
        symbol_bars: list[GallerySymbolBars] = []
        for symbol in symbols:
            await self._aggregator.ensure_subscribed(symbol)
            raw = self._aggregator.snapshot(symbol)
            symbol_bars.append(
                GallerySymbolBars(symbol=symbol, bars=aggregator_bars_to_chart_bars(raw))
            )
        self._last_sids = {row.strategy_instance_id for row in running}
        self._version += 1
        latest_bar_ms = _latest_bar_end_ms(symbol_bars)
        return GalleryLiveSnapshot(
            stream_epoch=self._epoch,
            surface_version=self._version,
            as_of_ms=now_ms_utc(),
            resolution="1m",
            bots=[self._project_bot(row, latest_bar_ms=latest_bar_ms) for row in running],
            symbols=symbol_bars,
            markers={},
        )

    async def build_update(self, since_bar_ms: dict[str, int]) -> GalleryLiveUpdate:
        """Build one incremental update: new bars per symbol + bot deltas + removals.

        Mirrors ``build_snapshot``'s subscribe-then-read loop, but each running
        symbol's bars are read with ``since_ms=since_bar_ms.get(symbol)`` so the
        aggregator returns only bars appended since the caller's last-seen bar.
        Every running bot is re-projected into ``bots_delta`` (no dirty-tracking
        yet). ``removed_sids`` diffs the previous call's running-bot roster
        (tracked in ``self._last_sids``, seeded by ``build_snapshot``) against
        this one. ``markers_delta`` is left empty — see module docstring.
        """
        catalog = await self._catalog_source.get_catalog(self._broker, self._account_id)
        running = [row for row in catalog if getattr(row, "running", False)]
        symbols = running_symbols(catalog)
        symbol_bars: list[GallerySymbolBars] = []
        for symbol in symbols:
            await self._aggregator.ensure_subscribed(symbol)
            raw = self._aggregator.snapshot(symbol, since_ms=since_bar_ms.get(symbol))
            symbol_bars.append(
                GallerySymbolBars(symbol=symbol, bars=aggregator_bars_to_chart_bars(raw))
            )
        current_sids = {row.strategy_instance_id for row in running}
        removed_sids = sorted(self._last_sids - current_sids)
        self._last_sids = current_sids
        self._version += 1
        latest_bar_ms = _latest_bar_end_ms(symbol_bars)
        return GalleryLiveUpdate(
            surface_version=self._version,
            as_of_ms=now_ms_utc(),
            symbols=symbol_bars,
            markers_delta={},
            bots_delta=[
                self._project_bot(row, model=GalleryBotDelta, latest_bar_ms=latest_bar_ms)
                for row in running
            ],
            removed_sids=removed_sids,
        )
