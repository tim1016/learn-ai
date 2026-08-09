# Strategy Lab redesign — Workbench UX + shared synced chart

**Date:** 2026-08-09
**Status:** Draft for review
**Scope:** Spec 1 of 2. This spec covers the **Strategy Lab** (renamed Engine Lab) redesign and the extraction of a **shared TradingChart component**. A follow-up **Spec 2 (Data Lab TradingView reskin)** is captured at the end but is out of scope here.

---

## 1. Context & goal

The page at `/engine` — today `LeanEngineComponent` (a 1,572-line component under `Frontend/src/app/components/lean-engine/`) — is a strategy-validation workbench that runs one strategy across the Python and LEAN engines and renders a persisted, backend-authored verdict + metrics + charts.

The user's direction: this page is a **strategy *diagnostic***, not a strategy showcase or a deploy gate. It should be renamed **Strategy Lab**, its configuration reduced to four clean inputs with everything else folded into Advanced, its results condensed and self-explaining, and its equity/price charting rebuilt so the equity curve and any indicators share the candlestick chart's **exact time axis and gridlines** (which they do not today). Deep strategy detail and promotion-to-deploy belong on the separate Strategy Validation page.

**Goal:** a calm, dense, self-documenting diagnostic page whose chart is a first-class, TradingView-style, time-aligned multi-pane view — built on a chart component that Data Lab will later share.

## 2. Non-goals (this spec)

- The Data Lab reskin (CSV-export modal, removing the Data Lab validation report, MACD+Volume defaults, full-screen chart). → **Spec 2.**
- Changing the backend run/verdict/parity pipeline, the engines, or any numerical computation. This is a **frontend + presentation** redesign. The only backend touch permitted is **persisting/exposing additional run configuration fields** if History-repopulation (§9) needs them.
- Re-authoring metric math. Metric *definitions/formulas* shown in help are **documentation of the existing engine computation**, not a new implementation (§7).
- Promotion / deploy-gate UX (moves off this page entirely).

## 3. Naming & routing

- Page title **"Engine Lab" → "Strategy Lab"**; header icon/subtitle updated to the diagnostic framing ("Run one strategy across Python / LEAN and read what it did").
- Primary route **`/strategy-lab`**, with **`/engine`, `/lean-engine`, and `/lean-lab` redirecting** to it. Documentation lives at `/strategy-lab/docs`; both `/engine/docs` and legacy `/engine-docs` redirect there, preserving metric fragments such as `#sharpe`.
- Run detail: **`/strategy-lab/runs/:id`**, with `/engine/runs/:id` redirecting.
- Two tabs only: **Workbench** and **History**. The current dynamic "strategy detail" third tab is **removed** — deep strategy detail is a Strategy Validation concern.

## 4. Config rail (the four inputs)

A **trimmed left rail** (the user's chosen form factor) holding **four flat inputs**, an **Advanced** disclosure, and the **Run** button. It collapses to an icon strip; collapsed, an "‹ expand config" affordance recalls the settings.

**The four inputs (flat controls — no nested `config-section` panels, no mismatched padding):**
1. **Engine** — `python | lean | both` (compatibility pair).
2. **Instrument** — symbol picker (from `ticker-range-picker`'s instrument card).
3. **Time window** — from/to + presets. Strategy Lab does not request the optional per-day availability strip, while the shared `time-window-card` retains that capability for Data and Research consumers.
4. **Strategy** — a **light picker** only (name + select). No params-heavy form, no description panels. If a strategy exposes parameters, they live in **Advanced**, not the primary input.

**Advanced (Strategy Lab is leaner than Data Lab):**
- **Sampling** — fixed **1-minute, shown read-only** (not editable).
- **Strategy parameters** — the JSON-Schema-driven param form, relocated here from the primary Strategy input.
- **Execution** — fill mode (`signal_bar_close | next_bar_open`), initial cash, commission/order.
- **LEAN** — validation-template summary + launcher status/command (only when `engine !== 'python'`).

**Collapse / recall behavior:**
- Before a run: rail expanded, showing the four inputs + Advanced + Run.
- On first completed run: rail auto-collapses to the icon strip so results take the full width (preserve today's `localStorage` "pinned open" override).
- Collapsed rail is always re-expandable to **recall the exact configuration** that produced the visible results.

## 5. Run flow

- **Remove the pre-run "ready to run / deployment validation" banner entirely.** Before a run, the results area shows the honest empty placeholder (kept) — current config chips + data-policy note, no fabricated metrics.
- The rail's primary button reads **"Run validation"** (or "Run both engines" for `both`, "Check launcher" when a LEAN launcher is blocked).
- **Live status** while running stays as the SSE-driven phase line (fetching bars → consolidating → simulating → computing stats → persisting → done), rendered as a slim status strip — not the old promotional banner.

## 6. Verdict line (post-run headline)

Replaces the banner + the standalone readiness card with **one slim line**:
- Grade chip from the backend-authored `RunVerdict` (e.g. `B · 72`), colored band (green/amber/red).
- Headline "Diagnostic complete".
- Terse outcome context: trade count and **parity state** (`parity: agree | diverged | pending | unavailable`). When `diverged`, the parity token is **clickable** to expand the divergence breakdown (the existing parity panel / `DivergenceCategory`), otherwise parity stays a single word.
- A `↻ re-run` affordance.
- **No restated configuration** (engine/symbol/window/strategy) — that is recalled from the rail, not duplicated here.

The **standalone "Production readiness / deploy-gate" score card is dropped.** The composite grade survives only as the verdict chip; promotion is a Strategy Validation concern.

## 7. Condensed metrics + "how it's calculated" help

**Layout:** a compact metric strip grouped into three labeled rows:
- **Returns** — Net P&L, Profit factor, Expectancy.
- **Risk-adjusted** — Sharpe, Sortino, Max drawdown.
- **Activity** — Win rate, Trades.

Each tile is a small value + micro-label. Positive/negative tiles get sign coloring. The seven KPI "hero cards" of today collapse into this strip.

**Help ("?") — chosen interaction: click → inline popover.** Every meaningful metric carries a small "?" that, on click, opens an inline popover next to the tile containing:
1. A one-line plain-English **definition**.
2. The actual **formula** (monospace).
3. A **"full definition →"** link into the metric docs (`/strategy-lab/docs#<metric>`, migrated from today's `/engine-docs#<metric>` anchors already referenced by `metric-grade.util.ts`).

**Source of the definitions/formulas (numerical rigor):** the popover text is **documentation of the engine's existing computation**, not a re-derivation. Each metric's formula string cites the canonical computation site (Python engine stats module / LEAN adapter). The existing plain-English guidance already lives in `metric-grade.util.ts` (`gradeSharpe`, `gradeSortino`, …); this spec **surfaces and extends it with formulas**, and adds a provenance comment naming the computation source so the doc text can't silently drift from the math. No golden fixtures change.

## 8. The chart — shared TradingChart component

### 8.1 What it is

A new **presentation-only** Angular component (working name `TradingChartComponent`, `Frontend/src/app/shared/trading-chart/`) built on TradingView `lightweight-charts` v5 (already the repo's charting library). It renders a **stacked, time-aligned multi-pane chart**:

- **Price pane** — candlesticks + trade markers (buy `arrowUp` / sell `arrowDown`, colored by WIN/LOSS with PnL labels) + overlay line series (e.g. the strategy's EMAs) + an optional volume histogram.
- **Equity pane** — the persisted equity-curve envelope as an area series.
- **N indicator panes** — one per oscillator-style indicator (RSI, MACD, …).

### 8.2 The alignment requirement (core of the fix)

Today Engine Lab renders the candlestick chart and the equity chart as **two independent charts**, each calling `fitContent()` separately → their x-axes drift. Data Lab syncs the **logical range** across panes but the price pane and sub-panes still **do not draw the same vertical gridlines / time ticks**.

`TradingChartComponent` must guarantee, across **every** pane:
1. **Logical-range sync** — adopt Data Lab's proven pattern: `subscribeVisibleLogicalRangeChange` ⇄ `setVisibleLogicalRange`, guarded by a single `_isSyncing` reentrancy flag + `requestAnimationFrame` to prevent ping-pong.
2. **Pixel-aligned axis column** — every pane pins the same right-price-scale `minimumWidth` so drawing areas start at the same x.
3. **Shared vertical gridlines & one time axis** — the panes render the **same vertical time gridlines at the same x**, and only the bottom pane shows the time axis. This is the piece neither page has today: the equity dip and an indicator reading line up under the exact candle that drove them.

### 8.3 Data-source-agnostic inputs

The component takes **normalized signal inputs** and owns all chart lifecycle/sync — it does **not** fetch. Inputs (shape to be finalized in the plan): `candles`, `volume?`, `overlays[]` (name + points + style), `subPanes[]` (name + series + reference levels), `markers[]`, and `visibleRange`. Every timestamp remains canonical `int64 ms UTC` in flight and on the wire. Conversion to Lightweight Charts seconds happens only in the immediate chart adapter as `timeMs / 1000`; fractional seconds are retained so distinct millisecond points do not collide at whole-second precision.

Strategy Lab feeds it from the **persisted run** (bars via `GET /api/engine/bars` from the run's `DataPolicy`, trades → markers, equity envelope → equity pane, strategy indicators → overlays/panes). Data Lab will feed it from `/api/chart/data` in Spec 2.

### 8.4 Chart header & the indicator rail (expand-to-reveal)

- **Header** carries the rich **ticker component** (`ticker-quote`) — symbol + price + change — plus the timeframe chips and an **expand ⛶** control.
- **Embedded (in results):** price + equity (+ strategy overlays), shared gridlines, **no indicator rail**.
- **Expanded (full-screen):** the chart takes over and the **indicator picker rail appears on the right** (reusing the shared `indicator-picker`), listing **Active** indicators (removable chips) and an **Add** list. Newly added indicators render as synced overlays/panes. The rail is present **only in the expanded state** — "the tab only appears when the panel is expanded."

### 8.5 Default indicators (Strategy Lab)

On first render the chart auto-overlays **the strategy's own signal indicators** (e.g. EMA-12/26 for `ema_crossover`), shown as removable chips — you open on exactly what the strategy trades on. (Data Lab's default of **MACD + Volume** is Spec 2.) Indicator→pane placement (overlay vs sub-pane) is decided server-side, as in Data Lab today.

### 8.6 Shared-implementation note

`TradingChartComponent` is authored by lifting Data Lab's proven sync logic into a reusable, source-agnostic component. **Spec 1: Strategy Lab consumes it** (replacing the unsynced dual-chart `engine-chart`). **Spec 2: Data Lab migrates to it** and the old `data-lab-chart` is deleted. The interim two-implementation state is deliberate and short-lived (avoids destabilizing Data Lab mid-Strategy-Lab-work); it is tracked as a Spec-2 deletion so we return to one canonical chart, per CLAUDE.md guiding-philosophy #5.

## 9. Deep-dive sections & History

**Deep-dive (below the chart, collapsed by default):** LEAN statistics, Fee & sequencing analytics, Validation atlas, Trade ledger — each a collapsible section, collapsed on load (the "lower documentation area"). They reuse today's components (`LeanStatisticsComponent`, fee drawer, `ValidationAtlasComponent`, `TradeLedgerComponent`) rewrapped; no re-implementation.

**Page order (post-run):** verdict line → condensed metric strip → chart → collapsed deep-dive sections.

**History tab → repopulate config + render results.** Selecting a historical run:
- Loads that run's **full configuration** back into the rail controls (engine, instrument, time window, strategy + params, Advanced execution settings) — the rail stays collapsed but is recallable.
- Renders that run's **persisted results** (verdict, metrics, chart) via the shared `RunReportComponent`.
- Makes **re-run** meaningful (tweak a loaded config and re-run).
- **Dependency:** repopulation needs the run to persist the full config set. The run already records `DataPolicy` (symbol/resolution/dates). **Verify strategy params + execution settings (fill/cash/commission) + engine are persisted and query-exposed; add the missing fields to `StrategyExecution`/the detail query if not.** This is the one backend touch this spec may require; if fields are missing, that's a small additive migration (hand-mirrored DDL per the repo's `EnsureCreated` gotcha).

## 10. Component architecture & decomposition

The 1,572-line `LeanEngineComponent` is decomposed (thermo-review will require this) into focused components:

- **`StrategyLabComponent`** (shell) — page header, two tabs, orchestrates config ⇄ run ⇄ results; owns form signals + job dispatch. Much smaller than today's shell.
- **`StrategyLabConfigRailComponent`** — the four flat inputs + Advanced + Run; collapse/recall; emits a run request.
- **`StrategyLabVerdictComponent`** — the verdict line (+ parity expand).
- **`StrategyLabMetricsComponent`** — condensed grouped strip.
- **`MetricHelpPopoverComponent`** — definition + formula + docs link; fed from `metric-grade.util.ts` (extended with formulas + provenance).
- **`TradingChartComponent`** (shared, §8) + a thin **Strategy-Lab chart adapter** mapping run data → chart inputs.
- **Deep-dive sections** — collapsible wrappers around the existing analytics components.
- **`RunReportComponent`** (kept, refactored to the new layout) remains the single render source for both the Workbench post-run stage and `/strategy-lab/runs/:id`, so they cannot diverge.

Old surfaces removed/retired: the dynamic strategy-detail tab, the standalone readiness/deploy-gate card, the pre-run promotional banner, and (in Spec 1) the unsynced `engine-chart` dual-chart.

## 11. Data flow

1. Config rail (signals) → **Run validation** → `JobsService.startJob(...)` (Python) / LEAN job → SSE phase line.
2. On completion → persisted run id → `RunReportComponent` fetches the run (`BACKTEST_RUN_DETAIL_QUERY`, polling while parity pending).
3. `RunReportComponent` renders verdict line and metric strip; the Strategy-Lab chart makes one atomic `/api/engine/chart` request that reads the run's exact policy-keyed bar store and computes strategy indicators through the canonical engine implementations before adapting them to `TradingChartComponent`.
4. History select → set config signals from the run + route to `RunReportComponent`.

## 12. Testing

- **Vitest + Angular Testing Library**, asserting rendered output, mocking services at the DI level (repo standard).
- **Config rail:** four inputs render; Advanced holds sampling (read-only), params, execution; collapse/recall works; time-window availability strip is gone.
- **Verdict line:** grade + parity render; parity expands only when diverged; no config text duplicated.
- **Metric help:** "?" opens a popover with definition + formula + docs link; a test pins that each metric's formula string names its computation source (drift guard).
- **TradingChart:** given fixture candles + equity + one sub-pane, assert every pane shares one visible logical range and the same gridline x-positions after `fitContent`; adding an indicator adds a synced pane; the indicator rail is present only in expanded state. (Chart-internal lightweight-charts calls exercised via the component's public inputs.)
- **History repopulation:** selecting a run sets the config signals to the run's recorded values and renders its results.
- **A11y:** the page passes AXE; "?" and expand controls have accessible names; WCAG AA contrast in the dark chart theme.
- **Metric formula evidence:** the user-visible formula registry is covered by `contracts/fixtures/strategy-metric-help-golden-v1.json`, a tolerance-pinned canonical Python test, and `docs/references/strategy-metric-help.md`.

## 13. Risks, dependencies, open questions

- **History repopulation persistence gap (dependency).** Must confirm the persisted run carries strategy params + execution settings + engine. If not, a small additive backend change is in scope; flag before implementation.
- **Interim two-chart duplication.** Deliberate; tracked for deletion in Spec 2. Risk: divergence if Spec 2 slips — mitigated by keeping `TradingChartComponent` source-agnostic from day one.
- **Server-driven indicator panel placement.** Strategy Lab must send its (strategy/user) indicators through the same `/api/chart/data`-style contract that assigns `panel`/`type`, or the engine-bars path must return equivalent placement metadata. Confirm the endpoint that serves Strategy-Lab indicators during planning.
- **Metric formula authorship.** Formulas are documentation; they must match the engine's actual computation (including annualization factor, downside-deviation definition for Sortino, drawdown convention). Cite the source per metric.
- **Decomposition size.** Splitting the 1,572-line shell is substantial; sequence so the page stays runnable between slices.

## 14. Follow-up — Spec 2 (Data Lab TradingView reskin) — captured, not in scope

- Migrate Data Lab to `TradingChartComponent`; delete `data-lab-chart`; deliver the shared-gridline fix to Data Lab too.
- **Remove the Data Lab validation / data-quality report** from the main screen.
- **CSV export becomes a modal.** Main screen shows no CSV settings — just an "Export CSV" button. The modal holds all export settings (VWAP/transactions/warm-up columns, row order/sort, bundle-as-ZIP). Pure data-inspection main screen.
- Resolve the seam where `session` / `forward-fill` / `adjusted` affect **both** chart and CSV: chart-affecting toggles move to the chart's own Advanced controls; pure CSV-column toggles live in the export modal.
- Data Lab default indicators: **MACD + Volume**.
- Full-screen TradingView aesthetic + `ticker-quote` header.
