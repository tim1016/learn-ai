# Recency Chart — design spec (rev 2)

**Status:** Draft for review · **Date:** 2026-08-16 · **Type:** architectural (Frontend, Backend, PythonDataService)

**Rev 2** incorporates the code-review P0/P1 findings (persistence seam, Python-owned statistics, durable launch entity, evidence fingerprint, hard-delete guard, provenance) and the display-mode / Sharpe-opacity additions.

Interactive design mockups (seeded mock data, not a backtest):
- Layout comparison: https://claude.ai/code/artifact/b3758409-3346-4b34-92ea-863bb592b63d
- **Chosen swimlane design (interactive):** https://claude.ai/code/artifact/052d7abd-beae-4376-b13e-ff84ab6f344d

---

## 1. Purpose

Maintain a **persistent, accumulating recency chart** — a source of truth of backtest trades. A user launches a **batch of backtests** (eligible runnable strategies, each swept across parameter ranges, over chosen symbols, for a lookback duration up to Polygon's ~2-year Starter limit). Every trade is **persisted forever** (with Python-authored statistics) and drawn on a **recency timeline**: per-symbol swimlanes where each trade is a bar whose **hue = PnL** and **opacity = its combo's Sharpe**, anchored at *today*. Launches **append**; runs stay until the user **soft-deletes** them.

No new trading math beyond aggregation/statistics that live in Python (per-trade PnL%, holding sessions, total-PnL, Sharpe). The one strategy-math change is parameterizing `ema_crossover_signal` (§7), gated by a golden-fixture parity test.

## 2. Scope & non-goals

**In scope (v1):** configure → launch-once (background job) → persist a recency-owned trade snapshot with Python-authored stats → project the accumulated, deduped, non-deleted trades onto interactive swimlanes with display modes, symbol/strategy toggles, and Sharpe-opacity → soft-delete/restore runs and launches.

**Non-goals (deferred):** recurring/cron scheduling; shorting (long-only; buy=entry, sell=exit); multi-symbol strategies; options-spread and other non-numeric/categorical-param strategies (gated out by a capability flag); cross-symbol portfolio PnL; the consensus/envelope combo view; hard-delete/purge; a product-level run-count cap (engineering safety rails only).

## 3. Key decisions

| # | Decision |
|---|---|
| D1 | **Eligible runnable strategies only.** A `recency_supported` capability gates the picker to long-only equity strategies with numeric params (EMA-signal, EMA-2bps, RSI-MR, SMA, ORB, …). Options-spread / categorical-param strategies are excluded from v1. |
| D2 | **Any tunable is a parameter.** `ema_crossover_signal` exposes `gap`, `rsi_min`, `rsi_max` (defaults preserved — §7). Repo-wide rule going forward. |
| D3 | **"Schedule" = configure & launch once now**, background job (Redis + SSE). No scheduler built. |
| D4 | **Param range input = value-list OR low/high/step**, per numeric param; editor dynamic per strategy from `params_schema.properties`. |
| D5 | **Combos → lane: hero + foldable rest.** Hero = **Python-computed** highest **total PnL** (§7.1) over the **current display window**; recomputed by a debounced query when the window/filters change. Other combos fold behind it. |
| D6 | **Per-symbol trade swimlanes.** Lane = symbol; bar = one trade (entry→exit); length = holding period. |
| D7 | **Overlaps = sub-row packing** (no x-nudge). Highest per-trade PnL on the top sub-row; every bar hoverable/clickable. |
| D8 | **Bar hue = trade PnL** (diverging CMYK: cyan loss ↔ gray 0% ↔ magenta gain, symmetric domain over shown trades). **Bar opacity = the combo's Sharpe** (§7.1), with a minimum-alpha floor. Strategy identity = thin left **cap**. |
| D9 | **Interaction:** hover = contextual tooltip; click = pin into a focus panel with an on-demand **% price sparkline** of the trade window. |
| D10 | **Lane order = by recency** (freshest trade on top). |
| D11 | **No product run-count cap** — but **engineering safety rails**: lazy grid expansion (never materialize a giant list), bounded concurrency, and rejection of only pathological/malformed grids past a very high sanity ceiling. |
| D12 | **Resolution is an execution detail.** Native resolution per strategy; a bar spans the whole trade. |
| D13 | **Time axis:** sticky top ruler while lanes zoom/scroll; recent-window main view + full-range overview strip; rendering virtualized to the visible window; duration on the bar in trading sessions. |
| D14 | **The chart is a persistent, accumulating source of truth** — a projection over a recency-owned trade snapshot. Launches **append**. |
| D15 | **Scope = recency-launched runs only** (tagged via the launch entity). Strategy Lab one-offs / folds never appear. |
| D16 | **Dedup by canonical evidence fingerprint** = symbol · strategy_key · **strategy_code_version** · params_hash · **data policy (adjustment/session/resolution)** · **fill model** · **commissions** · entry_ms · exit_ms. Not params alone. Deterministic conflict handling; a bar maps to a fingerprint that may back several studies (canonical representative = latest; the membership set is returned). |
| D17 | **Soft-delete (must exist), at run and launch granularity.** Sets a tombstone; excludes from projection; retains data; **restorable** (`restoreRecencyRun` / `restoreRecencyLaunch`). A trade disappears only when no surviving run contains its fingerprint. |
| D18 | **Two display modes.** *All-symbols*: window capped to ~1 trading week (~2,500 min, configurable). *Single-symbol*: window up to the full accumulated history. The store holds everything; the mode bounds the view. |
| D19 | **Symbol toggles + strategy-visibility toggles** filter the *view* without deleting; narrowing to one symbol unlocks the long window. |
| D20 | **Durable `RecencyLaunch` entity persisted before dispatch** — config, expected/succeeded/failed counts, status, timestamps, tombstone. Survives a zero-success or cancelled launch and Redis's 24h expiry. |
| D21 | **Recency owns its trade snapshot** with **Python-authored** per-trade `pnl_pct`, `holding_sessions`, `is_synthetic_exit`, plus per-combo `total_pnl` and `sharpe`. .NET never derives a compared number (AGENTS.md #5). Shared `StrategyExecution`/`BacktestTrade` stay structurally untouched. |
| D22 | **Hard-delete guard.** `DELETE /api/studies/{id}` rejects deletion of a recency-member study and routes to recency soft-delete; restrictive FK. |
| D23 | **Python owns business logic; UI renders.** Every statistic (hero total-PnL, Sharpe, pnl_pct, holding sessions) is Python-authored; the UI only maps numbers to geometry/colour/opacity. |
| D24 | **OOS integrity.** Hero is descriptive in-sample optimization — show per-strategy validation status and an explicit "in-sample selection, not out-of-sample proof" caption, especially for experimental strategies. |

## 4. Architecture & data flow

Ownership: **Python owns generation + statistics**; **Postgres/.NET owns the durable recency store + projection**; **Angular renders**.

```
Frontend (research-lab)          Backend (.NET / Postgres)             PythonDataService
──────────────────────          ─────────────────────────             ─────────────────
Config page → POST /api/jobs/recency_chart
   │                       JobsApi: persist RecencyLaunch(config,status=RUNNING)
   │                       BEFORE dispatch → forward /api/jobs-internal/recency-chart
   │                                                                ▼
   │                                                     recency_chart job:
   │                                                       lazy-expand grid (sym×strat×combo)
   │                                                       bounded-concurrency loop:
   │                                                         execute_engine_backtest (native res)
   │                                                         compute stats (pnl_pct, sessions,
   │                                                            total_pnl, sharpe) — Python
   │                                                         POST recency-snapshot persist
   │                                                            (RecencyRun + RecencyTrade[],
   │                                                             fingerprint, atomic, idempotent)
   │  SSE progress ◄── Redis ◄──────────────────────────── emit.progress (i/n, failed)
   ▼                       on child persist: update RecencyLaunch counts;
 on job.completed          honor launch tombstone (skip if cancelled/deleted)
   │ refetch projection
   ▼
 GraphQL recencyTrades(fromMs,toMs,symbols,strategies,mode)
   │   └ join RecencyTrade → RecencyRun(tombstone NULL) → RecencyLaunch(tombstone NULL)
   │     → window filter → dedup by fingerprint (canonical rep = latest)
 GraphQL recencyHero(fromMs,toMs,symbols,strategies)  ← Python-authored total-PnL selection
   ▼
 Swimlane renders: hue=pnl_pct, opacity=sharpe, hero solid, packing, modes, toggles
 Delete: softDeleteRecencyRun / softDeleteRecencyLaunch / restore*  → set/clear tombstone
```

- **Transport** mirrors the `spy_ema_walk_forward` job pattern; the Backend additionally persists the launch row before dispatch (P0-5).
- **Timestamps** are `int64 ms UTC` end to end; sessions/durations derive from the canonical calendar module.

## 4.1 Entities, accumulation & soft-delete

Three new EF entities (+ migration); shared study/trade tables untouched.

- **`RecencyLaunch`** — `Id`, `ConfigJson`, `ExpectedRuns`, `SucceededRuns`, `FailedRuns`, `Status` (RUNNING/COMPLETED/CANCELLED/FAILED), `CreatedAtMs`, `CompletedAtMs?`, `DeletedAtMs?` (tombstone). Persisted **before** dispatch so a zero-success/cancelled launch and its config survive Redis's 24h TTL.
- **`RecencyRun`** — one per `(symbol, strategy, combo)` child: `Id`, `RecencyLaunchId`, `StrategyKey`, `Symbol`, `ParamsJson`, `ParamsHash`, `Fingerprint`, `StudyId?` (best-effort, for "open run"), `Status`, `TotalPnl`, `Sharpe`, `CreatedAtMs`, `DeletedAtMs?`.
- **`RecencyTrade`** — one per trade: `Id`, `RecencyRunId`, `EntryMs`, `ExitMs`, `Side`, `EntryPrice`, `ExitPrice`, `Quantity`, `Pnl` (dollars), **`PnlPct`** (Python-authored), **`HoldingSessions`** (Python-authored), `IsSyntheticExit`, `SignalReason`.

- **Persist path:** the recency job POSTs a recency-snapshot payload; the Backend writes `RecencyRun` + its `RecencyTrade[]` **atomically**, **idempotent by `Fingerprint`** (re-run of the same evidence updates in place, never duplicates — fixes the engine-run no-idempotency gap for this store). A child that fails to persist increments `FailedRuns` and is reported ("N of M failed"), never silently dropped. Child inserts check the launch tombstone and skip if the launch was cancelled/deleted mid-run (fixes the bulk-delete race).
- **Projection (read):** `recencyTrades` joins `RecencyTrade → RecencyRun(tombstone NULL) → RecencyLaunch(tombstone NULL)`, filters `[fromMs, toMs]`, dedups by `Fingerprint` (canonical representative = latest study; membership set returned so "open run"/"delete" are unambiguous). Returns per-trade `{symbol, strategy_key, params_hash, fingerprint, entry_ms, exit_ms, pnl_pct, is_synthetic_exit, side, sharpe, study_id, run_id}`.
- **Hero (read):** `recencyHero` returns, per `(symbol, strategy)` in-window, the combo maximizing Python-authored `total_pnl` over the window — the selection is Python-authored, not client-computed.
- **Soft-delete/restore:** `softDeleteRecencyRun(runId)`, `softDeleteRecencyLaunch(launchId)`, `restoreRecencyRun(runId)`, `restoreRecencyLaunch(launchId)` set/clear tombstones. Nothing hard-deleted in v1. A trash view lists deleted runs/launches.
- **Hard-delete guard (P0-4):** the existing `DELETE /api/studies/{id}` refuses when the study is a live recency member, routing to recency soft-delete; FK is restrictive.

## 4.2 Display modes & performance

- **All-symbols mode:** visible window capped to ~1 trading week (~2,500 min, configurable). Many minute-scale lanes stay legible; the overview strip still spans available history but the main window is bounded.
- **Single-symbol mode:** narrowing to one symbol unlocks a long window (up to the full accumulated history / 2 yr).
- **Virtualization:** only trades in the visible window render.
- **Overview extent/density:** the strip is powered by a **separate lightweight extent/density query** (trade counts per time bucket), not the full windowed trade set (P1).
- **Window edge semantics:** a trade straddling the window boundary is display-clipped but counted by its true span; hero attributes a trade to a window by its **entry_ms** ∈ window.
- **Generation safety (D11):** lazy grid expansion, bounded concurrency, pathological-grid rejection — no product cap.

## 5. Components

### 5.1 Frontend (Angular 22, standalone, OnPush, signals, zoneless)

- **Routes:** `research-lab/recency-chart` (chart + launch), `research-lab/recency-chart/runs` (manage/trash). Lazy; nav under *Backtests*.
- **`recency-chart-config.component`** — symbol multi-select; strategy add/remove from the `recency_supported` subset of `/api/engine/strategies`; dynamic range editor (single | value-list | low/high/step, reusing the config-rail schema form); duration (presets 3/6/12/24 mo + custom, ≤2 yr); Launch with the expanded run count.
- **`recency-chart.store`** — signals for launch config, run count, job state (`JobsService`), the projection (`recencyTrades`), hero (`recencyHero`), and view state (mode, window/zoom, symbol + strategy toggles). Refetches on `job.completed`, window/filter change, and delete/restore.
- **`recency-swimlane.component`** — bespoke SVG timeline: per-symbol lanes by recency; sub-row packing (top = highest per-trade PnL); **hue = pnl_pct**, **opacity = sharpe** (clamped, alpha floor); strategy cap; sticky ruler; mode-aware window + overview strip; virtualized; duration labels; hover tooltip; click-to-pin; hero solid + foldable combos; OOS caption.
- **`recency-trade-focus.component`** — strategy, combo params, entry/exit (shared timestamp component, ET), held sessions, pnl_pct, sharpe, "open run ▸" (canonical representative; membership set available), delete-run action, on-demand price sparkline (shared lightweight-charts helper).
- **`recency-runs.component`** — manage/trash: soft-delete a run or launch, restore.
- **PnL colormap + opacity util** — diverging cyan↔gray↔magenta (symmetric); Sharpe→alpha mapping with a defined clamp/floor. Stops finalized via the `dataviz` validator (light+dark, CVD); win/loss stays readable via the PnL label.

### 5.2 Backend (.NET 10)

- `recency_chart` → `/api/jobs-internal/recency-chart` in `JobTypeRoutes`; **persist `RecencyLaunch` before dispatch**; update counts + honor tombstone as children land.
- New EF entities `RecencyLaunch` / `RecencyRun` / `RecencyTrade` (+ migration); a recency-snapshot persist endpoint (atomic, fingerprint-idempotent).
- **GraphQL (HC v15):** `recencyTrades`, `recencyHero`, `recencyRuns(includeDeleted)`, `recencyLaunches(includeDeleted)`; mutations `softDeleteRecencyRun/Launch`, `restoreRecencyRun/Launch`. `[GraphQLName]`; `AsNoTracking()`; dedup + hero selection server-side (passthrough of Python-authored numbers only).
- **Hard-delete guard** on `DELETE /api/studies/{id}`.

### 5.3 PythonDataService (FastAPI)

- **`app/routers/jobs.py`:** `start_recency_chart_job` (internal); `run_in_thread`; phases (`expand`, `fetch`, `run i/n`, `persist`); cooperative cancel.
- **`app/research/recency/` (new):** `grid.py` (**lazy** `expand_grid`, `params_hash`, fingerprint components); `stats.py` (**Python-authored** per-trade `pnl_pct`/`holding_sessions`, per-combo `total_pnl`, `sharpe` — §7.1); `runner.py` (bounded-concurrency loop over `execute_engine_backtest`, native resolution, `auto_fetch`; compute stats; POST recency snapshot with fingerprint; per-run failure isolation); `schema.py` (`RecencyConfig`, `RunSpec`, snapshot payloads).

## 6. Error handling

- Config validation at the boundary: empty symbols/strategies, ill-formed range, duration > 2 yr, ineligible (non-`recency_supported`) strategy → typed errors.
- Per-run isolation: a failing child is captured/reported ("N of M failed"), never silent, never aborts the batch.
- Launch durability + tombstone race: launch persisted before dispatch; child inserts skip a cancelled/deleted launch.
- Polygon/data: gaps auto-fetch; failures + 5 req/min surface as structured logs; duplicate/non-monotonic bar detection stays fail-fast (temporal-rigor).
- Delete safety: soft-only, restorable; hard-delete of a recency member refused.
- Generation safety: pathological/malformed grid rejected before materialization.

## 7. Parameterizing `ema_crossover_signal`

Expose `gap`, `rsi_min`, `rsi_max` with defaults **exactly** `0.20 / 50 / 70` and the `rsi_min < rsi_max` validator. **Guardrail (ADR 0020 + numerical-rigor):** defaults unchanged ⇒ default-point behavior identical; existing golden/parity fixtures must still pass (the regression gate). A parity test asserts default-config equivalence to the pre-change fixture. See [[feedback_parameterize_tunables]].

## 7.1 Python-authored statistics (numerical authority — AGENTS.md #5)

All computed in Python, documented in `docs/math-sources-of-truth.md` + the engine authority map, tolerance-tested:

- **`pnl_pct`** (per trade) — sourced from the engine's `EngineTradeResponse.pnl_pct` (already Python-authored); persisted, never re-derived in .NET.
- **`holding_sessions`** (per trade) — count of trading sessions from `entry_ms` to `exit_ms` via the canonical calendar module.
- **`total_pnl`** (per combo, per window; the hero metric) — **defined** as the sum of per-trade net realized P&L (account currency, dollars) for the combo's trades whose `entry_ms` ∈ window. Order-independent sum; explicit definition removes the "summed vs compounded vs net vs RoC" ambiguity.
- **`sharpe`** (per combo; opacity encoding) — mean / sample-stddev (ddof=1) of the combo's per-trade returns (`pnl_pct`), computed over the combo's trade history, with a minimum-trades guard (below the guard → neutral/undefined, rendered at the alpha floor). Non-annualized per-trade Sharpe; annualization deferred. UI maps `sharpe → alpha` via a defined clamp + floor (rendering only).

## 8. Testing

- **Python parity (numerical-rigor):** `ema_crossover_signal` at defaults reproduces its golden fixture (`atol=1e-9, rtol=0`).
- **Python stats:** `total_pnl` (window attribution by entry_ms; order-independence), `sharpe` (known series → known value, ddof=1, min-trades guard), `holding_sessions` (calendar-derived across a weekend/holiday) — explicit tolerances.
- **Python unit:** lazy `expand_grid` (list vs low/high/step, ordering, `params_hash`, fingerprint components; pathological-grid rejection); per-run failure isolation; job route via `httpx.AsyncClient`+`ASGITransport`, Polygon mocked.
- **.NET:** recency-snapshot persist is atomic + **fingerprint-idempotent** (re-run updates in place); `RecencyLaunch` persisted before dispatch and counts update; child insert honors launch tombstone; `recencyTrades` dedup-by-fingerprint + window + tombstone exclusion; `recencyHero` selects the max-`total_pnl` combo (passthrough, not recompute); soft-delete→restore round-trip at run + launch; a fingerprint shared by two runs survives deleting one; **hard-delete guard** refuses a recency member; via `IRequestExecutor`.
- **Frontend (Vitest + Testing Library):** config form renders from the eligible subset; range-mode toggles; run count; swimlane renders from a projection fixture; **hue=pnl_pct + opacity=sharpe** mapping; hero solid + fold; hover card; click pins focus; **mode switch** changes the window cap; symbol/strategy toggles filter the view; zoom virtualizes; delete removes a bar and restore returns it; OOS caption present; AXE pass.
- **Colormap:** dataviz validator on the diverging stops (light + dark).

## 9. Delivery slices (tracer-bullet first)

1. **Vertical tracer:** parameterize `ema_crossover_signal` (+ parity); `RecencyLaunch`/`RecencyRun`/`RecencyTrade` entities + migration + atomic fingerprint-idempotent persist; `recency_chart` job (launch-before-dispatch, lazy expand, one strategy + a value-list + 2–3 symbols + fixed 6-mo window); Python stats (`pnl_pct`, `holding_sessions`, `total_pnl`, `sharpe`); `recencyTrades` + `recencyHero`; swimlane render with hue=PnL, opacity=Sharpe, hero + hover + click-pin.
2. **Config surface:** full range editor, eligible-strategy subset, duration, run count.
3. **Display modes & axis:** all-symbols vs single-symbol windows, sticky ruler, overview extent/density query, virtualization, symbol/strategy toggles, duration labels.
4. **Fold/unfold + focus sparkline + open-run** (canonical representative) + OOS caption.
5. **Soft-delete/restore** (run + launch) + trash view + hard-delete guard.

## 10. Resolved (was: open items)

- **Sharpe for opacity = per-combo all-time** (stable). Window-relative discarded.
- **All-symbols window cap = ~1 trading week (~2,500 min), configurable.** Confirmed.
- **Trash/management view ships in v1** (slice 5). Build all slices.
