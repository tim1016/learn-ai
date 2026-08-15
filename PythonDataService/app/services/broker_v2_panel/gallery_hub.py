"""GalleryHub — snapshot composition for the broker-v2 bot gallery.

Aggregates the bot catalog (``panel_data_source.get_catalog`` in production)
and per-symbol live bars (``LIVE_BAR_AGGREGATOR`` in production) into one
versioned ``GalleryLiveSnapshot`` for the gallery wall's REST bootstrap and
SSE channel. The wall shows every **non-retired** bot — running and
stopped/off-duty alike (bot-gallery-redesign spec §7.1, D3) — filtered by the
closed-vocabulary phase (``BotCatalogView.phase == "RETIRED"``, mirroring
``catalog_projection_service.status_label_for``; see ``_is_retired``).
Retired bots never reach the snapshot or update. A stopped bot still needs
today's bars to chart, so its symbol is subscribed/read exactly like a
running one's — this can subscribe more symbols than the old running-only
scope when a stopped bot holds an otherwise-unwatched symbol; accepted (spec
§11 risk). Bar mapping reuses the existing live-pane conversion in
``chart_projection_service.aggregator_bars_to_chart_bars`` — no new bar→
``ChartBar`` mapping is introduced here.

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


def _is_retired(row: BotCatalogView) -> bool:
    """True when the catalog row's closed-vocabulary phase is Retired.

    Mirrors the predicate in
    ``catalog_projection_service.status_label_for`` (``status.phase ==
    "RETIRED"``) — the single source of truth for the phase→status-label
    vocabulary. Retired bots are archived, off-wall by design (spec §11):
    a Resume affordance on a retired tile would be a lie.
    """
    return getattr(row, "phase", "") == "RETIRED"


def shown_symbols(catalog: list[BotCatalogView]) -> list[str]:
    """Distinct symbols among shown (non-retired) bots, in first-seen order.

    "Shown" includes both running and stopped/off-duty bots — a stopped bot
    still needs today's bars to chart on the wall.
    """
    seen: set[str] = set()
    out: list[str] = []
    for row in catalog:
        if not _is_retired(row) and row.symbol not in seen:
            seen.add(row.symbol)
            out.append(row.symbol)
    return out


def _latest_bar_end_ms(symbol_bars: list[GallerySymbolBars]) -> dict[str, int]:
    """Latest ``end_ms`` per symbol (bars are ``start_ms``-ascending) among
    the bars fetched in one call. Symbols with no new bars in this call are
    omitted — callers merge this into a cumulative per-symbol map rather
    than replacing it, since a poll that saw no new bar for a symbol must
    not blank out that symbol's already-known ``last_bar_at_ms``."""
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
    """Composes one versioned snapshot of the non-retired bot gallery for one account."""

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
        # Cumulative per-symbol "latest known bar end_ms", merged (never
        # replaced) on every fetch — see ``_latest_bar_end_ms``. Safe to
        # share across every SSE client for this account: it only ever
        # advances, so whichever client's poll happens to observe a new bar
        # first, every other client's next projection sees the same value.
        # Unlike a per-client roster baseline (see ``build_update``), there
        # is no diff-against-a-snapshot here, so no cross-client race.
        self._latest_bar_end_ms: dict[str, int] = {}

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
    ) -> GalleryBotView:
        """Project one catalog row into ``model`` (``GalleryBotView`` or its ``GalleryBotDelta`` subtype).

        ``last_bar_at_ms`` is looked up by ``row.symbol`` in the hub's
        cumulative ``_latest_bar_end_ms`` map — ``None`` when that symbol has
        never produced a bar. ``realized_pnl_today``/``open_pnl``/
        ``fills_today`` preserve the catalog's own ``None`` (economics not
        yet available) rather than coercing it to a fabricated zero — the
        frontend renders the same dash the roster's ``fmtSignedCurrency``/
        ``fmtInteger`` already use for ``null``.
        """
        return model(
            sid=row.strategy_instance_id,
            symbol=row.symbol,
            label=getattr(row, "strategy_label", ""),
            running=getattr(row, "running", False),
            phase=getattr(row, "phase", ""),
            desired_state=getattr(row, "desired_state", ""),
            needs_attention=getattr(row, "needs_attention", False),
            realized_pnl_today=getattr(row, "realized_pnl_today", None),
            open_pnl=getattr(row, "open_pnl", None),
            fills_today=getattr(row, "fills_today", None),
            last_bar_at_ms=self._latest_bar_end_ms.get(row.symbol),
            primary_action=self._primary_action(row),
        )

    async def _fetch_shown_and_bars(
        self, *, since_bar_ms: dict[str, int] | None
    ) -> tuple[list[BotCatalogView], list[GallerySymbolBars]]:
        """Shared core of ``build_snapshot``/``build_update``: fetch the catalog,
        filter out retired bots (every other bot — running or stopped — is
        shown), and subscribe-then-read each shown symbol's bars exactly once
        (``shown_symbols`` dedup).

        ``since_bar_ms`` is ``None`` for a full snapshot read; when provided,
        each symbol's bars are read with ``since_ms=since_bar_ms.get(symbol)``
        so the aggregator returns only bars appended since the caller's
        last-seen bar (the incremental-update case). Merges this call's
        latest bar ends into ``self._latest_bar_end_ms`` before returning, so
        ``_project_bot`` always has the most recent known bar for a symbol
        even on a poll that saw no new bar for it.
        """
        catalog = await self._catalog_source.get_catalog(self._broker, self._account_id)
        shown = [row for row in catalog if not _is_retired(row)]
        symbol_bars: list[GallerySymbolBars] = []
        for symbol in shown_symbols(catalog):
            await self._aggregator.ensure_subscribed(symbol)
            since_ms = since_bar_ms.get(symbol) if since_bar_ms is not None else None
            raw = self._aggregator.snapshot(symbol, since_ms=since_ms)
            symbol_bars.append(
                GallerySymbolBars(symbol=symbol, bars=aggregator_bars_to_chart_bars(raw))
            )
        self._latest_bar_end_ms.update(_latest_bar_end_ms(symbol_bars))
        return shown, symbol_bars

    async def build_snapshot(self) -> GalleryLiveSnapshot:
        """Build one versioned snapshot: every non-retired bot (running +
        stopped/off-duty) + deduped per-symbol bars.

        ``markers`` is left empty — see module docstring.
        """
        shown, symbol_bars = await self._fetch_shown_and_bars(since_bar_ms=None)
        self._version += 1
        return GalleryLiveSnapshot(
            stream_epoch=self._epoch,
            surface_version=self._version,
            as_of_ms=now_ms_utc(),
            resolution="1m",
            bots=[self._project_bot(row) for row in shown],
            symbols=symbol_bars,
            markers={},
        )

    async def build_update(
        self, since_bar_ms: dict[str, int], *, known_sids: set[str]
    ) -> GalleryLiveUpdate:
        """Build one incremental update: new bars per symbol + bot deltas + removals.

        Every shown (non-retired) bot is re-projected into ``bots_delta`` (no
        dirty-tracking yet) — this includes a bot that stopped since the last
        call, which re-projects with ``running=False`` (``_primary_action``
        then derives Resume) rather than dropping it from the wall.
        ``known_sids`` is the *caller's own* last-observed shown roster —
        each SSE stream tracks this itself (see the router's
        ``_gallery_event_source``) rather than the hub holding one shared
        roster baseline. A single ``GalleryHub`` is cached per account and
        shared by every concurrent client (reconnects, multiple tabs); a
        hub-wide baseline would let the first client's poll consume a bot's
        departure from the catalog, leaving every other client's
        ``removed_sids`` empty for it. ``removed_sids`` diffs ``known_sids``
        against this call's shown roster, so it fires only when a bot
        actually leaves the catalog (retired or deleted) — never merely
        because it stopped running. ``markers_delta`` is left empty — see
        module docstring.
        """
        shown, symbol_bars = await self._fetch_shown_and_bars(since_bar_ms=since_bar_ms)
        current_sids = {row.strategy_instance_id for row in shown}
        removed_sids = sorted(known_sids - current_sids)
        self._version += 1
        return GalleryLiveUpdate(
            surface_version=self._version,
            as_of_ms=now_ms_utc(),
            symbols=symbol_bars,
            markers_delta={},
            bots_delta=[self._project_bot(row, model=GalleryBotDelta) for row in shown],
            removed_sids=removed_sids,
        )
