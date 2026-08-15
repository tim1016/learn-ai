"""GalleryHub — snapshot composition for the broker-v2 bot gallery.

Aggregates the bot catalog (``panel_data_source.get_catalog`` in production)
and per-symbol live bars (``LIVE_BAR_AGGREGATOR`` in production) into one
versioned ``GalleryLiveSnapshot`` for the gallery wall's REST bootstrap and
SSE channel. The wall shows every **non-retired** bot — running and
stopped/off-duty alike (bot-gallery-redesign spec §7.1, D3) — filtered by the
closed-vocabulary status label (``BotCatalogView.status_label == "Retired"``,
the field ``catalog_projection_service.status_label_for`` already populates;
see ``_is_retired``).
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

``markers``/``markers_delta`` are populated per shown bot from that bot's
today fills, reusing ``chart_projection_service.fill_to_marker``/
``markers_in_window`` verbatim — the same fill→marker projection the
single-bot detail chart's LIVE pane uses (CLAUDE.md single-source-of-truth
rule; see ``_fetch_markers``). ``fill_source`` is optional: when not
injected (e.g. tests exercising only the catalog/bars path), both stay
empty, matching this module's prior hard-coded behavior.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol
from uuid import uuid4

from app.broker.alpaca.clerk.fills import FillRecord
from app.schemas.broker_v2_gallery import (
    GalleryBotDelta,
    GalleryBotView,
    GalleryLiveSnapshot,
    GalleryLiveUpdate,
    GalleryPrimaryAction,
    GallerySymbolBars,
)
from app.schemas.broker_v2_panel import BotCatalogView, ChartBar, ChartFillMarker
from app.services.broker_v2_panel.chart_projection_service import (
    aggregator_bars_to_chart_bars,
    live_window,
    markers_in_window,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

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
    """True when the catalog row's closed status label is Retired.

    Reads the already-materialized ``status_label`` field — populated once by
    the single source of truth for the phase→status-label vocabulary,
    ``catalog_projection_service.status_label_for`` — rather than
    re-deriving the phase comparison here, so this predicate can never drift
    from the canonical mapping. Retired bots are archived, off-wall by design
    (spec §11): a Resume affordance on a retired tile would be a lie.
    """
    return getattr(row, "status_label", "") == "Retired"


def _day_pnl(realized: float | None, open_pnl: float | None) -> float | None:
    """Null-safe ``realized + open`` — the wall's one day-P&L authority.

    ``None`` only when both components are unavailable; a lone-present
    component contributes its own value (mirrors the "show whichever side is
    present" display intent this replaces the frontend's own summing of).
    """
    if realized is None and open_pnl is None:
        return None
    # Explicit None-checks, not `x or 0.0` — a legitimate 0.0 P&L is falsy
    # too and must not be confused with "absent".
    return (realized if realized is not None else 0.0) + (
        open_pnl if open_pnl is not None else 0.0
    )


def _session_change_pct(bars: Sequence[ChartBar], *, open_ms: int) -> float | None:
    """Session return from the first bar within TODAY's session to the last
    bar overall — never ``bars[0]``, which can be a prior session's bar: the
    aggregator's ring buffer intentionally retains slightly more than one
    session's worth (``live_bar_aggregator.py``'s ``_RING_BUFFER_SIZE``
    comment: "covers a full session plus"), so a naive first-element read
    can silently fold in a stale close-to-open gap from a prior day.
    ``None`` when no bar falls within ``[open_ms, ...)`` yet, or that
    session's first open is zero.
    """
    session_bars = [bar for bar in bars if bar.start_ms >= open_ms]
    if not session_bars:
        return None
    first_open = float(session_bars[0].open)
    if first_open == 0:
        return None
    last_close = float(session_bars[-1].close)
    return (last_close - first_open) / first_open


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


def _markers_delta(
    markers: dict[str, list[ChartFillMarker]],
    since_marker_keys: dict[str, set[str]] | None,
) -> dict[str, list[ChartFillMarker]]:
    """Filter ``markers`` down to each sid's fills whose ``event_key`` is not
    already in the caller's own delivered set — see ``GalleryHub.build_update``
    for why this cursor is caller-supplied rather than hub state.

    Keyed on ``event_key``, not ``filled_at_ms``: two distinct fills (a fast
    partial fill, two bots filling in the same poll) can share a millisecond
    timestamp, and a scalar watermark (``filled_at_ms > baseline``) would
    silently and permanently drop whichever one wasn't the strict max — a
    fill "equal to" an already-delivered fill's timestamp would never satisfy
    a strict ``>`` and would never be sent. A sid with nothing new is
    omitted, mirroring ``_latest_bar_end_ms``'s omission convention.
    """
    baseline = since_marker_keys or {}
    delta: dict[str, list[ChartFillMarker]] = {}
    for sid, sid_markers in markers.items():
        seen = baseline.get(sid, set())
        newer = [marker for marker in sid_markers if marker.event_key not in seen]
        if newer:
            delta[sid] = newer
    return delta


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


class GalleryFillSource(Protocol):
    """Production implementation: an adapter over
    ``panel_chart_data_source.resolve_symbol_and_fills`` wired in
    ``broker_v2_gallery.get_gallery_hub`` — the same SQLite-vs-legacy fill
    authority branch ``get_live_chart`` uses for the single-bot detail chart,
    so the wall and the detail chart never diverge on fill provenance.

    Must not raise for one bot's unavailable fill evidence (a SQLite revision
    race, a bot too new to have a projection yet) — return an empty fill
    sequence so one bot's projection gap never fails the whole gallery
    snapshot for every other bot.
    """

    async def resolve_symbol_and_fills(
        self, broker: str, account_id: str, sid: str, *, now_ms: int
    ) -> tuple[str, Sequence[FillRecord]]: ...


class GalleryHub:
    """Composes one versioned snapshot of the non-retired bot gallery for one account."""

    def __init__(
        self,
        *,
        broker: str,
        account_id: str,
        catalog_source: GalleryCatalogSource,
        aggregator: GalleryBarAggregator,
        fill_source: GalleryFillSource | None = None,
        io_cache_ttl_ms: int = 0,
    ) -> None:
        self._broker = broker
        self._account_id = account_id
        self._catalog_source = catalog_source
        self._aggregator = aggregator
        self._fill_source = fill_source
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
        # A hub is cached per (broker, account_id) and shared by every
        # concurrently-connected client (each SSE stream polls it on its own
        # ~1s cadence, plus REST fallback) — without this, N connected
        # clients cause N independent catalog fetches and N independent
        # fill-source fan-outs per poll interval for identical data. A
        # short-TTL cache on those two genuinely expensive I/O calls
        # (``_fetch_catalog``/``_fetch_markers``) collapses that to O(1) per
        # TTL window regardless of N. `0` (default) disables caching
        # entirely — every call fetches fresh, the behavior every existing
        # test assumes — production wiring (``broker_v2_gallery.get_gallery_hub``)
        # opts in with a real TTL. Deliberately NOT a full background
        # producer/subscriber-fan-out rewrite (the `SurfaceHub` pattern,
        # `surface_hub.py`): that would change every `build_snapshot`/
        # `build_update` call from "computes and returns fresh data now" to
        # "returns whatever the last background cycle produced", which is a
        # different contract the extensive existing per-caller-delta test
        # suite is built around — tracked as a larger follow-up, not
        # attempted here under time pressure.
        self._io_cache_ttl_ms = io_cache_ttl_ms
        self._catalog_cache: list[BotCatalogView] | None = None
        self._catalog_cache_at_ms = -1
        self._markers_cache: dict[str, list[ChartFillMarker]] | None = None
        self._markers_cache_at_ms = -1

    def _primary_action(self, row: BotCatalogView) -> GalleryPrimaryAction:
        """Derive the wall's single quick action: Stop while running, Resume
        while not. Enablement/disabled_reason reuse the roster's own
        authoritative ``row_action`` (``panel_data_source.get_catalog`` →
        ``build_roster_action``, admission/hold/flatten-aware) when it's
        present and its ``action_id`` matches what this projects — a stopped
        bot is not unconditionally resumable (e.g. admission unavailable, an
        active hold), and presenting an enabled Resume the confirm-time
        authoritative-panel check then rejects is a real, if non-fatal, bad
        experience. Falls back to always-enabled when no ``row_action`` was
        projected (catalog sources that don't populate it, incl. most test
        doubles) or it names a different routine action for this row — the
        gallery's one quick-action slot has no way to represent a non-
        stop/resume routine action yet, unchanged from before this fix.
        """
        running = getattr(row, "running", False)
        action_id = "stop" if running else "resume"
        label = "Stop" if running else "Resume"
        row_action = getattr(row, "row_action", None)
        if row_action is not None and row_action.action_id == action_id:
            return GalleryPrimaryAction(
                action_id=action_id,
                label=label,
                enabled=row_action.enabled,
                disabled_reason=None if row_action.enabled else row_action.explanation,
            )
        return GalleryPrimaryAction(
            action_id=action_id,
            label=label,
            enabled=True,
            disabled_reason=None,
        )

    def _project_bot(
        self,
        row: BotCatalogView,
        *,
        session_change_pcts: dict[str, float],
        model: type[GalleryBotView] = GalleryBotView,
    ) -> GalleryBotView:
        """Project one catalog row into ``model`` (``GalleryBotView`` or its ``GalleryBotDelta`` subtype).

        ``last_bar_at_ms`` is looked up by ``row.symbol`` in the hub's
        cumulative ``_latest_bar_end_ms`` map — ``None`` when that symbol has
        never produced a bar. ``realized_pnl_today``/``open_pnl``/
        ``fills_today`` preserve the catalog's own ``None`` (economics not
        yet available) rather than coercing it to a fabricated zero — the
        frontend renders the same dash the roster's ``fmtSignedCurrency``/
        ``fmtInteger`` already use for ``null``. ``day_pnl`` is the one
        numerical answer derived here rather than left to the frontend to sum
        (see ``_day_pnl``). ``session_change_pct`` is looked up by symbol in
        ``session_change_pcts`` (see ``_fetch_session_change_pcts``) —
        ``None`` when that symbol has no session-scoped bar yet.
        """
        realized = getattr(row, "realized_pnl_today", None)
        open_pnl = getattr(row, "open_pnl", None)
        return model(
            sid=row.strategy_instance_id,
            symbol=row.symbol,
            label=getattr(row, "strategy_label", ""),
            running=getattr(row, "running", False),
            phase=getattr(row, "phase", ""),
            desired_state=getattr(row, "desired_state", ""),
            needs_attention=getattr(row, "needs_attention", False),
            realized_pnl_today=realized,
            open_pnl=open_pnl,
            day_pnl=_day_pnl(realized, open_pnl),
            session_change_pct=session_change_pcts.get(row.symbol),
            fills_today=getattr(row, "fills_today", None),
            last_bar_at_ms=self._latest_bar_end_ms.get(row.symbol),
            primary_action=self._primary_action(row),
        )

    async def _fetch_catalog(self) -> list[BotCatalogView]:
        """The account's bot catalog, cached for ``self._io_cache_ttl_ms``.

        Real I/O (``catalog_source.get_catalog``) — the expensive half of
        every poll, alongside ``_fetch_markers``'s fan-out. Every
        concurrently-connected client's poll would otherwise trigger its own
        fetch of identical data; within the TTL window, this returns the
        cached result instead. TTL ``0`` (the default) disables caching —
        every call fetches fresh.
        """
        now = now_ms_utc()
        if (
            self._catalog_cache is not None
            and now - self._catalog_cache_at_ms < self._io_cache_ttl_ms
        ):
            return self._catalog_cache
        catalog = await self._catalog_source.get_catalog(self._broker, self._account_id)
        self._catalog_cache = catalog
        self._catalog_cache_at_ms = now
        return catalog

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
        catalog = await self._fetch_catalog()
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

    def _fetch_session_change_pcts(self, symbols: list[str], *, open_ms: int) -> dict[str, float]:
        """Per-symbol session return (see ``_session_change_pct``).

        Reads the aggregator's FULL in-memory bar buffer fresh
        (``since_ms=None``) for each symbol — not ``build_update``'s own
        ``since_bar_ms``-sliced ``symbol_bars``, which on an incremental poll
        may carry only the newest bar or two and can't establish today's
        session open on its own. ``GalleryBarAggregator.snapshot`` is
        documented as a synchronous in-memory ring-buffer read, not I/O, so
        this extra unsliced read per poll is cheap — unlike the fill-source
        fan-out in ``_fetch_markers``, which is real I/O and is fanned out
        via ``asyncio.gather`` for exactly that reason. A symbol with no
        session-scoped bar yet is omitted, mirroring ``_latest_bar_end_ms``'s
        omission convention.
        """
        pcts: dict[str, float] = {}
        for symbol in symbols:
            raw = self._aggregator.snapshot(symbol, since_ms=None)
            pct = _session_change_pct(aggregator_bars_to_chart_bars(raw), open_ms=open_ms)
            if pct is not None:
                pcts[symbol] = pct
        return pcts

    async def _fetch_markers(
        self, shown: list[BotCatalogView], *, now_ms: int
    ) -> dict[str, list[ChartFillMarker]]:
        """Today's fill markers per shown bot, reusing
        ``chart_projection_service.markers_in_window`` verbatim — no
        fill→marker mapping is reimplemented here (module docstring).

        Empty dict when no ``fill_source`` was injected (e.g. tests
        exercising only the catalog/bars path). A sid with no fills in
        today's window is omitted from the result entirely, mirroring how
        ``_latest_bar_end_ms`` omits a symbol with no new bars — never an
        empty list under the key.

        Fanned out via ``asyncio.gather`` — this runs once per shown bot on
        every ~1s SSE poll (``broker_v2_gallery._gallery_event_source``), so
        a sequential per-bot await chain would scale poll latency linearly
        with the account's bot count. Mirrors the same fan-out idiom
        ``live_projection.py`` already uses for its per-hub refresh.
        ``return_exceptions=True`` is a defensive backstop, not the expected
        path: the production ``fill_source`` (``_PanelChartFillSource``)
        already catches and degrades per bot, so a raised exception here
        would mean an *unexpected* fill-source failure — logged and skipped
        rather than allowed to fail every other bot's markers too.

        Cached for ``self._io_cache_ttl_ms`` (same TTL, same reasoning as
        ``_fetch_catalog``): this is real per-bot I/O fanned out across every
        shown bot, and every concurrently-connected client's poll would
        otherwise repeat the whole fan-out for identical data. Safe to share
        across calls within the window because ``shown`` is itself derived
        from the TTL-cached catalog, so it doesn't vary faster than the cache
        does.
        """
        if self._fill_source is None:
            return {}
        if (
            self._markers_cache is not None
            and now_ms - self._markers_cache_at_ms < self._io_cache_ttl_ms
        ):
            return self._markers_cache
        open_ms, close_ms = live_window(now_ms)
        sids = [row.strategy_instance_id for row in shown]
        results = await asyncio.gather(
            *(
                self._fill_source.resolve_symbol_and_fills(
                    self._broker, self._account_id, sid, now_ms=now_ms
                )
                for sid in sids
            ),
            return_exceptions=True,
        )
        markers: dict[str, list[ChartFillMarker]] = {}
        for sid, result in zip(sids, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "[GALLERY] unexpected fill-source failure for bot; rendering no markers",
                    extra={"broker": self._broker, "account_id": self._account_id, "sid": sid},
                    exc_info=result,
                )
                continue
            _symbol, fills = result
            projected = markers_in_window(fills, from_ms=open_ms, to_ms=close_ms)
            if projected:
                markers[sid] = projected
        self._markers_cache = markers
        self._markers_cache_at_ms = now_ms
        return markers

    async def build_snapshot(self) -> GalleryLiveSnapshot:
        """Build one versioned snapshot: every non-retired bot (running +
        stopped/off-duty) + deduped per-symbol bars + today's fill markers
        per bot (see ``_fetch_markers``).

        ``version`` is captured into a local right after the increment, not
        read back from ``self._version`` at return time: this hub is cached
        per account and shared by every concurrent caller (REST fallback
        poll racing an SSE stream, multiple SSE clients), and ``_fetch_markers``
        below awaits real I/O — during that gap another concurrent call can
        also increment ``self._version``. Reading the instance field again at
        construction time would let a later call's increment leak into this
        call's response (two responses claiming the same version, or this
        call's genuinely-older payload inheriting a newer number than the
        response that already went out under it) — the same class of
        cross-client race ``known_sids``/``since_marker_keys`` are kept
        caller-local to avoid, just on the hub's own counter instead.
        """
        shown, symbol_bars = await self._fetch_shown_and_bars(since_bar_ms=None)
        self._version += 1
        version = self._version
        as_of_ms = now_ms_utc()
        markers = await self._fetch_markers(shown, now_ms=as_of_ms)
        open_ms, _close_ms = live_window(as_of_ms)
        session_change_pcts = self._fetch_session_change_pcts(
            sorted({row.symbol for row in shown}), open_ms=open_ms
        )
        return GalleryLiveSnapshot(
            stream_epoch=self._epoch,
            surface_version=version,
            as_of_ms=as_of_ms,
            resolution="1m",
            bots=[
                self._project_bot(row, session_change_pcts=session_change_pcts) for row in shown
            ],
            symbols=symbol_bars,
            markers=markers,
        )

    async def build_update(
        self,
        since_bar_ms: dict[str, int],
        *,
        known_sids: set[str],
        since_marker_keys: dict[str, set[str]] | None = None,
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
        because it stopped running.

        ``markers_delta`` mirrors this same caller-local discipline via
        ``since_marker_keys``: the *caller's own* set of already-delivered
        ``event_key``s per sid (``None``/omitted meaning "nothing delivered
        yet"). Like ``known_sids``, this cursor is **not** hub state — a
        hub-shared cursor would let one concurrently-connected client's poll
        consume a fill before another client ever received it (the same
        cross-client race ``removed_sids`` avoids by keeping ``known_sids``
        caller-owned). A fill already in the caller's delivered set is never
        resent — see ``_markers_delta`` for why this is keyed on
        ``event_key``, not ``filled_at_ms``.

        ``version`` is captured into a local right after the increment — see
        ``build_snapshot`` for why reading ``self._version`` back at return
        time (after the ``_fetch_markers`` await) would be a race.
        """
        shown, symbol_bars = await self._fetch_shown_and_bars(since_bar_ms=since_bar_ms)
        current_sids = {row.strategy_instance_id for row in shown}
        removed_sids = sorted(known_sids - current_sids)
        self._version += 1
        version = self._version
        as_of_ms = now_ms_utc()
        markers = await self._fetch_markers(shown, now_ms=as_of_ms)
        markers_delta = _markers_delta(markers, since_marker_keys)
        open_ms, _close_ms = live_window(as_of_ms)
        session_change_pcts = self._fetch_session_change_pcts(
            sorted({row.symbol for row in shown}), open_ms=open_ms
        )
        return GalleryLiveUpdate(
            surface_version=version,
            as_of_ms=as_of_ms,
            symbols=symbol_bars,
            markers_delta=markers_delta,
            bots_delta=[
                self._project_bot(row, session_change_pcts=session_change_pcts, model=GalleryBotDelta)
                for row in shown
            ],
            removed_sids=removed_sids,
        )
