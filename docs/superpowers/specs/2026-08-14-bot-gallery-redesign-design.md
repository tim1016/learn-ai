# Bot Gallery redesign — design spec

- **Status:** Draft for review
- **Date:** 2026-08-14
- **Author:** Inkant (design), paired with Claude
- **Supersedes:** the tile/layout/toolbar decisions in
  `docs/superpowers/specs/2026-08-11-bot-gallery-design.md` (SP1). SP0's
  *chart-engine* choice (lightweight-charts) is **explicitly reversed** for the
  gallery here — see §3.1.
- **Source of truth for the visual target:** the design mock
  `galleryredesign.html` (provided by the user; a self-contained canvas
  prototype). Where this spec and the mock disagree, the mock wins on look; this
  spec wins on how it binds to real data.

---

## 1. Why

The gallery shipped (PR #1544) as a functional 20-tile wall, but three things
diverge from how the user wants to *watch a book of bots*:

1. **The charts don't read like the rest of the desk.** lightweight-charts
   reserves a price-scale gutter, labels outside the plot, a separate volume
   pane, and a standard last-value label. The user's mock deliberately escapes
   all of that — inside faint labels, a floating last-price tag on the last
   drawn bar, a ghost volume band, sparse inside time labels. These are
   *anti-library* traits; no charting library renders them without the same
   custom canvas drawing the mock already contains (Chart.js/PrimeNG, ECharts,
   and D3 were each evaluated and rejected — see §3.1).
2. **Fill markers are invisible — and, in fact, never sent.** `gallery_hub`
   hard-codes `markers={}` in both the snapshot and update (a documented
   deferral). Even if populated, the frontend draws buy/sell in the *candle
   colours* (green/red), so they camouflage into the bars.
3. **The wall only shows running bots**, so a bot the operator just stopped
   vanishes — you lose sight of exactly the thing you were watching. The wall
   should show the whole book (running + stopped/off-duty) with a filter.

This redesign is a near-total rewrite of the four gallery frontend files plus
two thin backend slices. It is intentionally aggressive: the user has approved
throwing away the current tile/chart/layout code.

## 2. Decisions (settled with the user)

| # | Area | Decision |
|---|---|---|
| D1 | **Chart engine** | **Custom canvas renderer**, per-tile. Retire lightweight-charts *for the gallery* (it stays in Strategy Lab / `TradingChartComponent`). Rationale + rejected alternatives in §3.1. |
| D2 | **Fill markers** | First-class, always drawn. **Buy → `--accent` #2962ff (blue) triangle below the bar; Sell → `--warn` #ff9800 (orange) triangle above.** Theme tokens, chosen to contrast the green/red candles. Backend must populate them. |
| D3 | **Wall scope** | Show **all** bots (running + stopped/off-duty). **Retired** stays **off-wall by default** (archive; a Resume affordance on a retired tile would be a lie). |
| D4 | **Asset identity** | Render the canonical asset identity without a gallery-added ring, glow, pill, or border. Status remains available through the Stop/Resume action and the chart region's accessible attention hint. |
| D5 | **Layout** | **Uniform auto-fit** — column count auto-chosen to keep each chart near a target aspect ratio; last row stretches to fill (no dead cells). **Drag-reorder + Reset layout kept; per-tile resize removed.** |
| D6 | **Tile readouts** | Minimal 24px header (asset icon + symbol + strategy-on-hover + icon-only Stop/Resume + rightmost drag grip). Header controls are borderless. Stats live in a **top-left chart legend: Δ% · fills · P&L**; on hover the legend swaps to the hovered bar's **OHLCV**. No separate live quote. |
| D7 | **Filter** | **Single-select segmented control in the footer**: `All · Running · Needs attn · Stopped`. |
| D8 | **Asset icon** | Reuse the existing **`AssetIdentityComponent`** (`app-asset-identity`) for the per-symbol brand icon, with a compact gallery-local logo size and no added frame. |
| D9 | **Selection border** | The mock's amber `is-selected` border becomes the **keyboard-focus / active-tile** affordance, not a persistent selection model. |
| D10 | **App bar** | Out of scope — the Market Scope shell is consumed, not changed. |

## 3. Architecture

```
bot-gallery-page  (route host: broker/accountId inputs; owns GalleryLiveStore;
  │                loading/error/empty/ready; owns action pipeline — unchanged)
  │  ── removes: <h1> + top toolbar. The page is just the dock now.
  │  ── passes:  connection status into the dock's footer ●Live indicator.
  ▼
bot-gallery-dock  (the wall: status filter + uniform auto-fit layout + reorder +
  │                pagination + footer with Reset + filter + Today·1m·Live + pager)
  │  ── owns:    filter state (a footer/wall concern) — filters received bots
  │              before layout + pagination; keeps the page thin.
  │  ── removes: per-tile resize + CSS-grid spans.
  │  ── layout math moves to lib/gallery-layout.ts (rewritten, order-only).
  ▼
bot-tile          (compact unframed identity + custom-canvas chart + legend + markers)
  │  ── uses:    lib/candle-renderer.ts  (pure drawing + geometry)
  ▼
lib/candle-renderer.ts   (NEW — pure canvas draw fns + geometry, no Angular)
```

Data flow is unchanged end-to-end: `GalleryLiveStore` (SSE snapshot/update +
5s poll fallback) → `bots()` / `barsBySymbol()` / `markersBySid()` signals →
dock → tile. The store's contract types (`gallery.types.ts`) are unchanged;
only the **producer** of `markers` (backend) and the **scope** of `bots`
(backend) change.

### 3.1 Chart engine — why custom canvas (D1)

The mock's chart traits are each unattainable in a charting library without
writing the same canvas/plugin drawing code:

| Trait | Library reality |
|---|---|
| Price labels *inside* the plot, no gutter | Every lib reserves an axis gutter; you hide the axis and hand-draw labels. |
| Floating tag on the **last drawn bar** | Custom overlay / plugin in every lib. |
| Ghost volume **band** (not a pane) | Overlay axis tricks + custom colour. |
| Sparse inside time labels | Hide axis, hand-draw. |

Evaluated and rejected: **lightweight-charts** (keeps a gutter; can't inside-label
— the SP0 engine, now reversed for the gallery only); **PrimeNG/Chart.js**
(needs `chartjs-chart-financial` + date adapter + ~4 custom plugins = the mock's
drawing anyway, plus 2–3 deps and 20 heavy instances); **ECharts** (already
benchmarked 3–4× worse on this exact streaming page in SP0 — 90 MB vs 30 MB heap,
399 ms vs 133 ms mount — and *still* needs custom `graphic` work); **D3** (SVG =
~4k live nodes reflowing per tick; canvas = the custom renderer + an unnecessary
`d3-scale` dep). The custom renderer is ~90 lines of pure geometry, zero charting
deps, the lightest option on the one page built for performance, and reusable as
a `candle-sparkline` elsewhere.

### 3.2 `lib/candle-renderer.ts` (NEW)

Pure, framework-free, unit-testable. **The component owns the DOM/canvas,
resize, and pointer events; this module owns math and pixels.** Consumes
`ChartBar[]` (`start_ms` is `int64 ms UTC`) and `ChartFillMarker[]`.

Exported pure functions (each independently testable — see §9):

- `computeScale(bars, cfg) → { lo, hi, vmax, plot rect }` — price/volume bounds
  with the mock's 8% padding; deterministic.
- `barIndexAtX(x, scale) → number` — hover hit-testing (crosshair + readout).
- `layoutTag(lastClose, scale) → { x, y }` — clamps the floating price tag into
  the plot so it never clips top/bottom.
- `draw(ctx, bars, markers, scale, hoverIndex | null, cfg)` — the full paint:
  3 grid lines + inside price labels, ghost volume band (bottom `volFrac` =
  0.15), candles at 100% width, **markers** (blue ▲ buy below / orange ▼ sell
  above), 4 sparse inside time labels, dashed last-price line + floating tag
  (colour = session direction: `firstOpen → lastClose`), crosshair when hovering.

Config (`CFG`) carries the mock's constants (`padT/padB/padL/padR`, `volFrac`,
grid alpha, up/down colours) **from the theme tokens**, not hard-coded hex, so
the renderer tracks the Market Scope palette.

**Temporal rigor.** The renderer never parses or stores time. Bars arrive as
`int64 ms UTC`; axis + crosshair time labels are formatted through the shared
chart time formatter already used by the app charts
(`shared/charts/chart-utils` `formatChartAxisTick`), not a new scattered
`Intl.DateTimeFormat`/`DatePipe`. The last-price tag is derived **only from the
last drawn bar's close** — never a separate live quote — so it cannot disagree
with what's painted (`.claude/rules/temporal-rigor.md` display discipline; the
value shown is a rendered artifact, never stored or re-sent).

### 3.3 Tile (`bot-tile.component`, rewritten)

- **Mount:** `afterNextRender` creates the canvas + a `ResizeObserver`; an
  `effect()` repaints on `bars()`/`markers()` change. No per-tick change
  detection through Angular (mirrors the current imperative pattern; zoneless).
- **Header (24px):** `app-asset-identity` `SYMBOL`
  `strategy(hover-reveal)` … icon-only `Stop`/`Resume` `⠿drag`. The action and
  drag controls are borderless, and the projected drag grip is the final
  rightmost header item. Strategy text = `bot().label`, revealed on
  `:hover`/`:focus-within`.
- **Chart body:** canvas + a DOM legend (top-left). Legend default =
  `Δ% · Nf · P&L`; on `mousemove` over the chart it swaps to
  `HH:MM O H L C V` for the hovered bar and reverts on `mouseleave`. A fill on
  the hovered bar also surfaces `SIDE qty @ price`.
- **Action:** Stop (running) / Resume (stopped) with the existing inline confirm
  + focus management, emitting the same `{sid, actionId}` the page already
  handles. No new action endpoint.
- **Navigate:** clicking the chart body still routes to the single-bot detail
  (`…/bots/:sid`); hover crosshair and click-navigate coexist (mousemove paints,
  click routes).

### 3.4 Legend value derivation (D6)

- **Δ%** — session move from the drawn bars: `(lastClose − firstOpen)/firstOpen`,
  coloured by sign; `—` when < 1 bar.
- **fills** — `bot().fills_today` (`—` when null).
- **P&L** — day P&L = `realized_pnl_today + open_pnl`, null-safe (show whichever
  is present; `—` when both null), coloured by sign. Uses the existing
  `fmtSignedCurrency`/`fmtSignedNumber` formatters.

## 4. Layout engine (D5) — `lib/gallery-layout.ts` (rewritten)

Replace the near-square CSS-grid + per-tile spans with the mock's
**aspect-ratio-optimised uniform flex-wrap**:

- `chooseColumns(n, gridW, gridH, gap, headerH) → { cols, rows }` — scan
  `cols ∈ [1, MAX_COLS]`, score each by `|log(chartAR / TARGET_AR)|` (chart AR =
  `tileW / (tileH − headerH)`), penalise out-of-band (`[MIN_AR, MAX_AR]`) and
  empty trailing cells; reject any layout whose tile height is unusably short.
  Constants: `TARGET_AR 2.2`, band `1.9–3.1`, `MAX_COLS 6` (from the mock).
- Tiles are `flex: 1 1 <one-column basis>` so the **short final row grows to
  fill the width** — no dead cell.
- **Persistence simplifies to order only.** `TileLayout` loses `colSpan`/
  `rowSpan`; the persisted value is a per-account **sid order array**
  (`localStorage`, same key scheme + corruption guard). `resetLayout` clears it,
  reverting to catalog order. Reorder via CDK `cdkDropList` (kept). **All
  resize code is deleted** (the pointer-capture handlers, the `ResizeSession`,
  the corner handle).
- **Pagination kept** (`GALLERY_PAGE_SIZE = 20`), but applied to the **filtered**
  list; `chooseColumns` runs on the current page's tile count.

## 5. Filter (D7) + Footer

The page loses its `<h1>`/top toolbar entirely and declares the route as a
full-bleed workspace. One compact 8px gallery inset replaces the shell/page
padding stack, and the grid owns all remaining vertical space above a single
**32px footer**:

```
[Reset layout] | [ All · Running · Needs attn · Stopped ] | Today · 1m · ●Live | ‹ page x/y ›
```

- **Single-select** status segments with counts. `All` = running + stopped;
  `Running`, `Stopped` are mutually exclusive status buckets; `Needs attn` is a
  cross-cut view (running bots with `needs_attention`). Default segment: **All**.
- Filter state is a signal on the **dock** (a footer/wall concern); it filters
  the received `bots` before layout + pagination, so the page stays thin (store
  + action pipeline + view states). Empty filtered result → a small honest empty
  note in the dock ("No bots match this filter"), distinct from the page's
  whole-wall empty state ("no bots at all").
- The connection status (`connecting`/`live`/`stale`/`error`) folds into the
  `●Live` indicator (green live / amber stale / muted connecting), replacing the
  old top-right status label.

## 6. Status actions (D3, D4) — backend + frontend

The wall's status is derived, not a new field. From the existing
`GalleryBotView` (`running`, `needs_attention`, `desired_state`, `phase`):

| Condition | Header action | Accessible status signal |
|---|---|---|
| `running && !needs_attention` | ■ Stop | Stop action label |
| `running && needs_attention` | ■ Stop | Chart-region `needs attention` hint |
| `!running` (stopped / off-duty) | ▶ Resume | Resume action label |

`primary_action` already encodes Stop-vs-Resume (`action_id = "stop" if running
else "resume"` — `gallery_hub._primary_action`), so stopped tiles get Resume for
free once non-running bots are in scope. **Retired** bots are excluded upstream
(§7); they never reach the action table.

## 7. Backend slices

### 7.1 All-bots scope (D3)

`gallery_hub.py` + `broker_v2_gallery.py`:

- `_fetch_running_and_bars` — stop filtering to `running`; include every
  non-retired catalog row. Rename accordingly.
- `running_symbols` → **all shown symbols**: `ensure_subscribed` + bar-read for
  every shown bot's symbol (a stopped bot still needs today's bars to chart).
  Dedup preserved. *Cost:* more subscriptions when stopped bots hold otherwise-
  unwatched symbols — acceptable; note it in the module docstring.
- **Retired exclusion:** filter out retired rows using the catalog's existing
  lifecycle — `BotCatalogView.phase == RETIRED` (equivalently `status_label ==
  "Retired"`, the closed vocabulary from `catalog_projection_service`) — so they
  never enter the wall or the action table (§6). (Opt-in retired view is a
  deliberate non-goal for this PRD — §11.)
- `removed_sids` semantics (router `_gallery_event_source` + `hub.build_update`):
  a bot that **stops** is no longer "removed" — it stays on the wall. Only a
  bot that leaves the catalog (retired/deleted) is removed. Update the roster
  diff + the docstrings that say "stopped tile stuck forever" (that reasoning was
  for the running-only model).

### 7.2 Fill-marker population (D2)

`gallery_hub.build_snapshot`/`build_update` currently set `markers={}`
/`markers_delta={}`. Populate them by **reusing** the canonical fill→marker
projection — do **not** redefine it:

- Source: `app.broker.alpaca.clerk.fills.FillRecord` per bot (today's window),
  the same SQLite-native source the single-bot detail chart uses.
- Projection: `chart_projection_service._fill_to_marker` (already builds
  `ChartFillMarker` from a `FillRecord`) and its window filter `_markers_in_window`.
  Promote these to an importable helper if they're currently module-private, with
  a provenance note naming the canonical file (single-source rule, CLAUDE.md #5).
- Snapshot: `markers = { sid: [markers…] }` for shown bots.
- Update: `markers_delta` carries only fills newer than the caller's cursor
  (same incremental discipline as bars).

Colour is a **frontend** concern (D2) — the backend emits neutral
`ChartFillMarker`s (`side`, `quantity`, `price`, `filled_at_ms`, `order_ref`);
the renderer maps `buy→--accent`, `sell→--warn`.

## 8. Accessibility

- Every interactive control keeps an accessible name (Stop/Resume already do;
  drag handle stays `aria-hidden` pointer-only — the **known keyboard-reorder
  gap carries over** and is explicitly out of scope, with the always-available
  catalog order as the fallback, unchanged from today).
- Status does not rely on a decorative colour ring: the header action (Stop vs
  Resume) and — for `needs_attention` — a text/`aria` hint carry it.
- Legend and tag are decorative duplicates of accessible data; the tile's
  `aria-label` still names symbol + sid for the navigate target.
- Filter segments are a labelled single-select group (radio semantics).

## 9. Testing

- **Renderer geometry (`candle-renderer.spec.ts`)** — golden-fixture style per
  `numerical-rigor.md`: for a fixed `ChartBar[]` input, assert `computeScale`
  bounds, `barIndexAtX` hit-testing at known x's, and `layoutTag` clamping, with
  explicit tolerances. Pixel-paint is smoke-tested (draws without throwing on
  empty / single-bar / all-flat inputs); geometry is asserted numerically.
- **Layout (`gallery-layout.spec.ts`)** — `chooseColumns` picks the expected
  cols across sizes; short-final-row flex basis; order-only persistence
  round-trips + corruption guard (kept from today, spans removed).
- **Filter + status actions** — the page's filter predicate is unit-tested
  across all four buckets; tile tests cover Stop/Resume and the accessible
  needs-attention hint.
- **Tile (`bot-tile.component.spec.ts`)** — Testing Library: renders symbol,
  reveals strategy on hover, shows Resume for a stopped bot, emits the action on
  confirm, legend swaps to OHLCV on chart hover.
- **Backend** — `gallery_hub` now includes stopped bots + populates markers;
  `pytest` asserts a stopped bot survives an update (not in `removed_sids`) and
  that `markers`/`markers_delta` carry the reused `_fill_to_marker` output.
- Project-scope lint + suites green before push (`ruff`, `eslint --max-warnings 0`,
  the touched pytest + vitest surfaces); thermo review before the first PR of
  each slice per CLAUDE.md.

## 10. Proposed slices

Backend-first so the frontend has real data to build against; each is an
independently shippable PR with its own tests + thermo gate.

1. **S1 — Backend: all-bots scope.** Relax the running filter, keep stopped
   tiles, subscribe all shown symbols, fix `removed_sids` semantics, exclude
   retired. (§7.1)
2. **S2 — Backend: fill markers.** Populate `markers`/`markers_delta` via the
   reused `_fill_to_marker`/`FillRecord` source. (§7.2)
3. **S3 — Frontend: `candle-renderer.ts`.** Pure renderer + geometry tests. (§3.2)
4. **S4 — Frontend: tile redesign.** New `bot-tile` on the renderer — ring,
   asset-identity, hover-strategy, legend + hover-OHLCV, recoloured markers,
   Stop/Resume, last-price tag. (§3.3)
5. **S5 — Frontend: layout engine.** AR-optimised uniform flex-wrap; delete
   resize; order-only persistence; reorder + reset. (§4)
6. **S6 — Frontend: page + footer + filter.** Remove toolbar/h1; footer with
   Reset + single-select status filter + Today·1m·Live + pager; wire filter +
   status derivation to the all-bots store. (§5, §6)

## 11. Risks & non-goals

- **Non-goal: keyboard reorder/resize.** Resize is deleted; reorder stays
  pointer-only (pre-existing gap, catalog order is the fallback).
- **Non-goal: opt-in Retired view.** Retired is off-wall, full stop, for this
  PRD. A later filter bucket can add it (with no Resume affordance).
- **Non-goal: multi-day / per-tile timeframe on the wall.** Still "Today · 1m"
  (detail-page concern, unchanged from SP1).
- **Risk: stopped-bot subscriptions.** Showing stopped bots pulls their symbols
  into the live subscription set; if a large book of stopped bots holds many
  unique symbols, the subscription count grows. Mitigation: the wall is
  paginated (20) and scoped to one account; revisit only if it bites.
- **Risk: renderer ownership.** We own ~90 lines of canvas math. Mitigation: it's
  pure + fixture-tested, no external surface; extractable as a shared
  `candle-sparkline`.
- **Concurrency:** Codex is editing `bot-tile` on another branch; S3–S6 rewrite
  these files. This PRD is developed in an isolated worktree
  (`prd/bot-gallery-redesign` off `origin/master`); the rewrite is expected to
  supersede, not merge with, the concurrent edits.

## 12. Files touched

- `Frontend/.../gallery/lib/candle-renderer.ts` — **new**
- `Frontend/.../gallery/lib/gallery-layout.ts` — rewritten (order-only, AR-fit)
- `Frontend/.../gallery/bot-tile/*` — rewritten (compact unframed header + custom canvas + legend + markers)
- `Frontend/.../gallery/bot-gallery-dock/*` — rewritten (uniform layout, footer + filter, no resize)
- `Frontend/.../gallery/bot-gallery-page/*` — toolbar/h1 removed; filter state
- `PythonDataService/app/services/broker_v2_panel/gallery_hub.py` — all-bots scope + markers
- `PythonDataService/app/routers/broker_v2_gallery.py` — `removed_sids` semantics
- `PythonDataService/app/services/broker_v2_panel/chart_projection_service.py` — promote `_fill_to_marker`/`_markers_in_window` to importable (provenance note)
- Tests alongside each of the above.
- Contract types (`gallery.types.ts`, `broker_v2_gallery.py` schemas) — **unchanged**.
