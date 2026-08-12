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

import time
from typing import Protocol

from app.schemas.broker_v2_gallery import (
    GalleryBotView,
    GalleryLiveSnapshot,
    GalleryPrimaryAction,
    GallerySymbolBars,
)
from app.schemas.broker_v2_panel import BotCatalogView
from app.services.broker_v2_panel.chart_projection_service import (
    aggregator_bars_to_chart_bars,
)


def running_symbols(catalog: list[BotCatalogView]) -> list[str]:
    """Distinct symbols among running bots, in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for row in catalog:
        if getattr(row, "running", False) and row.symbol not in seen:
            seen.add(row.symbol)
            out.append(row.symbol)
    return out


class GalleryCatalogSource(Protocol):
    """Production implementation: ``app.services.broker_v2_panel.panel_data_source``."""

    async def get_catalog(self, broker: str, account_id: str) -> list[BotCatalogView]: ...


class GalleryBarAggregator(Protocol):
    """Production implementation: ``LIVE_BAR_AGGREGATOR`` (live_bar_aggregator.py)."""

    def ensure_subscribed(self, symbol: str) -> object: ...

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
        self._epoch = f"{broker}:{account_id}"
        self._version = 0

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

    def _project_bot(self, row: BotCatalogView) -> GalleryBotView:
        return GalleryBotView(
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
            last_bar_at_ms=None,
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
            self._aggregator.ensure_subscribed(symbol)
            raw = self._aggregator.snapshot(symbol)
            symbol_bars.append(
                GallerySymbolBars(symbol=symbol, bars=aggregator_bars_to_chart_bars(raw))
            )
        self._version += 1
        return GalleryLiveSnapshot(
            stream_epoch=self._epoch,
            surface_version=self._version,
            as_of_ms=int(time.time() * 1000),
            resolution="1m",
            bots=[self._project_bot(row) for row in running],
            symbols=symbol_bars,
            markers={},
        )
