# Bot Gallery (SP1) — Overnight autonomous-build decisions log

**Started:** 2026-08-11 late evening, user asleep. Task: finish the SP1 bot-gallery build
(plan `docs/superpowers/plans/2026-08-11-bot-gallery.md`), run an independent
thermo-nuclear-code-quality-review + implement findings, then open a PR ready for CodeRabbit.

This log records every decision I made without you. Skim it in the morning; anything you
disagree with is cheap to change (it's all on branch `feat/bot-gallery`, not merged).

## Environment / setup decisions
- **Branch, not worktree.** `compose.yaml:87` bind-mounts `./PythonDataService/app` from the
  main repo path, so a git worktree would be invisible to the running containers. Working
  in-place on branch **`feat/bot-gallery`** off `master`.
- **Python tests run on the HOST venv** (`PythonDataService/.venv`, py3.12, has fastapi/
  pandas/pytest/ruff), NOT in `polygon-data-service` — `tests/` isn't mounted to that
  container and it OOMs on pytest. Recipe: `cd PythonDataService && .venv/bin/python -m pytest <test>`.
- **Frontend tests** run scoped in `my-frontend` via exact spec paths (dir globs sweep
  scss/html and fail the build).
- SDD ledger: `.superpowers/sdd/2026-08-11-bot-gallery/progress.md` (git-ignored).

## Per-task decisions

### Task 1 — Gallery DTOs (`app/schemas/broker_v2_gallery.py`) — COMPLETE
- **`GalleryBotDelta` inherits `GalleryBotView`** (self-contained delta carrying `symbol`/
  `label`), resolving a contradiction between the plan's Interfaces list and its sample code.
  Rationale: simpler client merge (replace bot by `sid`); trivial byte cost. Plan updated to match.
- Deferred minors (cosmetic): delta test could add `isinstance` assert; `resolution` has a
  default `"1m"` (harmless).

### Task 2 — GalleryHub snapshot composition (`services/broker_v2_panel/gallery_hub.py`) — COMPLETE
- **Bar-mapper `aggregator_bars_to_chart_bars` was extracted into `chart_projection_service.py`**,
  not `panel_chart_data_source.py` as the plan's comment assumed — that's where the real
  aggregator-bar→`ChartBar` mapping actually lives (verified). Reasonable correction.
- Added `or 0.0` / `or 0` guards in `_project_bot` because `BotCatalogView` pnl/fills are
  genuinely `Optional` but `GalleryBotView` requires non-optional values.
- **DEFERRED — fill markers.** `build_snapshot` emits `markers={}`. Per-bot fill-marker
  population from the Clerk projection is not yet wired. Consequence: gallery tiles render
  candles + volume + live price + P&L, but **no buy/sell dots** in v1. Rationale for deferring:
  markers need per-bot fill data (heavier fan-in); candles+P&L are the core monitoring value;
  keeping v1 shippable overnight. Tracked as a known limitation; fast-follow to wire markers
  through the hub (and the frontend already reads `markersBySid`, so it lights up when the
  backend populates it). **If you want markers in v1, this is the one scope cut to reverse.**

### Task 3 — GalleryHub deltas (`build_update`) — COMPLETE
- `build_update(since_bar_ms)` re-projects the full running roster each call (no per-bot dirty-tracking) and tracks `self._last_sids` for `removed_sids`. Approved. Deferred minors: an inaccurate-but-harmless return-type hint (no mypy gate); one test name overstates (the `since_ms` fake ignores its arg, but the hub forwards it correctly — verified in review).

### Task 4 — Gallery SSE endpoints + OpenAPI contract — COMPLETE (after fix round 1)
- New router `app/routers/broker_v2_gallery.py`: `GET .../gallery/snapshot` + `GET .../gallery/stream` (SSE), framing copied from the existing `stream_live_snapshot_scoped`/`stream_fleet_roster`. Wired in `main.py` behind the same data-plane read protection as the sibling broker routes. OpenAPI contract regenerated + committed.
- Production deps: `catalog_source=panel_data_source`, `aggregator=LIVE_BAR_AGGREGATOR`. Hub is cached per `(broker, account_id)`.
- **Test approach:** `httpx.ASGITransport` can't partial-read an infinite SSE, so the stream tests drive the generator's `body_iterator` directly (documented). Snapshot test uses `app.dependency_overrides` on `get_gallery_hub` with fakes.
- **Fix round 1 (review-driven):** (a) made `get_gallery_hub` `async def` — a sync dep runs in a threadpool, making the hub-cache check-then-set non-atomic; a gallery load fires snapshot+stream concurrently and could build two divergent hubs. Now matches the codebase's `live_projection.py` async pattern. (b) Added tests for the `reset`-on-stale-cursor branch (previously untested). (c) Reworded a docstring that overstated "emit only on change."
- **DEFERRED (tracked):** `bots_delta` is full-roster, so the stream emits an `update` ~every 1s while any bot runs (not only on change). Acceptable for v1 — a live wall's forming bar changes each second anyway — but per-bot dirty-tracking is the obvious optimization. Same root as the Task-2 markers deferral family.

### Task 5 — Frontend gallery live store + TS types — COMPLETE (after fix round 1)
- `gallery/lib/gallery.types.ts` (hand-written `Gallery*` TS mirrors; reuses existing `ChartBar`/`ChartFillMarker` from `broker-v2-panel.types.ts`) + `gallery-live-store.service.ts` (one `EventSource`, component-provided, signals `bots`/`barsBySymbol`/`markersBySid`/`status`, 5s poll fallback, generation-guarded async). Merge: bars by `start_ms` (forming-bar replace + append + sort), markers dedupe by `order_ref`, bots upsert by `sid`, drop `removed_sids`. Modeled on `account-desk-holdings-store.service.ts`.
- **Fix round 1 (review-driven):** (a) a malformed SSE frame no longer wedges connection `status` on `'error'` — parse errors decoupled from transport health (matching `BotPanelLiveStore`); (b) added a same-identity-restart test (state preserved); (c) tightened bar/marker signal typing to `readonly` arrays so consumers can't corrupt store state; (d) replaced an empty `catch {}` in bootstrap with explicit handling (repo hard-rule).
- Deferred minor: `reset` re-fetches via REST even though the stream re-emits a snapshot on the same connection (harmless; arbitrated by the monotonic guard).

### Task 6 — BotTileComponent (lightweight-charts tile) — COMPLETE (after fix round 1)
- Thin tile: header (state dot + symbol + label + live price/Δ%), lightweight-charts candle+volume+markers (imperative `effect` + `afterNextRender`, cleaned up on destroy — outside change detection), footer (realized/open P&L + fills), single guarded quick action (confirm → emit; disabled-with-reason), tile-body click → detail route.
- **Fix round 1:** confirm dialog now has focus-on-open + Escape-to-cancel (mirrors `TypedHaltConfirmComponent`); `toCandle` extracted to shared `lib/chart-bar-mapping.ts` (dual-pane-chart refactored to import it — no more duplication); mapping fns exported + unit-tested; Space key `preventDefault`.
- Deferred: `setData` full-replace vs incremental `update` (fine at ~390 bars/tile; optimize if the wall struggles); `needs_attention` border-tint vs badge (cosmetic); 20 doc-level Escape listeners on a full wall.

### Task 7 — Layout model + BotGalleryDockComponent — COMPLETE (after fix round 1)
- `gallery/lib/gallery-layout.ts` (pure): `autoDivision` (ceil-sqrt), `paginate` (20/page, clamped), `load/save/resetLayout` (localStorage per account, guarded, type-checked). `BotGalleryDockComponent`: CDK drag-reorder (handle-scoped, page-relative→global index translation), custom pointer resize → grid spans, pagination (20/page + prev/next), Reset, persistence through one `persist()` seam; forwards bars-by-symbol/markers-by-sid/action to `<app-bot-tile>`.
- **Fix round 1:** clamp `page` when the roster shrinks below the current page (was "page 2 of 1" + dead Next); added a cross-page-reorder regression test (page-2 drop must not scramble page 1); drop-time guard for the one-tick `pageBots`/`pageTiles` divergence; deleted dead `trackBySid`; catch comment.
- Deferred: grid sized from total roster (stable column width across pages — debatable, kept); no keyboard resize (WCAG 2.1.1 gap — layout-only, lower stakes; a11y follow-up).

### Task 8 — Gallery page host + route + list toggle + states — COMPLETE (after fix round 1)
- `BotGalleryPageComponent` (route `brokers/:broker/accounts/:accountId/gallery`, lazy, component-input-bound; provides `GalleryLiveStore`, `start()` on an effect + `stop()` on destroy). Toolbar = "Today · 1m" + connection status (Reset + pagination stay in the dock, not duplicated). States: loading skeleton / honest empty ("No running bots" + roster link) / non-blocking stale indicator / error banner. "Gallery" link added to the bots-list page.
- **Decision — action needs a preflight:** `GalleryPrimaryAction` is a lean projection lacking the `revision`/concurrency token `runBotAction` requires, so `onAction` does ONE `getPanel` fetch per confirmed click to get the current `PanelAction`, then submits — reuses existing endpoints, no new action surface (verified necessary in review; the sibling roster path has the full token inline and needs no preflight). **Follow-up option:** source `primary_action` from the catalog's `row_action` (which already carries the full token) inside `GalleryHub` to eliminate the preflight entirely — a small Task-1/Task-2 revisit.
- **Decision — state order:** error is checked before empty because the store's invariant is `status==='error' ⟹ epoch==='' ⟹ bots===[]` (verified), making the brief's literal empty→error order dead code.
- **Fix round 1:** threaded VISUAL optimistic-pending page → dock → tile (mirrors `bots-list-page` `pendingBotIds`) — the tile's action button now disables + `aria-busy` while an action is in flight, closing the brief's "optimistic pending" requirement (was reentrancy-guard-only).

<!-- appended as the build proceeds -->
