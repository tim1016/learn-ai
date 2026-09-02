# Strategy Lab one-page workbench — design

**Status:** approved-in-conversation (operator, 2026-09-01). Every fork in §3 was
put to the operator and answered; the answers are recorded inline as
**Decision** lines and are not open for re-litigation during implementation.

**Predecessors:**

- PRD #1425 / `docs/superpowers/specs/2026-08-09-strategy-lab-redesign-design.md`, which
  specified "`RunReportComponent` stays the single render source (**workbench stage** +
  run-detail route)". The run-detail half shipped; the workbench half never did. This
  design completes that intent with a different arrangement, and in doing so deletes
  `RunReportComponent` rather than embedding it (see §4.1).
- `docs/superpowers/specs/2026-08-12-slim-rail-and-fullbleed-shell-design.md`, which
  established the `data: { fullBleed: true }` route contract this page already uses.

## Goal

Make Strategy Lab one page: configure, run, and read the result without navigating.

Acceptance claim:

> Running a validation from `/strategy-lab` populates the chart in place on the right
> and the run's statistics under the configuration on the left, without unmounting the
> configuration or the in-flight runner; the configuration collapses on completion
> without writing the operator's saved collapse preference; and every existing deep link
> to a persisted run (`/strategy-lab/runs/:id`, `/engine/runs/:id`, `?restoreRun=N`)
> resolves to that same page with that run loaded.

## 1. What exists today

`/strategy-lab` (`StrategyLabComponent`) is a two-tab shell. The **Workbench** tab is a
two-column grid — `minmax(260px, 300px) minmax(0, 1fr)` — with the configuration rail on
the left and, on the right, `ValidationStagePlaceholderComponent`: an honest empty state
that never fabricates a preview. That placeholder is the "empty long chart div" this
design fills.

When a run completes, `StrategyLabRunner` **navigates away**
(`strategy-lab-runner.service.ts:351` for Python, `:378` for LEAN) to
`/strategy-lab/runs/:id`. That route renders `StrategyLabResultsComponent` — a
"← Back to workbench" link wrapping `RunReportComponent`, whose layout is the mirror
image of what the operator wants: supplementary statistics and the evidence menu on the
**left**, headline metrics and the chart on the **right**.

Measured live at a 1600×1000 viewport, that results page is 1813px tall: the summary
block alone is 668px and the chart 944px. Nothing about it fits on one screen.

Two mechanisms already exist and are reused rather than rebuilt:

- **The configuration rail already collapses.** `StrategyLabConfigRailComponent` renders
  a `.config-strip` summary (Engine · Instrument · Strategy · Window) when
  `collapsed` is set. The store keeps the transient state (`configNavCollapsed`) and the
  persisted operator preference (`configNavOverride`, localStorage key
  `engineLab.configNavOverride`) in **two separate signals**, which is what makes §4.3
  free.
- **Configuration restore from a persisted run already exists.**
  `StrategyLabComponent.restoreSavedRun` / `restoreConfiguration` (lines 83 and 113)
  rehydrate engine, range, strategy, params, fill mode, cash, commission, and data policy
  from a `BacktestRunDetail`, driven today by the `?restoreRun=` query param.

## 2. The change, in one picture

```text
/strategy-lab?run=204                      height: calc(100dvh − tabs − 36px dock strip)
┌───────────────────────────┬──────────────────────────────────────────────┐
│ left  (overflow-y: auto)  │ right (pinned, does not scroll)              │
│                           │                                              │
│  config rail              │  run / config notices                        │
│    (full │ summary strip) │                                              │
│  ─────────────────────    │  ┌────────────────────────────────────────┐  │
│  run statistics           │  │ placeholder  │  chart  │  chart+overlay │  │
│    evidence grade         │  │                                        │  │
│    headline metrics       │  │  fills the column height               │  │
│    more statistics        │  │                                        │  │
│    evidence menu ▸drawer  │  └────────────────────────────────────────┘  │
└───────────────────────────┴──────────────────────────────────────────────┘
```

Left column widens to `minmax(320px, 380px)`. The right column is the chart and nothing
else — that separation ("left = inputs and numbers, right = evidence") is the organising
rule the rest of the design defers to.

## 3. The forks, and what was decided

**3.1 — How the run id rides the URL. Decision: query param `?run=N`.**

The obvious shape — pointing `/strategy-lab/runs/:id` at `StrategyLabComponent` — is
**fatal**, and this is the load-bearing finding of the investigation. `/strategy-lab` and
`/strategy-lab/runs/:id` are different route configs, so navigating between them destroys
and recreates the component. `StrategyLabConfigStore` and `StrategyLabRunner` are
component-scoped `providers`, so a completing run would tear down its own runner mid-flight
and reset the configuration to defaults — the exact opposite of "one page".

A componentless child route under `/strategy-lab` would work, at the cost of a parent
reading `firstChild.paramMap`. The query param achieves the same thing with less
machinery and reuses an existing mechanism: the store already subscribes to
`queryParamMap` for launch params (`strategy`, `engine`, `symbol`, `from`, `to`,
`resolution`, `tab`), so `run` is one more entry in `parseEngineLaunchParams`.

**3.2 — What lives under the configuration. Decision: all of it.** Evidence grade,
headline metrics (Returns / Risk-adjusted / Activity), "More statistics", and the
Evidence drawer menu all stack in the left column. The right column stays purely the
chart.

**3.3 — What collapsing buys. Decision: statistics take the freed height, and the
configuration auto-collapses when a run completes.** The collapse is transient — see §4.3.

**3.4 — The right pane during a re-run. Decision: keep the previous chart, dimmed, under
a progress overlay.** Nothing is destroyed before its replacement exists; the dimming
prevents a stale chart reading as live.

**3.5 — Configuration when a run is loaded. Decision: snap to that run's inputs**, as
`?restoreRun=` does today, so the configuration always describes the run on screen and
"Run validation" reproduces it. One guard applies — §4.4.

**3.6 — The LEAN source editor. Decision: off the chart column into a drawer, plus a
read-only viewer on Strategy Validation.** §4.6 records why removing it outright was
rejected.

## 4. Design

### 4.1 `RunReportComponent` is split, not embedded

`RunReportComponent` is 293 lines of data derivation wrapped around a layout. The layout
is exactly what this design replaces; the derivations are the valuable part and are
lifted verbatim.

Giving it a `layout: 'stacked' | 'split'` input was considered and **rejected on two
grounds**. First, it cannot work: the workbench needs the statistics *interleaved* with
the configuration in the left column, and a single embedded component cannot place its
own rail beneath a sibling's rail. Second, a component that branches on a layout-mode flag
is precisely the kind of magical abstraction the repo's quality gate exists to catch.

- **New — `StrategyLabRunReport`**, a component-scoped *service* under
  `components/strategy-lab/`, provided by `StrategyLabComponent` alongside the two
  services it already provides. This matches the shell's existing composition pattern
  exactly. It owns:
  - `activeRunId: WritableSignal<number | null>`;
  - the `rxResource` fetch of `BACKTEST_RUN_DETAIL_QUERY` with the pending-parity
    `pollInterval` and its `stopPolling` conditions, moved unchanged;
  - the derivations `run`, `loading`, `loadError`, `verdictEnvelope`, `verdict`,
    `engineResult`, `markers`, `equityPoints`, `reportNotices`, `parity`;
  - the pure helpers `toEngineTrade`, `parseLeanStatistics`, `parseLeanAnalysis`,
    `toParityView`, and the `UNAVAILABLE_REASON_COPY` map.

  It does **not** own the `runDetail` input fast-path: the workbench always has a run id,
  never a pre-fetched detail, so that branch is dropped rather than carried forward dead.

- **New — `StrategyLabRunStatsComponent`**, presentational, the left column's results
  block. Composes the existing `results-summary` (which already contains
  `evidence-grade`), `results-sidebar`, and `deep-dives`, all with unchanged inputs. The
  `p-drawer` inside `deep-dives` is already viewport-positioned, so opening evidence from
  a 360px column needs no change.

- **New — `StrategyLabStageComponent`**, the right column: placeholder, chart, or
  dimmed chart under a progress overlay. Extracted rather than inlined because
  `strategy-lab.component.html` is at 94 lines and the repo caps templates at ~80.

- **Deleted** — `components/engine-lab/run-report/` (4 files; the `engine-lab/`
  directory contains nothing else and goes with it) and
  `components/strategy-lab/results-page/` (4 files).

### 4.2 Layout

`.workbench` becomes a fixed-height two-column grid:

```scss
.workbench {
  grid-template-columns: minmax(320px, 380px) minmax(0, 1fr);
  height: calc(100dvh - var(--strategy-lab-chrome));  // tab list + 36px dock strip
}
.workbench__rail  { overflow-y: auto; }
app-strategy-lab-stage { min-height: 0; }             // lets the chart shrink to fit
```

**Correction, found during final review.** This section originally wrapped the stage in a
`<section class="workbench__stage" aria-label="Strategy evidence">` carrying the run-error
notice. That wrapper is gone: it duplicated the "Strategy evidence" landmark the stage's own
root already declares (tripping axe's `landmark-unique`), and its
`grid-template-rows: auto minmax(0, 1fr)` with a conditionally rendered notice needed a
`grid-row: 2` pin on the stage to place the chart. The run error is now an `error` input on
the stage, rendered beside the report notices inside the stage's own notice container, and
`app-strategy-lab-stage` is the workbench grid's second column directly.

`--strategy-lab-chrome` is declared on `.strategy-lab` in the component's own stylesheet,
not globally — it describes this page's chrome and nothing else's.
`RunDockComponent` is `position: fixed; bottom: 0` with a 36px collapsed strip, so that
strip is reserved in it. Expanded (320px) the dock deliberately overlays — that is its
existing behaviour and is left alone.

`results-summary.component.scss` is restyled to an unconditional single-column stack. Its
current `grid-template-columns: repeat(3, ...)` and the two **viewport**-width media
queries at lines 62–69 are deleted: viewport queries cannot fire for a 360px column on a
1600px screen, and after `results-page/` is deleted this component has exactly one
consumer, which is always narrow. A container query would be speculative generality for a
second consumer that does not exist.

The existing `@media (max-width: 900px)` single-column fallback on `.workbench` is kept
and extended to release the fixed height, so the page reverts to normal document flow on
narrow screens.

### 4.3 Auto-collapse is transient

One `effect` in `StrategyLabComponent`: when `report.run()` yields a run id that differs
from the last one seen, call `config.configNavCollapsed.set(true)`.

It deliberately does **not** call `toggleConfigNav()` and does **not** touch
`configNavOverride`. Because the store already keeps those two signals separate, the
transient collapse and the persisted operator preference stay independent with no new
state and no new persistence. A manual expand afterwards behaves exactly as today
(including writing the preference, which is existing behaviour and correct — that *is* an
operator choice). The next completion collapses again, because completion is an event,
not a setting.

### 4.4 Routing and run loading

- `app.routes.ts`: `strategy-lab/runs/:id` becomes an Angular 22 **redirect function**
  returning a `UrlTree` for `/strategy-lab?run=<id>`. The
  `[routerLink]="['/strategy-lab/runs', id]"` in
  `analytical-manual/metric-reference-entry.component.html` keeps working through it
  unchanged.

  **Correction, found during implementation.** This section originally claimed that
  `engine/runs/:id` — which redirects to `strategy-lab/runs/:id` — would keep working
  "through Angular's recursive redirect application". **That is false** for
  `@angular/router@22.0.8`. `expandSegmentAgainstRouteUsingRedirect` recurses via
  `processSegment(..., false, ...)`, forcing the local `allowRedirects` flag to `false` on
  the re-match, so a redirect whose target is itself a redirect route throws `NoMatch` on
  the second hop and falls through to the `**` wildcard. The legacy deep link would have
  silently died. `engine/runs/:id` therefore redirects **directly** to the final URL
  through the same shared `redirectToStrategyLabRun` function, and a
  `router.navigateByUrl('/engine/runs/204')` test pins the resolved URL — a route-config
  shape assertion could not have caught this.
- `run` is read by a **separate** signal over `queryParamMap`, accepting `?restoreRun=N` as
  an alias so existing bookmarks resolve identically. It is deliberately *not* added to
  `EngineLaunchParams`: `applyLaunchParams` is guarded by `appliedLaunchParamsKey`, so
  folding `run` into that key would make every run load re-fire the whole launch-param
  path — resetting `activeTab` and re-selecting the strategy — which is not what loading a
  run means. Run loading and launch-param application stay separate mechanisms over the
  same query string.
- `StrategyLabRunner` keeps navigating on completion, but to `/strategy-lab` with
  `queryParams: { run: id }` and `queryParamsHandling: 'merge'`. Same route config, so the
  component instance and both stores survive; the URL still identifies the run, and browser
  back/forward still steps through runs.
- History-tab selection navigates the same way and sets `activeTab` to `configuration`.

**The restore guard.** Loading run N snaps the configuration to N's inputs (§3.5). But
`restoreConfiguration` calls `config.restoreStrategy(...)` → `applyStrategy(...)`, which
sets `customLeanSource` to `null` (`strategy-lab-config.store.ts:207`). Without a guard,
finishing a custom-source LEAN run would immediately discard the QCAlgorithm that produced
it. So: when the incoming run id is the one the runner just produced, skip the restore —
the configuration already describes that run, making the restore both redundant and
destructive. The guard is a single `justProducedRunId` field on the runner, consumed once.

### 4.5 The chart fits the viewport

`TradingChartComponent.chartHeight` is today `panes().reduce(sum of PANE_HEIGHTS)` —
470 (price) + 205 (equity) + 185 per indicator pane, so 860–1100px by construction. It
becomes a distribution of *measured available height*:

- The component already runs a `ResizeObserver` (used for width at
  `trading-chart.component.ts:330`); it starts reporting height too.
- Today's constants become **weights**, so relative pane proportions are preserved
  exactly. Each pane gets a floor (120px).
- Below the floor sum, it falls back to today's fixed heights and lets
  `.trading-chart__canvas-wrap` scroll — that element already has `overflow: auto`, so
  short viewports degrade rather than break.
- `.trading-chart` gains `height: 100%`; the workbench grid gives the host a definite
  height. Expanded (fullscreen) mode already uses `100dvh` and is unaffected — normal mode
  simply becomes a shorter version of the same model.

This is safe to change because Strategy Lab is the component's **only** consumer
(`grep` for `app-trading-chart` finds `strategy-lab-chart` and the component's own spec,
nothing else). Data Lab's migration to it, sketched as Spec 2 of PRD #1425, has not
happened.

### 4.6 The LEAN source editor

The editor is load-bearing, not decorative: `customLeanSource` is the **only** route to a
parameterized LEAN run, because `StrategyLabRunner` refuses a LEAN run when any strategy
parameter differs from its schema default unless a custom source is supplied
(`strategy-lab-runner.service.ts:178`) — the bundled templates hardcode their own gates.
Deleting it outright was therefore rejected: it would make LEAN in the Lab permanently
default-parameters-only.

- **In the Lab:** the config rail gains an "Edit QCAlgorithm source" button (shown when
  `engine !== 'python'`) that opens the existing `LeanSourceEditorComponent` in a
  right-side `p-drawer`, reusing the pattern `deep-dives` already uses. Code needs width; a
  360px column does not have it. `customLeanSource` wiring is unchanged.
- **On Strategy Validation:** a new read-only QCAlgorithm-twin viewer beside the existing
  `app-quantconnect-reference-code` block.

**Verification that this is possible, and the constraint it produced.**
`strategy_registry_seeds()` sets `strategy_key=key` directly from
`_STRATEGY_REGISTRY.items()` (`strategy_validation_manifest.py:84`), and
`resolve_strategy_lean_source` looks up `_STRATEGY_REGISTRY.get(strategy_name)`
(`strategy_lean_source_service.py:27`). Same dict — the key spaces cannot diverge.
Confirmed live against all 7 catalog entries: every key resolved as a strategy name, none
returned "Unknown strategy".

But only **3 of 7** have a registered twin:

| `strategy_key` | `GET /api/engine/strategies/{key}/lean-source` |
|---|---|
| `deployment_validation`, `ema_crossover_signal`, `rsi_mean_reversion` | 200 |
| `sma_crossover`, `spy_strategy_a`, `spy_strategy_b`, `spy_strategy_c` | 404 *"has no registered LEAN validation source"* |

So the empty state is the majority case, and the viewer must distinguish a **semantic**
404 (an honest fact about the strategy: `lean_twin is None`) from a **transport** failure
(a system problem). `LeanSourceService.getStrategySource` today rejects on both
identically, and `lean-source-editor.component.html` collapses them into one string,
"Registered QCAlgorithm source is unavailable" — which misreports an unregistered twin as a
broken system. The service gains a discriminated result; both the drawer editor and the new
viewer render from it.

The two artifacts are distinct and the viewer is additive, not duplicative:
`reference_code` is the vendored QuantConnect **audit copy** under `references/qc-shadow/`
(what the port was validated *against*, present for 1 of 7); `lean-source` is the
registered **validation twin** (what Strategy Lab *executes*, present for 3 of 7). Only
`ema_crossover_signal` has both.

## 5. What is deliberately not in scope

- **Persisted custom LEAN twins.** Making an edited QCAlgorithm a saved, receipted
  artifact per strategy — so Strategy Validation could own it outright and the Lab could
  select one — needs a new store, API, and provenance receipts. That is its own PRD.
- **Data Lab's migration to `TradingChartComponent`** (Spec 2 of PRD #1425). §4.5 is
  written so that migration inherits the viewport-fitting height model rather than
  conflicting with it.
- **Comparing two runs side by side.** One active run at a time.
- **Any backend change.** No Python, .NET, GraphQL, or OpenAPI surface moves. Every field
  this design renders is already served.

## 6. Testing

Per `.claude/rules/testing.md`, behaviour over implementation, asserted on rendered output.

**Rewritten:**

- `run-report.component.spec.ts` (236 lines) → `strategy-lab-run-report.service.spec.ts`.
  Its derivation assertions — parity polling and its stop conditions, `engineResult`
  mapping, marker construction, `reportNotices` for each unavailable-evidence branch,
  `toParityView` reason copy — are the valuable content and carry over close to verbatim.
- `strategy-lab.component.spec.ts` — one-page composition: statistics render under the
  configuration after a run, the chart replaces the placeholder, and the shell survives
  completion without remounting.
- `app.routes.spec.ts` — `strategy-lab/runs/:id` is now a redirect function; assert it
  produces `/strategy-lab?run=<id>`, and that both `strategy-lab` and the legacy
  `engine/runs/:id` chain still resolve.

**New:**

- `strategy-lab-run-stats.component.spec.ts` — cluster order and the honest empty state.
- `strategy-lab-stage.component.spec.ts` — the three states, including that a re-run keeps
  the previous chart mounted (dimmed) rather than unmounting it.
- **Auto-collapse:** completion collapses the rail **and** leaves
  `localStorage['engineLab.configNavOverride']` untouched. Both halves asserted; the second
  is the one that would silently regress.
- **Restore guard:** a completing custom-source LEAN run does not null `customLeanSource`.
- `lean-source.service.spec.ts` — semantic 404 and transport failure produce different
  results.

**Amended:**

- `trading-chart.component.spec.ts` — pane heights distribute measured height by weight;
  below the floor, fixed heights return and the wrap scrolls.

**Unchanged** (inputs are untouched): `results-summary`, `results-sidebar`,
`strategy-lab-deep-dives`, `evidence-grade`, `strategy-lab-chart`,
`validation-stage-placeholder`, `metric-help-modal`.

**Deleted:** `strategy-lab-results.component.spec.ts`.

Frontend runs are scoped per `.claude/rules/testing.md` — exact spec files, never
directory globs — but a **full** `ng test` sweep is required before push, because parent
specs in this repo pin child component copy and a scoped run has been shown to miss what
CI catches.
