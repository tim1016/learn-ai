# SP1 — Bot Gallery (live 20-bot wall) — Design

**Date:** 2026-08-11
**Status:** Design, pending user review → implementation plan
**Author:** brainstorming session (Claude + user)

## Context & problem

Today, seeing how each running bot is doing means opening its single-bot detail
page one at a time (`/brokers/:broker/accounts/:accountId/bots/:sid`). The user
wants a **gallery**: one page that auto-divides its grid by the number of running
bots and shows **up to 20 live candlestick charts at once**, so the whole fleet is
visible without drilling in. The forcing function is scale — 20 simultaneous,
tick-by-tick live charts — which the current single-bot data path cannot serve.

This spec is **SP1** of a decomposed effort. It is preceded by **SP0** (a chart-engine
spike, done) and followed by **SP2** (single-bot detail redesign — the four original
critiques) and **SP3** (operator-lens improvements). SP1 is self-contained and shippable.

## SP0 result (engine decision — settled)

A throwaway benchmark rendered synthetic candlestick+volume tiles streaming tick
updates, across three architectures at 1→25 tiles (raw data in the session record):

| @ 20 tiles | lightweight-charts / tile | ECharts / tile | ECharts single-instance |
|---|---|---|---|
| JS heap | **30 MB** | 90 MB | 113 MB |
| Mount | **133 ms** | 399 ms | 426 ms |
| P95 frame | **47 ms** | 65 ms | 59 ms |

Memory and mount (measurement-symmetric) favor **lightweight-charts by 3–4×**.
ECharts' `setOption`-per-tick reprocessing is its streaming weakness; its `matrix`
layout is elegant but rides the same render path. **Decision: build tiles on
lightweight-charts, per-tile** — the engine we already own (also used by
`TradingChartComponent`/Strategy Lab), so no new dependency and one charting stack.
The real 20-bot bottleneck is the **data layer**, addressed below.

## Decisions (settled with user)

| Area | Decision |
|---|---|
| Chart engine | lightweight-charts, one thin instance per tile |
| Gallery scope | Bots with `running == true` for the account in the route |
| Tile click | Navigates to that bot's single-bot detail page |
| Entry point | New route `…/gallery` + a "Gallery" toggle on the bots list page |
| Tile anatomy | **B**: header (identity + live price/Δ) · chart (candles+volume; fill markers deferred, see below) · footer (realized/open P&L + fills) |
| Dock model | **Resizable dock**: auto near-square grid → drag-reorder + resize-to-span |
| Dock engine | **Angular CDK drag-drop** (already a dep) + custom corner resize; **no** gridster |
| Overflow (>20) | **Paginate** in pages of 20 (nothing hidden) |
| Layout persistence | `localStorage`, keyed per account; "Reset layout" button |
| Tile actions | One posture-appropriate **quick action** (Resume/Stop), guarded (see §7) |
| Live data | **New aggregated backend SSE** (`GalleryHub`) — one connection, per-symbol dedup |
| Timeframe | Shared **Today · 1m** for the whole wall (multi-day history is SP2) |

## Goals / non-goals

**Goals**
- One page showing all running bots (up to 20/page) as live, tick-by-tick candlestick tiles.
- A single aggregated live stream that scales to 20 tiles without hitting the browser's
  ~6-connections-per-host limit.
- Drag-reorder + resize dock, persisted per account.
- Per-tile quick action with the same safety/guards as the detail page.

**Non-goals (SP1)**
- Single-bot detail-page redesign (SP2), operator-lens changes (SP3).
- Multi-day history / per-tile timeframe controls on the wall (detail-page concern).
- Cross-account "fleet wall", pop-out windows, saved dashboards (future).
- New market-data infrastructure — we reuse the existing per-symbol `LIVE_BAR_AGGREGATOR`.

## 1. Backend live-data map (from exploration)

- Per-bot live endpoints are **Python** (`PythonDataService/app/routers/broker_v2_panel.py`,
  mounted `/api/brokers`). The per-bot `live-stream` is a **heavy full-snapshot** SSE
  (entire panel + chart) via a ref-counted `SurfaceHub` — not runnable ×20.
- **No aggregated bar stream exists.** The only fleet SSE
  (`/api/live-instances/fleet/stream` → `FleetRosterSnapshot`) carries process/readiness
  roster only — **no symbol, no OHLCV**.
- **Enabler:** live bars are already centralized **per symbol** in a shared singleton
  `LIVE_BAR_AGGREGATOR` (`app/services/live_bar_aggregator.py`): one IBKR subscription per
  symbol, `ensure_subscribed(symbol)` idempotent, `snapshot(symbol, since_ms)`. So 20 bots
  on N distinct symbols cost N subscriptions, not 20.
- Bar schema `ChartBar` (`app/schemas/broker_v2_panel.py`): `start_ms:int`, `end_ms:int`
  (int64 ms UTC), `open/high/low/close: str` (exact decimals over the wire), `volume:int`,
  `source: "ibkr"|"polygon"|"mixed"`. Fills `ChartFillMarker`: `filled_at_ms:int`,
  `side`, `quantity`, `price`, `order_ref`. **Reuse these as-is** (temporal-rigor clean).
- Running roster from `catalog` (`BotCatalogView`): `strategy_instance_id`, `symbol`,
  `running`, `strategy_label`, `realized_pnl_today`, `open_pnl`, `fills_today`,
  `needs_attention`, `last_activity_at_ms`.

**Data lineage (informs SP2 point 3):** execution/account = **Alpaca**; live chart bars =
**IBKR** (via `LIVE_BAR_AGGREGATOR`); history = **Polygon**. The wall's liveness depends on
the IBKR public session — the *same* dependency the current single-bot live chart has.

## 2. Architecture & data flow

```
catalog (running bots)     ─┐
per-bot Clerk stats/fills   ├─► GalleryHub (NEW; account-scoped; ref-counted)
LIVE_BAR_AGGREGATOR ────────┘     • filter running • dedupe symbols
   (per-symbol IBKR, shared)      • compose versioned snapshot + deltas
                                             │
                            ONE SSE  GET …/gallery/stream
                                             │
                            GalleryLiveStore (Angular signals)
                            barsBySymbol() · statsBySid() · markersBySid() · status()
                                             │
                            BotGalleryDockComponent (CDK drag + custom resize)
                                             │
                            BotTileComponent × ≤20  (own lightweight-charts instance;
                            chart updated imperatively OUTSIDE Angular change detection)
```

## 3. Backend design — the one new surface

**Router** — a **new** `app/routers/broker_v2_gallery.py` (keeps `broker_v2_panel.py` from
growing and mirrors the live-control router-freeze discipline; router only validates/parses
and calls the hub):
- `GET /api/brokers/{broker}/accounts/{accountId}/gallery/snapshot` → bootstrap `GalleryLiveSnapshot`.
- `GET /api/brokers/{broker}/accounts/{accountId}/gallery/stream` → SSE, modeled on
  `stream_fleet_roster` for framing/versioning.

**`GalleryHub`** (new service, `app/services/broker_v2_panel/gallery_hub.py`), account-scoped
and ref-counted like `SurfaceHub`:
1. Read `catalog` → keep `running == true` → `{sid, symbol, label, stats}`.
2. Dedupe symbols → `LIVE_BAR_AGGREGATOR.ensure_subscribed(symbol)` once per symbol.
3. Compose a **versioned document**:
   - `bots`: roster + `realized_pnl_today`, `open_pnl`, `fills_today`, `phase`,
     `desired_state`, `running`, `needs_attention`, `last_bar_at_ms`, and the current
     posture-appropriate `primary_action` (id + enabled + disabled_reason) for the tile CTA.
   - `symbols`: `{ SYM: { bars: [today's 1m ChartBar tail] } }` (shared across bots on SYM).
   - `markers`: `{ sid: [ChartFillMarker today] }` — **deferred in v1**: `GalleryHub` always
     emits `markers={}`/`markers_delta={}`; the contract field exists but per-bot fill-marker
     population is a fast-follow, not shipped SP1 behavior.
4. **Latest-wins**: `event: snapshot` (full) on connect; `event: update` **deltas**
   (per-symbol new/changed bars via `since_ms`; new fills; changed bot stats; roster
   add/remove); `: keepalive` 15s; `event: reset` (`reason: epoch_changed`); `event: end`.

**Schemas** (Pydantic v2, snake_case): `GalleryLiveSnapshot`, `GallerySymbolBars`,
`GalleryBotView`, `GalleryBotDelta`; reuse `ChartBar`/`ChartFillMarker`. All timestamps
`int64 ms UTC`.

**Bar resolution:** 1m (matches `LIVE_BAR_AGGREGATOR.snapshot` + current live pane). 5s is
out of scope for the wall (heavier; belongs to the zoomed detail page).

**Boundary discipline** (`live_instances.py` router freeze does not apply here — different
router — but the pattern is the model): the router validates/parses and calls the hub; all
composition logic lives in `GalleryHub`. Quick actions reuse the existing per-sid action
endpoint (`POST …/bots/{sid}/actions`) — the gallery does **not** add a new action path.

**Contract & tests:** new endpoints ⇒ regenerate the committed OpenAPI contract
(`export_openapi_contract.py`; this is a CI gate that plain pytest won't catch). Tests:
`GalleryHub` composition (running filter, symbol dedup, delta emission, epoch/version,
reset), and an endpoint test via `httpx.AsyncClient` + `ASGITransport`.

## 4. Frontend — components (all new; tile deliberately separate & minimal)

- **`BotTileComponent`** — thin lightweight-charts wrapper: candlestick + volume histogram +
  native crosshair, wired for fill markers (`createSeriesMarkers`) against the always-empty
  `markers` the hub emits in v1 — see the deferred note above; the plumbing is in place, the
  data isn't yet. Anatomy **B**: header
  (identity + live price/Δ from the last bar), chart, footer (realized/open P&L + fills).
  Reuses the shared `TickerQuoteComponent`? — **no** at tile scale (too tall); a compact
  inline header instead, but the `receiptLabel`/formatting conventions still apply. Holds its
  own `IChartApi`; subscribes to its symbol's bar signal + its sid's markers/stats; updates
  the chart **imperatively** in an `effect` (never re-render the component per tick). Click on
  the chart body → router navigate to the detail page; the footer quick-action is a separate
  hit target.
  - *Not* the heavy `TradingChartComponent` (indicator picker, sub-panes) — minimal per-tile
    cost is the point (SP0).
- **`BotGalleryDockComponent`** — CDK `cdkDropList` (free-drag reorder) + **custom** corner
  resize handles (CDK has no resize) that set `grid-column: span C / grid-row: span R`;
  auto near-square default (`cols = ceil(sqrt(n))`), row-major placement; "Reset layout".
  Owns the layout model and persistence.
- **`BotGalleryPageComponent`** — route host; reads `broker`/`accountId` route inputs;
  `provides` `GalleryLiveStore`; renders toolbar (timeframe label, page controls, reset) +
  dock; handles loading / empty / feed-stale / error states.
- **`GalleryLiveStore`** (component-provided service) — one `EventSource` (model:
  `account-desk-holdings-store.service.ts`); parses snapshot/delta/reset into signals:
  `bots()`, `barsBySymbol()`, `markersBySid()`, `status()`, `error()`; 5s poll fallback if the
  stream drops (mirrors `BotPanelLiveStore`). Bars keyed by **symbol** and shared, so two
  bots on SPY reference one bar slice (rendered in each tile).

**Change-detection strategy (the 20-tile crux):** tiles are `OnPush`; the store parses SSE
off the render path; per-tick chart mutation is imperative via `effect`, so streaming does
not walk the component tree. lightweight-charts already coalesces multiple `update()` calls
into a single rAF paint, so tick-by-tick fidelity is safe without an explicit throttle.

## 5. Dock, pagination & persistence

- **Layout model:** per-account ordered list of `{ sid, colSpan, rowSpan }`. Default = auto
  near-square, all `1×1`. Persist to `localStorage['gallery-layout:{accountId}']`.
- **Pagination:** running bots (in the persisted order; new bots appended) are chunked into
  **pages of 20**. Reorder/resize apply within the current page; "showing page X of Y". A bot
  that stops leaves the roster (its tile disappears; order compacts).
- **Reset layout:** clears the stored layout → back to auto grid, order = catalog order.

## 6. States

- **Loading:** skeleton tiles (grid sized to expected count).
- **Empty (0 running):** honest empty state + link to the roster/deploy page.
- **Feed stale/degraded:** tile keeps its last bars and shows a "delayed"/source marker
  derived from the stream (`source` / overlay notices) — never fabricated; matches the
  repo's error-authoring guidance (state what/why, no fake "try again").
- **Per-tile attention:** `needs_attention` → header badge.

## 7. Tile quick action (safety)

- Exactly **one** action per tile, posture-appropriate: **Resume** if paused/stopped,
  **Stop** if running (the backend supplies `primary_action` = id + enabled + disabled_reason,
  so the tile never re-derives posture — consistent with the execution-posture model).
- **Guarded:** clicking opens a tiny inline confirm ("Stop SPY · Aug11-02?") to prevent
  dense-wall mis-clicks; only the confirm dispatches.
- **Routed through the existing pipeline:** `BrokerV2PanelService.runBotAction(...)` →
  `POST …/bots/{sid}/actions` — identical guards/receipts to the detail page; no new action
  surface. Disabled-with-reason when blocked (e.g., admission/readiness gate).
- **Optimistic + reconciled:** the action button shows a pending state until the stream
  reflects the new `desired_state`/`running`.

## 8. Temporal & numerical rigor

- All timestamps `int64 ms UTC` end-to-end (bars, fills, `as_of`); no ISO/`DateTime` on the
  wire (temporal-rigor). The tile renders instants viewer-local via the shared display
  convention; there are no date-anchored values on the wall.
- No new math: the tile displays server-computed P&L/prices; the chart plots server bars. No
  golden-fixture obligation (no ported math); this is a rendering/transport feature.

## 9. Testing

- **Backend (pytest):** `GalleryHub` unit tests (running filter, symbol dedup, snapshot vs
  delta, version monotonicity, epoch reset); endpoint test (`httpx.AsyncClient`); OpenAPI
  contract regenerated & committed.
- **Frontend (Vitest + Testing Library):** `GalleryLiveStore` (parse snapshot/delta/reset,
  fallback poll); `BotTileComponent` (renders identity/P&L, chart mounts, click navigates,
  quick-action confirm → dispatch, disabled-with-reason); `BotGalleryDockComponent`
  (auto-division for N, drag reorder persists, resize span persists, reset, pagination).

## 10. Risks & mitigations

- **IBKR session dependency** for live bars (execution is Alpaca). Same as today's single-bot
  live chart; surface feed-stale honestly, don't fabricate.
- **20 lightweight-charts instances** — SP0 shows this is fine (~30 MB, 133 ms mount).
- **SSE payload size** — mitigated by delta framing (only changed bars/fills/stats after the
  initial snapshot) and per-symbol bar sharing.
- **Dock/pagination interaction** — scoped: reorder/resize within the current page; global
  order persisted; documented above.

## 11. Suggested vertical slices (for the implementation plan)

1. **Backend GalleryHub + `/gallery/snapshot`** (no stream yet): compose roster + per-symbol
   bar tails + markers; unit + endpoint tests; OpenAPI contract.
2. **`/gallery/stream` SSE**: snapshot + deltas + reset/keepalive; ref-counting.
3. **`GalleryLiveStore` + `BotTileComponent`** (static grid, no dock): one live tile, then N.
4. **`BotGalleryDockComponent`**: auto-grid → CDK reorder → custom resize → persistence → reset.
5. **Pagination + states** (empty/loading/feed-stale/attention).
6. **Tile quick action** (guarded, via existing action pipeline).
7. **Route + "Gallery" toggle** on the bots list page; end-to-end verify.

## Follow-ups (separate specs)

- **SP2** — single-bot detail redesign: unify status/telemetry header, fix execution-badge
  collisions, clarify feed lineage (IBKR-live/Polygon/Alpaca), right-panel/table controls.
- **SP3** — operator-lens: kill status redundancy, action bar, restructure columns, sortable
  key/value panels, readiness icon cues.
