# Strategy Lab one-page workbench — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse Strategy Lab's workbench and its separate run-results route into one page — chart on the right, run statistics under the configuration on the left, configuration collapsible.

**Architecture:** `RunReportComponent` is split into a component-scoped *service* (all data derivation) plus two presentational components the workbench composes into its two columns. The run id rides a **query param** (`/strategy-lab?run=204`), not a path segment, because a different route config would destroy and recreate `StrategyLabComponent` and tear down its component-scoped config store and runner mid-run. `TradingChartComponent` stops using fixed pane pixel heights and distributes measured available height instead.

**Tech Stack:** Angular 22 (zoneless, signals, standalone, OnPush), Apollo Angular, PrimeNG (`p-drawer`, `p-tabs`), lightweight-charts v5, Vitest + Angular Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-01-strategy-lab-one-page-design.md`

**Worktree:** `.claude/worktrees/strategy-lab-one-page`, branch `feat/strategy-lab-one-page`, based on `origin/master` (`df44c0ee`). The main checkout is shared with other live sessions — **do not `git switch` there.**

## Global Constraints

Copied from the repo rules and the spec. Every task's requirements implicitly include this section.

- **Angular:** standalone (never write `standalone: true` — it is the default), `ChangeDetectionStrategy.OnPush`, `inject()` not constructor injection, `input()`/`output()`/`model()` not decorators, `@if`/`@for`/`@switch` with `track` on every `@for`, `[class.x]`/`[style.x]` never `ngClass`/`ngStyle`, no `mutate()` on signals.
- **TypeScript:** strict. No `any`; use `unknown`. No `as X` without justification.
- **Templates under ~80 lines.** Extract a child component when exceeded.
- **No `console.log`.** No silent catches (`catch {}`).
- **Accessibility:** AXE-clean, WCAG AA. Every interactive control has an accessible name.
- **Test naming:** `*.component.spec.ts`, `*.service.spec.ts`. Assert rendered output, not private signal values.
- **Frontend lint gate:** `npx eslint Frontend/src/ --max-warnings 0` — project scope, zero warnings.
- **Frontend test gate:** run tests on the **host**, from this worktree's `Frontend/` — `cd Frontend && npx ng test [--include=…]`. **Not** `podman exec my-frontend`: that container mounts the *main* checkout shared with other live sessions, so it tests someone else's tree and can report a false green on work that is not in it. Scoped runs during development (`--include='exact.spec.ts'`, never directory globs), and a **full** `ng test` sweep before push, because parent specs in this repo pin child component copy.
- **No backend change.** No Python, .NET, GraphQL, or OpenAPI surface moves in this plan.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File Structure

**Created**

| Path | Responsibility |
|---|---|
| `Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.ts` | All persisted-run data derivation. Component-scoped. |
| `Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.spec.ts` | Derivation tests, ported from `run-report.component.spec.ts`. |
| `Frontend/src/app/components/strategy-lab/run-stats/strategy-lab-run-stats.component.{ts,html,scss,spec.ts}` | Left column's results block. Presentational. |
| `Frontend/src/app/components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component.{ts,html,scss,spec.ts}` | Right column: placeholder / chart / dimmed chart + overlay. |
| `Frontend/src/app/components/strategy-validation/lean-twin-source/lean-twin-source.component.{ts,html,scss,spec.ts}` | Read-only registered QCAlgorithm twin viewer. |

**Modified**

| Path | Change |
|---|---|
| `.../strategy-lab/strategy-lab.component.{ts,html,scss,spec.ts}` | One-page composition, run loading, auto-collapse, LEAN drawer host. |
| `.../strategy-lab/strategy-lab-config.store.ts` | `run` query-param signal; nothing else. |
| `.../strategy-lab/strategy-lab-runner.service.ts` | Navigate by query param; expose `justProducedRunId`. |
| `.../strategy-lab/strategy-lab-config-rail/strategy-lab-config-rail.component.{ts,html,scss}` | "Edit QCAlgorithm source" trigger. |
| `.../strategy-lab/results-summary/results-summary.component.scss` | Unconditional single-column stack. |
| `.../strategy-lab/strategy-lab-chart/strategy-lab-chart.component.scss` | Fill host height. |
| `.../strategy-lab/lean-source-editor/lean-source-editor.component.{ts,html,spec.ts}` | Consume the discriminated source result. |
| `Frontend/src/app/services/lean-source.service.ts` (+ new spec) | Discriminated `LeanSourceResult`. |
| `Frontend/src/app/shared/trading-chart/trading-chart.component.{ts,scss,spec.ts}` | Viewport-fitting height. |
| `Frontend/src/app/components/strategy-validation/strategy-validation.component.{ts,html}` | Host the twin viewer. |
| `Frontend/src/app/app.routes.{ts,spec.ts}` | `strategy-lab/runs/:id` becomes a redirect function. |

**Deleted**

- `Frontend/src/app/components/engine-lab/` (entire directory — `run-report/` is its only content)
- `Frontend/src/app/components/strategy-lab/results-page/`

---

### Task 1: `StrategyLabRunReport` service

Lift every derivation out of `RunReportComponent` into a component-scoped service keyed by a settable `activeRunId`. `RunReportComponent` is left untouched and still compiles; Task 7 deletes it.

**Files:**
- Create: `Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.ts`
- Test: `Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.spec.ts`
- Read for reference: `Frontend/src/app/components/engine-lab/run-report/run-report.component.ts`

**Interfaces:**
- Consumes: `BACKTEST_RUN_DETAIL_QUERY`, `BacktestRunDetail` from `../../graphql/backtest-runs.query`; `EngineResultData` from `../lean-engine/engine-results/engine-results.component`; `StrategyLabParityView`, `parseRunVerdictEnvelope` from `./strategy-lab.models`.
- Produces:
  ```ts
  class StrategyLabRunReport {
    readonly activeRunId: WritableSignal<number | null>;
    readonly run: Signal<BacktestRunDetail | null>;
    readonly loading: Signal<boolean>;
    readonly loadError: Signal<unknown>;
    readonly verdict: Signal<RunVerdict | null>;
    readonly engineResult: Signal<EngineResultData | null>;
    readonly markers: Signal<TradingMarker[]>;
    readonly equityPoints: Signal<TradingPoint[]>;
    readonly reportNotices: Signal<string[]>;
    readonly parity: Signal<StrategyLabParityView | null>;
  }
  export function toEngineTrade(trade: BacktestRunDetailTrade, index: number): EngineTrade;
  export function parseLeanAnalysis(json: string | null): LeanAnalysisFinding[];
  ```

**Design note for the implementer.** `RunReportComponent` has a `runDetail` input fast-path that skips the query when a pre-fetched detail is supplied. **Drop it.** The workbench always has a run id and never a pre-fetched detail, so carrying it forward would be dead code. Everything else moves verbatim, including `pollInterval: 5000` and both `stopPolling()` call sites.

- [ ] **Step 1: Write the failing test**

Create `strategy-lab-run-report.service.spec.ts`. Copy the `makeTrade`, `curve`, and `makeRun` builders verbatim from `engine-lab/run-report/run-report.component.spec.ts` (lines 27–108) — they are correct and complete — then add:

```ts
import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { Apollo } from "apollo-angular";
import { from } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StrategyLabRunReport, toEngineTrade } from "./strategy-lab-run-report.service";

// ... makeTrade / curve / makeRun copied from run-report.component.spec.ts ...

/**
 * `runId: null` leaves the resource idle — that is the "no run selected"
 * state, and it must not reach Apollo. Reading a signal is what makes
 * `rxResource` evaluate its params, so every test touches `report` after
 * awaiting a microtask.
 */
function makeReport(queryResult: unknown, runId: number | null) {
  const stopPolling = vi.fn();
  const watchQuery = vi.fn(() => ({ valueChanges: from([queryResult]), stopPolling }));
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      StrategyLabRunReport,
      { provide: Apollo, useValue: { watchQuery } },
    ],
  });
  const report = TestBed.inject(StrategyLabRunReport);
  report.activeRunId.set(runId);
  return { report, watchQuery, stopPolling };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe("StrategyLabRunReport", () => {
  it("does not query until a run is active", async () => {
    const { report, watchQuery } = makeReport({ data: { backtestRun: null }, loading: false }, null);
    await Promise.resolve();

    expect(report.run()).toBeNull();
    expect(watchQuery).not.toHaveBeenCalled();
  });

  it("uses the producer-authored realized staircase instead of mark-to-market points", async () => {
    const run = makeRun();
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.equityPoints()).toEqual([
      { timeMs: Date.UTC(2026, 0, 5, 15, 0), value: 100_000 },
      { timeMs: makeTrade().exitTimestamp, value: 100_048 },
      { timeMs: Date.UTC(2026, 0, 6, 21, 0), value: 100_048 },
    ]);
  });

  it("keeps buy and sell markers tied to persisted trade outcomes", async () => {
    const run = makeRun({ trades: [makeTrade({ pnL: 0, pnlPts: 0, pnlPct: 0 })] });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.markers()).toEqual([
      expect.objectContaining({ color: "#90a4ae", text: "BUY · BREAK EVEN" }),
      expect.objectContaining({ color: "#90a4ae", text: "SELL · $0.00" }),
    ]);
  });

  it("makes a missing realized curve explicit instead of relabeling mark-to-market evidence", async () => {
    const run = makeRun({
      equityCurve: {
        schemaVersion: 2,
        error: null,
        markToMarket: curve([{ t: 1, e: 100 }], "strategy_bar_close"),
        realized: { cadence: "trade_exit", rawPoints: 0, keptPoints: 0, error: "Realized equity unavailable.", points: [] },
      },
    });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.reportNotices()).toContain("Realized equity unavailable.");
    expect(report.equityPoints()).toEqual([]);
  });

  it("surfaces a malformed persisted verdict as a notice with no verdict", async () => {
    const run = makeRun({ verdictJson: "{}" });
    const { report } = makeReport({ data: { backtestRun: run }, loading: false }, run.id);
    await Promise.resolve();

    expect(report.verdict()).toBeNull();
    expect(report.reportNotices()).toContain("Persisted verdict data is incomplete or malformed.");
  });

  it("does not mislabel a report-query failure as a missing run", async () => {
    const { report, stopPolling } = makeReport(
      { data: undefined, loading: false, error: new Error("Backtest detail query is incompatible with the server.") },
      44,
    );
    await Promise.resolve();

    expect(report.loadError()).toBeTruthy();
    expect(report.run()).toBeNull();
    expect(stopPolling).toHaveBeenCalled();
  });

  it("keeps polling only while a parity verdict is pending", async () => {
    const pending = makeRun({ parityVerdicts: [{ status: "pending", verdictJson: "{}", createdAt: 1 }] });
    const { stopPolling } = makeReport({ data: { backtestRun: pending }, loading: false }, pending.id);
    await Promise.resolve();

    expect(stopPolling).not.toHaveBeenCalled();
  });
});

describe("toEngineTrade", () => {
  it("passes through persisted P&L fields without recomputing them", () => {
    const trade = toEngineTrade(makeTrade({ pnlPts: 7, pnlPct: 0.02, pnL: -1.9 }), 4);
    expect(trade).toEqual(expect.objectContaining({ trade_number: 5, pnl_pts: 7, pnl_pct: 0.02, result: "LOSS" }));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-run-report.service.spec.ts'
```

Expected: FAIL — `Cannot find module './strategy-lab-run-report.service'`.

- [ ] **Step 3: Write the service**

Create `strategy-lab-run-report.service.ts`. Move the following from `engine-lab/run-report/run-report.component.ts` **unchanged**: `hasPendingParity` (delete it — only the dropped fast-path used it), `parseLeanAnalysis`, `UNAVAILABLE_REASON_COPY`, `toParityView`, `toEngineTrade`, `parseLeanStatistics`, `formatMoney`. Then:

```ts
import { computed, inject, Injectable, signal } from "@angular/core";
import { rxResource } from "@angular/core/rxjs-interop";
import { Apollo } from "apollo-angular";
import { filter, map, of } from "rxjs";

import {
  BACKTEST_RUN_DETAIL_QUERY,
  type BacktestRunDetail,
  type BacktestRunDetailQueryResult,
} from "../../graphql/backtest-runs.query";
import type { TradingMarker, TradingPoint } from "../../shared/trading-chart";
import type { EngineResultData } from "../lean-engine/engine-results/engine-results.component";
import { parseRunVerdictEnvelope, type StrategyLabParityView } from "./strategy-lab.models";

/**
 * Every derivation the one-page workbench needs from one persisted run.
 *
 * Component-scoped and provided by `StrategyLabComponent`, alongside
 * `StrategyLabConfigStore` and `StrategyLabRunner`, so the screen cannot
 * inherit run state from a retired product surface.
 */
@Injectable()
export class StrategyLabRunReport {
  private readonly apollo = inject(Apollo);

  readonly activeRunId = signal<number | null>(null);

  private readonly runResource = rxResource<BacktestRunDetail | null, number | null>({
    params: () => this.activeRunId(),
    stream: ({ params }) => {
      if (params === null) return of(null);
      const ref = this.apollo.watchQuery<BacktestRunDetailQueryResult>({
        query: BACKTEST_RUN_DETAIL_QUERY,
        variables: { id: params },
        fetchPolicy: "network-only",
        pollInterval: 5000,
      });
      return ref.valueChanges.pipe(
        filter((result) => !result.loading),
        map((result): BacktestRunDetail | null => {
          // Apollo can surface a GraphQL validation error alongside an empty
          // result. Propagate it so an unavailable report is not incorrectly
          // presented as a missing run.
          if (result.error) {
            ref.stopPolling();
            throw result.error;
          }
          const run = (result.data?.backtestRun as BacktestRunDetail | null | undefined) ?? null;
          if (!run || !run.parityVerdicts.some((verdict) => verdict.status === "pending")) {
            ref.stopPolling();
          }
          return run;
        }),
      );
    },
  });

  readonly run = computed(() => {
    const value = this.runResource.hasValue() ? this.runResource.value() : null;
    return value?.id === this.activeRunId() ? value : null;
  });
  readonly loading = computed(() => this.runResource.isLoading() && !this.run());
  readonly loadError = computed(() => this.runResource.error());

  private readonly verdictEnvelope = computed(() =>
    parseRunVerdictEnvelope(this.run()?.verdictJson ?? null),
  );
  readonly verdict = computed(() => this.verdictEnvelope().verdict);

  // engineResult, markers, equityPoints, reportNotices, parity:
  // copy the five `computed(...)` blocks from run-report.component.ts verbatim,
  // replacing `this.run()` reads unchanged — the signal has the same name here.
}
```

Copy `engineResult`, `markers`, `equityPoints`, `reportNotices`, and `parity` from `run-report.component.ts` verbatim (they already read `this.run()` and `this.verdictEnvelope()`, both of which exist here with the same names).

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-run-report.service.spec.ts'
```

Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.ts Frontend/src/app/components/strategy-lab/strategy-lab-run-report.service.spec.ts
git commit -m "$(cat <<'EOF'
Extract persisted-run derivations into StrategyLabRunReport

Lifts every computed from RunReportComponent into a component-scoped
service keyed by a settable activeRunId, so the workbench and not a
route container owns run state. The runDetail fast-path is dropped:
the workbench always has an id, never a pre-fetched detail.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `StrategyLabRunStatsComponent`

The left column's results block: everything that is a number or a receipt, stacked.

**Files:**
- Create: `Frontend/src/app/components/strategy-lab/run-stats/strategy-lab-run-stats.component.{ts,html,scss}`
- Test: `Frontend/src/app/components/strategy-lab/run-stats/strategy-lab-run-stats.component.spec.ts`

**Interfaces:**
- Consumes: `StrategyLabRunReport`'s output shapes (Task 1) — `BacktestRunDetail`, `EngineResultData`, `RunVerdict | null`, `StrategyLabParityView | null`.
- Produces:
  ```ts
  class StrategyLabRunStatsComponent {
    readonly run = input.required<BacktestRunDetail>();
    readonly result = input.required<EngineResultData>();
    readonly verdict = input<RunVerdict | null>(null);
    readonly parity = input<StrategyLabParityView | null>(null);
    readonly tradesTruncated = input(false);
  }
  ```
  Selector: `app-strategy-lab-run-stats`.

- [ ] **Step 1: Write the failing test**

```ts
import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import { StrategyLabRunStatsComponent } from "./strategy-lab-run-stats.component";

// Reuse the makeRun builder shape from strategy-lab-run-report.service.spec.ts.
// Keep it local to this file — test builders are not shared infrastructure here.
function makeRun() { /* copy from strategy-lab-run-report.service.spec.ts */ }

function makeResult() {
  return {
    success: true, strategy_name: "spy_ema_crossover", fill_mode: "signal_bar_close",
    initial_cash: 100_000, final_equity: 100_048, net_profit: 48, total_fees: 2,
    total_trades: 1, winning_trades: 1, losing_trades: 0, win_rate: 1,
    statistics: { max_drawdown_pct: 0.01, sharpe_ratio: 1.2, sortino_ratio: 1.4, profit_factor: 2.1, expectancy_pct: null },
    lean_statistics: null, lean_analysis: [], trades: [], log_lines: [], validation_analytics: null,
  };
}

describe("StrategyLabRunStatsComponent", () => {
  it("stacks the grade, headline metrics, supplementary statistics and evidence menu", async () => {
    await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun(), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText("Backtest Evidence Grade")).toBeTruthy();
    expect(screen.getByText("Returns")).toBeTruthy();
    expect(screen.getByText("More statistics")).toBeTruthy();
    expect(screen.getByText("Validation atlas")).toBeTruthy();
  });

  it("never renders the retired results-page framing", async () => {
    const { container } = await render(StrategyLabRunStatsComponent, {
      inputs: { run: makeRun(), result: makeResult(), verdict: null, parity: null, tradesTruncated: false },
      providers: [provideZonelessChangeDetection()],
    });

    expect(container.textContent).not.toContain("Back to workbench");
    expect(container.querySelector("app-strategy-lab-chart")).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/run-stats/strategy-lab-run-stats.component.spec.ts'
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the component**

`strategy-lab-run-stats.component.ts`:

```ts
import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { RunVerdict } from "../../../api/run-verdict.types";
import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import type { EngineResultData } from "../../lean-engine/engine-results/engine-results.component";
import { ResultsSidebarComponent } from "../results-sidebar/results-sidebar.component";
import { ResultsSummaryComponent } from "../results-summary/results-summary.component";
import { StrategyLabDeepDivesComponent } from "../strategy-lab-deep-dives/strategy-lab-deep-dives.component";
import type { StrategyLabParityView } from "../strategy-lab.models";

/** The workbench left column's results block, beneath the configuration. */
@Component({
  selector: "app-strategy-lab-run-stats",
  imports: [ResultsSummaryComponent, ResultsSidebarComponent, StrategyLabDeepDivesComponent],
  templateUrl: "./strategy-lab-run-stats.component.html",
  styleUrl: "./strategy-lab-run-stats.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabRunStatsComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly result = input.required<EngineResultData>();
  readonly verdict = input<RunVerdict | null>(null);
  readonly parity = input<StrategyLabParityView | null>(null);
  readonly tradesTruncated = input(false);
}
```

`strategy-lab-run-stats.component.html`:

```html
<section class="run-stats" aria-label="Run statistics">
  <app-strategy-lab-results-summary
    [result]="result()"
    [verdict]="verdict()"
    [metricDocumentation]="run().metricDocumentation ?? []"
    [runId]="run().id"
  />
  <app-strategy-lab-results-sidebar [run]="run()" />
  <app-strategy-lab-deep-dives
    [result]="result()"
    [tradesTruncated]="tradesTruncated()"
    [parity]="parity()"
  />
</section>
```

`strategy-lab-run-stats.component.scss`:

```scss
:host { display: block; min-width: 0; }

.run-stats {
  display: grid;
  gap: var(--space-3);
  min-width: 0;
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/run-stats/strategy-lab-run-stats.component.spec.ts'
```

Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-lab/run-stats/
git commit -m "$(cat <<'EOF'
Add StrategyLabRunStatsComponent for the workbench left column

Composes the existing results-summary, results-sidebar and deep-dives
blocks with unchanged inputs, stacked for a narrow column.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `StrategyLabStageComponent`

The right column: placeholder before any run, chart after one, and a dimmed chart under a progress overlay during a re-run. The previous chart is never unmounted.

**Files:**
- Create: `Frontend/src/app/components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component.{ts,html,scss}`
- Test: `Frontend/src/app/components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component.spec.ts`

**Interfaces:**
- Consumes: `BacktestRunDetail`, `TradingMarker`, `TradingPoint`.
- Produces:
  ```ts
  class StrategyLabStageComponent {
    readonly run = input<BacktestRunDetail | null>(null);
    readonly markers = input<readonly TradingMarker[]>([]);
    readonly equityPoints = input<readonly TradingPoint[]>([]);
    readonly notices = input<readonly string[]>([]);
    readonly running = input(false);
    readonly runStatus = input("");
    readonly runPhaseDetail = input("");
    readonly symbol = input.required<string>();
    readonly resolution = input.required<string>();
    readonly fillMode = input.required<string>();
    readonly engine = input.required<string>();
    readonly dataPolicyNote = input("");
  }
  ```
  Selector: `app-strategy-lab-stage`.

- [ ] **Step 1: Write the failing test**

```ts
import { Component, input, provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";
import { StrategyLabStageComponent } from "./strategy-lab-stage.component";

@Component({ selector: "app-strategy-lab-chart", template: `<div data-testid="chart">chart</div>` })
class ChartStubComponent {
  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<unknown[]>([]);
  readonly equityPoints = input<unknown[]>([]);
}

function makeRun() { /* copy from strategy-lab-run-report.service.spec.ts */ }

async function renderStage(inputs: Record<string, unknown>) {
  await TestBed.configureTestingModule({
    imports: [StrategyLabStageComponent],
    providers: [provideZonelessChangeDetection()],
  }).overrideComponent(StrategyLabStageComponent, {
    remove: { imports: [StrategyLabChartComponent] },
    add: { imports: [ChartStubComponent] },
  }).compileComponents();

  const fixture = TestBed.createComponent(StrategyLabStageComponent);
  const base = { symbol: "SPY", resolution: "minute", fillMode: "signal_bar_close", engine: "python" };
  for (const [key, value] of Object.entries({ ...base, ...inputs })) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
  return fixture;
}

describe("StrategyLabStageComponent", () => {
  it("shows the honest placeholder before any run exists", async () => {
    const fixture = await renderStage({ run: null });
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain("Run a validation to populate the equity curve");
    expect(root.querySelector("[data-testid='chart']")).toBeNull();
  });

  it("replaces the placeholder with the chart once a run is loaded", async () => {
    const fixture = await renderStage({ run: makeRun() });
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector("[data-testid='chart']")).not.toBeNull();
    expect(root.textContent).not.toContain("Run a validation to populate the equity curve");
  });

  it("keeps the previous chart mounted and dimmed during a re-run", async () => {
    const fixture = await renderStage({
      run: makeRun(),
      running: true,
      runStatus: "Running indicators and strategy logic…",
    });
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector("[data-testid='chart']")).not.toBeNull();
    expect(root.querySelector(".stage__evidence--stale")).not.toBeNull();
    expect(root.querySelector("[role='status']")?.textContent)
      .toContain("Running indicators and strategy logic…");
  });

  it("renders every report notice", async () => {
    const fixture = await renderStage({ run: makeRun(), notices: ["No strict dual-curve report."] });

    expect((fixture.nativeElement as HTMLElement).textContent).toContain("No strict dual-curve report.");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component.spec.ts'
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the component**

`strategy-lab-stage.component.ts`:

```ts
import { ChangeDetectionStrategy, Component, input } from "@angular/core";

import type { BacktestRunDetail } from "../../../graphql/backtest-runs.query";
import type { TradingMarker, TradingPoint } from "../../../shared/trading-chart";
import { ValidationStagePlaceholderComponent } from "../../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabChartComponent } from "../strategy-lab-chart/strategy-lab-chart.component";

/**
 * The workbench's evidence column. A run in flight never destroys the chart it
 * is about to replace: the previous run stays mounted and is dimmed under a
 * progress overlay, so nothing is removed before its replacement exists and a
 * stale chart cannot read as live.
 */
@Component({
  selector: "app-strategy-lab-stage",
  imports: [ValidationStagePlaceholderComponent, StrategyLabChartComponent],
  templateUrl: "./strategy-lab-stage.component.html",
  styleUrl: "./strategy-lab-stage.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabStageComponent {
  readonly run = input<BacktestRunDetail | null>(null);
  readonly markers = input<readonly TradingMarker[]>([]);
  readonly equityPoints = input<readonly TradingPoint[]>([]);
  readonly notices = input<readonly string[]>([]);
  readonly running = input(false);
  readonly runStatus = input("");
  readonly runPhaseDetail = input("");
  readonly symbol = input.required<string>();
  readonly resolution = input.required<string>();
  readonly fillMode = input.required<string>();
  readonly engine = input.required<string>();
  readonly dataPolicyNote = input("");
}
```

`strategy-lab-stage.component.html`:

```html
<section class="stage" aria-label="Strategy evidence">
  @for (notice of notices(); track notice) {
    <p class="stage__notice" role="note">{{ notice }}</p>
  }

  <div class="stage__evidence" [class.stage__evidence--stale]="running() && run() !== null">
    @if (run(); as currentRun) {
      <app-strategy-lab-chart
        [run]="currentRun"
        [markers]="markers()"
        [equityPoints]="equityPoints()"
      />
    } @else {
      <app-validation-stage-placeholder
        [symbol]="symbol()"
        [resolution]="resolution()"
        [fillMode]="fillMode()"
        [engine]="engine()"
        [dataPolicyNote]="dataPolicyNote()"
      />
    }

    @if (running()) {
      <div class="stage__progress" role="status" aria-live="polite">
        <span class="stage__pulse" aria-hidden="true"></span>
        <strong>{{ runStatus() || "Running validation…" }}</strong>
        @if (runPhaseDetail()) { <span>{{ runPhaseDetail() }}</span> }
      </div>
    }
  </div>
</section>
```

`strategy-lab-stage.component.scss`:

```scss
:host { display: block; min-width: 0; min-height: 0; height: 100%; }

.stage {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: var(--space-2);
  min-width: 0;
  min-height: 0;
  height: 100%;
}

.stage__notice {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  background: var(--bg-surface);
  color: var(--text-subtle);
  font-size: var(--fs-xs);
}

.stage__evidence {
  position: relative;
  min-width: 0;
  min-height: 0;
  height: 100%;
}

// A run in flight must not let the previous run read as live evidence.
.stage__evidence--stale > app-strategy-lab-chart {
  opacity: 0.42;
  filter: saturate(0.6);
}

.stage__progress {
  position: absolute;
  z-index: 2;
  inset-inline: 50%;
  top: var(--space-4);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: max-content;
  max-width: min(90%, 640px);
  translate: -50% 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
}

.stage__progress span:last-child {
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stage__pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-soft);
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component.spec.ts'
```

Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-lab/strategy-lab-stage/
git commit -m "$(cat <<'EOF'
Add StrategyLabStageComponent for the workbench evidence column

Placeholder before a run, chart after one, and during a re-run the
previous chart stays mounted but dimmed under a progress overlay —
nothing is destroyed before its replacement exists.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `LeanSourceService` tells the truth about an unregistered twin

4 of 7 catalog strategies have `lean_twin = None`, so `GET /api/engine/strategies/{name}/lean-source` returns **404 with a semantic detail** — an honest fact about the strategy, not a system failure. Today the service rejects identically for that and for a transport error, and the editor collapses both into "Registered QCAlgorithm source is unavailable", which misreports an unregistered twin as a broken system.

**Files:**
- Modify: `Frontend/src/app/services/lean-source.service.ts`
- Modify: `Frontend/src/app/components/strategy-lab/lean-source-editor/lean-source-editor.component.ts:47-52`, `...component.html:44-48`
- Test: `Frontend/src/app/services/lean-source.service.spec.ts` (create)
- Test: `Frontend/src/app/components/strategy-lab/lean-source-editor/lean-source-editor.component.spec.ts` (amend)

**Interfaces:**
- Produces:
  ```ts
  export type LeanSourceResult =
    | { readonly kind: "available"; readonly source: LeanStrategySource }
    | { readonly kind: "unregistered"; readonly detail: string }
    | { readonly kind: "unavailable"; readonly detail: string };

  class LeanSourceService {
    getStrategySource(strategyName: string): Promise<LeanSourceResult>;
  }
  ```
  `unregistered` means the strategy has no LEAN twin (HTTP 404). `unavailable` means the lookup itself failed (any other error).

- [ ] **Step 1: Write the failing test**

Create `Frontend/src/app/services/lean-source.service.spec.ts`:

```ts
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { afterEach, describe, expect, it } from "vitest";

import { LeanSourceService } from "./lean-source.service";

function setup() {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting(), LeanSourceService],
  });
  return { service: TestBed.inject(LeanSourceService), http: TestBed.inject(HttpTestingController) };
}

afterEach(() => TestBed.resetTestingModule());

describe("LeanSourceService", () => {
  it("returns the registered twin when one exists", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("rsi_mean_reversion");
    http.expectOne((request) => request.url.endsWith("/rsi_mean_reversion/lean-source")).flush({
      strategy_name: "rsi_mean_reversion", template: "rsi_mean_reversion", language: "python",
      source: "class A(QCAlgorithm): pass", source_sha256: "a".repeat(64),
    });

    expect(await pending).toEqual(expect.objectContaining({ kind: "available" }));
  });

  it("reports an unregistered twin as a fact about the strategy, not a failure", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("sma_crossover");
    http.expectOne((request) => request.url.endsWith("/sma_crossover/lean-source")).flush(
      { detail: "Strategy 'sma_crossover' has no registered LEAN validation source" },
      { status: 404, statusText: "Not Found" },
    );

    expect(await pending).toEqual({
      kind: "unregistered",
      detail: "Strategy 'sma_crossover' has no registered LEAN validation source",
    });
  });

  it("keeps a transport failure distinguishable from an unregistered twin", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("rsi_mean_reversion");
    http.expectOne((request) => request.url.endsWith("/rsi_mean_reversion/lean-source"))
      .flush("boom", { status: 500, statusText: "Server Error" });

    expect((await pending).kind).toBe("unavailable");
  });
});
```

Then amend `lean-source-editor.component.spec.ts`: every existing `getStrategySource` stub must now resolve `{ kind: "available", source: REGISTERED_SOURCE }` instead of `REGISTERED_SOURCE`. Add one case:

```ts
it("says a strategy has no registered twin instead of blaming the system", async () => {
  await renderEditor({
    getStrategySource: vi.fn(async () => ({
      kind: "unregistered" as const,
      detail: "Strategy 'sma_crossover' has no registered LEAN validation source",
    })),
  });

  expect(screen.getByText(/has no registered LEAN validation source/)).toBeTruthy();
  expect(screen.queryByText("Registered QCAlgorithm source is unavailable.")).toBeNull();
});
```

(Match `renderEditor`'s existing shape in that file — it already provides a `LeanSourceService` stub.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd Frontend && npx ng test --include='src/app/services/lean-source.service.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-lab/lean-source-editor/lean-source-editor.component.spec.ts'
```

Expected: FAIL — the service still returns `LeanStrategySource`, and the editor has no unregistered branch.

- [ ] **Step 3: Implement**

`lean-source.service.ts`:

```ts
import { HttpClient, HttpErrorResponse } from "@angular/common/http";
import { inject, Injectable } from "@angular/core";
import { catchError, firstValueFrom, map, of } from "rxjs";

import { environment } from "../../environments/environment";
import type { components } from "../api/broker.types";

export type LeanStrategySource = components["schemas"]["StrategyLeanSourceResponse"];

/**
 * A strategy with no `lean_twin` is a fact about the strategy — 4 of 7
 * registered strategies are in that state — not a system failure. Keeping the
 * two apart stops the UI reporting "unavailable" for something that was never
 * registered in the first place.
 */
export type LeanSourceResult =
  | { readonly kind: "available"; readonly source: LeanStrategySource }
  | { readonly kind: "unregistered"; readonly detail: string }
  | { readonly kind: "unavailable"; readonly detail: string };

@Injectable({ providedIn: "root" })
export class LeanSourceService {
  private readonly http = inject(HttpClient);

  getStrategySource(strategyName: string): Promise<LeanSourceResult> {
    return firstValueFrom(
      this.http
        .get<LeanStrategySource>(
          `${environment.pythonServiceUrl}/api/engine/strategies/${encodeURIComponent(strategyName)}/lean-source`,
        )
        .pipe(
          map((source): LeanSourceResult => ({ kind: "available", source })),
          catchError((error: unknown) => of(toFailure(error))),
        ),
    );
  }
}

function toFailure(error: unknown): LeanSourceResult {
  if (error instanceof HttpErrorResponse && error.status === 404) {
    return { kind: "unregistered", detail: detailOf(error) ?? "This strategy has no registered LEAN validation source." };
  }
  return {
    kind: "unavailable",
    detail: "The registered QCAlgorithm source could not be loaded.",
  };
}

function detailOf(error: HttpErrorResponse): string | null {
  const body: unknown = error.error;
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as Record<string, unknown>)["detail"];
  return typeof detail === "string" && detail.trim() ? detail : null;
}
```

In `lean-source-editor.component.ts`, replace the two computeds at lines 47–52:

```ts
  protected readonly registeredSource = computed(() => {
    const result = this.sourceResource.hasValue() ? this.sourceResource.value() : null;
    return result?.kind === "available" ? result.source : null;
  });
  protected readonly sourceFailure = computed(() => {
    const result = this.sourceResource.hasValue() ? this.sourceResource.value() : null;
    return result && result.kind !== "available" ? result : null;
  });
```

Guard `toggleCustomSource` and `resetSource` are already `registeredSource !== null` checks and need no change.

In `lean-source-editor.component.html`, replace the load-state block:

```html
  @if (sourceResource.isLoading()) {
    <p class="lean-source__load-state" role="status">Loading registered QCAlgorithm…</p>
  } @else if (sourceFailure(); as failure) {
    <p class="lean-source__load-state" role="status">{{ failure.detail }}</p>
  }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd Frontend && npx ng test --include='src/app/services/lean-source.service.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-lab/lean-source-editor/lean-source-editor.component.spec.ts'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/services/lean-source.service.ts Frontend/src/app/services/lean-source.service.spec.ts Frontend/src/app/components/strategy-lab/lean-source-editor/
git commit -m "$(cat <<'EOF'
Distinguish an unregistered LEAN twin from a failed source lookup

Four of seven registered strategies have no lean_twin, so the endpoint's
404 is a fact about the strategy. The service now returns a discriminated
result and the editor reports the registry's own reason instead of
blaming the system for an absence that was never a failure.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Move the LEAN source editor into a drawer off the config rail

The editor is a run *input*, so it belongs with the inputs — but code needs width a 360px column does not have. It moves out of the evidence column into a right-side `p-drawer`, reusing the pattern `strategy-lab-deep-dives` already uses. This must land **before** Task 7 evicts it from the stage, so there is never a commit where custom LEAN source is unreachable.

**Files:**
- Modify: `.../strategy-lab-config-rail/strategy-lab-config-rail.component.ts` (add one output), `...html` (add the trigger), `...scss` (style it)
- Modify: `.../strategy-lab/strategy-lab.component.ts` (import `Drawer`, hold drawer state), `...html` (remove the stage editor, add the drawer)
- Test: `.../strategy-lab-config-rail/strategy-lab-config-rail.component.spec.ts` (amend), `.../strategy-lab/strategy-lab.component.spec.ts` (amend)

**Interfaces:**
- Consumes: `LeanSourceResult` (Task 4).
- Produces: on `StrategyLabConfigRailComponent`, `readonly leanSourceRequested = output();` — emitted by a button rendered only when `engine() !== "python"`. On `StrategyLabComponent`, `protected readonly leanSourceOpen = signal(false);`.

- [ ] **Step 1: Write the failing test**

In `strategy-lab-config-rail.component.spec.ts` add:

```ts
it("offers the QCAlgorithm editor only when an engine that runs LEAN is selected", async () => {
  const requested = vi.fn();
  const { fixture } = await renderRail({ engine: "lean" }, { leanSourceRequested: requested });
  const root = fixture.nativeElement as HTMLElement;

  const button = root.querySelector<HTMLButtonElement>("button[aria-label='Edit QCAlgorithm source']");
  if (!button) throw new Error("QCAlgorithm editor trigger is missing");
  button.click();

  expect(requested).toHaveBeenCalled();
});

it("hides the QCAlgorithm editor for the Python engine", async () => {
  const { fixture } = await renderRail({ engine: "python" });

  expect((fixture.nativeElement as HTMLElement)
    .querySelector("button[aria-label='Edit QCAlgorithm source']")).toBeNull();
});
```

(Match the existing `renderRail` helper in that file; if it does not accept outputs, extend it.)

In `strategy-lab.component.spec.ts`, replace the existing test `"loads the registered QCAlgorithm in LEAN mode without probing the launcher"` with a drawer-based version:

```ts
it("opens the registered QCAlgorithm in a drawer without probing the launcher", async () => {
  const { fixture, http, diagnose } = await createLab();
  http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
  await fixture.whenStable();

  fixture.componentInstance.config.changeEngine("lean");
  fixture.detectChanges();

  const root = fixture.nativeElement as HTMLElement;
  expect(root.querySelector("app-lean-source-editor")).toBeNull();

  root.querySelector<HTMLButtonElement>("button[aria-label='Edit QCAlgorithm source']")?.click();
  fixture.detectChanges();
  http.expectOne((request) => request.url.endsWith(
    "/api/engine/strategies/ema_crossover_signal/lean-source",
  )).flush({
    strategy_name: "ema_crossover_signal",
    template: "ema_crossover_signal",
    language: "python",
    source: "from AlgorithmImports import *\nclass MyAlgorithm(QCAlgorithm):\n    pass\n",
    source_sha256: "a".repeat(64),
  });
  await fixture.whenStable();
  fixture.detectChanges();

  const view = within(fixture.nativeElement);
  expect(view.getByText("QCAlgorithm source")).toBeDefined();
  expect(view.getByLabelText("QCAlgorithm source editor").textContent).toContain("class MyAlgorithm");
  expect(diagnose).not.toHaveBeenCalled();
  http.verify();
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-config-rail/strategy-lab-config-rail.component.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab.component.spec.ts'
```

Expected: FAIL — no trigger button exists; the editor still renders inline in the stage.

- [ ] **Step 3: Implement**

In `strategy-lab-config-rail.component.ts`, add beside the other outputs:

```ts
  readonly leanSourceRequested = output();
```

In `strategy-lab-config-rail.component.html`, inside the `@if (engine() !== "python")` block in `<details class="advanced">`, above the existing `<section class="lean-launcher">`:

```html
        <button
          type="button"
          class="lean-source-trigger"
          aria-label="Edit QCAlgorithm source"
          aria-haspopup="dialog"
          (click)="leanSourceRequested.emit()"
        >
          <span>Edit QCAlgorithm source</span>
          <i class="pi pi-angle-right" aria-hidden="true"></i>
        </button>
```

In `strategy-lab-config-rail.component.scss`, append:

```scss
.lean-source-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--border-light);
  border-radius: var(--radius);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.lean-source-trigger:hover,
.lean-source-trigger:focus-visible {
  border-color: var(--accent);
  color: var(--text-primary);
}
```

In `strategy-lab.component.ts`, add `Drawer` to `imports` (from `primeng/drawer`), keep `LeanSourceEditorComponent` in `imports`, and add:

```ts
  protected readonly leanSourceOpen = signal(false);
```

In `strategy-lab.component.html`: delete the `@if (config.engine() === "lean" && config.selectedStrategyName(); as strategyName) { <app-lean-source-editor .../> }` block from the stage, add `(leanSourceRequested)="leanSourceOpen.set(true)"` to `<app-strategy-lab-config-rail>`, and add before `<app-run-dock />`:

```html
  @if (leanSourceOpen() && config.selectedStrategyName(); as strategyName) {
    <p-drawer
      [visible]="true"
      (visibleChange)="leanSourceOpen.set($event)"
      position="right"
      [modal]="true"
      [dismissible]="true"
      [style]="{ width: 'min(960px, 92vw)' }"
      header="QCAlgorithm source"
    >
      <app-lean-source-editor
        [strategyName]="strategyName"
        [launcherStatus]="runs.leanLauncherStatus()"
        [customSource]="config.customLeanSource()"
        (customSourceChange)="config.customLeanSource.set($event)"
      />
    </p-drawer>
  }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-config-rail/strategy-lab-config-rail.component.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab.component.spec.ts'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-lab/strategy-lab-config-rail/ Frontend/src/app/components/strategy-lab/strategy-lab.component.ts Frontend/src/app/components/strategy-lab/strategy-lab.component.html Frontend/src/app/components/strategy-lab/strategy-lab.component.spec.ts
git commit -m "$(cat <<'EOF'
Move the QCAlgorithm editor into a drawer off the config rail

The editor is a run input, so it belongs with the inputs — but code
needs width the rail does not have. It opens in the same right-side
drawer pattern the evidence menu uses, freeing the evidence column for
the chart without losing the only route to a parameterized LEAN run.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Route the run id through a query param

**Files:**
- Modify: `Frontend/src/app/app.routes.ts:163-169`
- Test: `Frontend/src/app/app.routes.spec.ts:10-11, 28-34`

**Interfaces:**
- Produces: `/strategy-lab?run=<id>` as the canonical URL for a loaded run. `/strategy-lab/runs/:id` and (transitively) `/engine/runs/:id` redirect to it.

**Why not a path param.** `/strategy-lab` and `/strategy-lab/runs/:id` are different route configs, so Angular destroys and recreates the component between them. `StrategyLabConfigStore` and `StrategyLabRunner` are component-scoped `providers`, so a completing run would tear down its own runner mid-flight and reset the configuration. This is the load-bearing constraint of the whole design.

- [ ] **Step 1: Write the failing test**

In `app.routes.spec.ts`, replace the two assertions that expect a `loadComponent` on the run route:

```ts
import { Injector, runInInjectionContext } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideRouter, Router, type UrlTree } from "@angular/router";

// in the first test, replace the runs/:id loadComponent assertion with:
    expect(routes.find((route) => route.path === 'strategy-lab/runs/:id')?.redirectTo).toBeTypeOf('function');

// replace the whole "loads the read-only Results page for persisted run URLs" test with:
  it('redirects a persisted run URL onto the one-page workbench', () => {
    const route = routes.find((candidate) => candidate.path === 'strategy-lab/runs/:id');
    const redirect = route?.redirectTo;
    if (typeof redirect !== 'function') throw new Error('Strategy Lab run route is not a redirect.');

    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const tree = runInInjectionContext(TestBed.inject(Injector), () =>
      redirect({
        routeConfig: route ?? null,
        url: [],
        params: { id: '204' },
        queryParams: {},
        fragment: null,
        data: {},
        outlet: 'primary',
        title: undefined,
      } as never),
    );

    expect(TestBed.inject(Router).serializeUrl(tree as UrlTree)).toBe('/strategy-lab?run=204');
  });
```

The `as never` on the redirect argument is deliberate and the one type assertion this plan
permits: Angular's `RedirectFunction` parameter is a `Pick` of `ActivatedRouteSnapshot`
whose exact member list is not part of the public type surface, so constructing a literal
is the only way to invoke it directly.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/app.routes.spec.ts'
```

Expected: FAIL — `redirectTo` is undefined; the route still has `loadComponent`.

- [ ] **Step 3: Implement**

In `app.routes.ts`, add `import { inject } from "@angular/core";` and `Router` to the `@angular/router` import, then replace the `strategy-lab/runs/:id` route:

```ts
  {
    // The workbench and this URL must be the SAME route config: a different
    // one would destroy and recreate StrategyLabComponent, tearing down its
    // component-scoped config store and runner while a run is in flight.
    path: "strategy-lab/runs/:id",
    redirectTo: ({ params }) =>
      inject(Router).parseUrl(`/strategy-lab?run=${encodeURIComponent(String(params["id"] ?? ""))}`),
  },
```

Delete the `StrategyLabResultsComponent` import from `app.routes.spec.ts`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/app.routes.spec.ts'
```

Expected: PASS.

**Correction, established during implementation (see the design spec's "Correction, found
during implementation").** This step originally expected the legacy
`engine/runs/:id` → `strategy-lab/runs/:id` → `/strategy-lab?run=N` chain to resolve
"through Angular's recursive redirect application". That is **false** for
`@angular/router@22.0.8`: `expandSegmentAgainstRouteUsingRedirect` recurses via
`processSegment(..., false, ...)`, forcing `allowRedirects` to `false` on the re-match, so a
redirect whose target is itself a redirect route throws `NoMatch` on the second hop and
falls through to the `**` wildcard — the legacy deep link would have died silently. The
shipped code therefore redirects `engine/runs/:id` **directly** to the final URL through the
same shared `redirectToStrategyLabRun` function, and the test pins the resolved URL rather
than the route-config shape.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/app.routes.ts Frontend/src/app/app.routes.spec.ts
git commit -m "$(cat <<'EOF'
Redirect persisted run URLs onto the one-page workbench

/strategy-lab/runs/:id becomes a redirect function producing
/strategy-lab?run=N so the run id rides the same route config as the
workbench. A different route config would destroy the component and its
component-scoped stores between a run finishing and its report loading.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The one-page workbench

The tracer bullet. After this task the feature works end to end and `RunReportComponent` and the results page are gone.

**Files:**
- Modify: `.../strategy-lab/strategy-lab.component.{ts,html,scss,spec.ts}`
- Modify: `.../strategy-lab/strategy-lab-config.store.ts` (add the `run` param signal)
- Modify: `.../strategy-lab/strategy-lab-runner.service.ts:345-380` (navigate by query param, expose `justProducedRunId`)
- Delete: `Frontend/src/app/components/engine-lab/` (whole directory)
- Delete: `Frontend/src/app/components/strategy-lab/results-page/` (whole directory)

**Interfaces:**
- Consumes: `StrategyLabRunReport` (Task 1), `StrategyLabRunStatsComponent` (Task 2), `StrategyLabStageComponent` (Task 3), `/strategy-lab?run=N` (Task 6).
- Produces: on `StrategyLabRunner`, `readonly justProducedRunId = signal<number | null>(null);` — set immediately before the completion navigation, consumed once by the workbench's restore guard.

- [ ] **Step 1: Write the failing test**

Replace the two workbench tests in `strategy-lab.component.spec.ts` that assert the old shape (`"starts with Workbench and History tabs…"` keeps its tab assertions but drops `app-engine-run-report`; `"opens a selected history run on its dedicated Results route"` becomes a query-param assertion), and add:

```ts
it("populates statistics under the configuration and the chart on the stage", async () => {
  const saved = run();
  const { fixture, http } = await createLab({ activeRun: saved.id, backtestRun: saved });
  http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
  const root = fixture.nativeElement as HTMLElement;
  await vi.waitFor(() => {
    expect(root.querySelector("app-strategy-lab-run-stats")).not.toBeNull();
  });

  const rail = root.querySelector(".workbench__rail");
  expect(rail?.querySelector("app-strategy-lab-run-stats")).not.toBeNull();
  expect(root.querySelector(".workbench__stage app-strategy-lab-stage")).not.toBeNull();
  expect(root.textContent).not.toContain("Back to workbench");
  http.verify();
});

it("opens a selected history run on the same page", async () => {
  const { fixture, http, navigate } = await createLab();
  http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
  await fixture.whenStable();

  fixture.componentInstance.selectHistoryRun("91");

  expect(navigate).toHaveBeenCalledWith(["/strategy-lab"], expect.objectContaining({
    queryParams: { run: 91 },
    queryParamsHandling: "merge",
  }));
  http.verify();
});

it("collapses the configuration on completion without writing the saved preference", async () => {
  localStorage.removeItem("engineLab.configNavOverride");
  const saved = run();
  const { fixture, http } = await createLab({ activeRun: saved.id, backtestRun: saved });
  http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
  await vi.waitFor(() => {
    expect(fixture.componentInstance.config.configNavCollapsed()).toBe(true);
  });

  // Completion is an event, not a setting: the operator's stored preference
  // must be untouched so a reload does not inherit an automatic collapse.
  expect(localStorage.getItem("engineLab.configNavOverride")).toBeNull();
  http.verify();
});

it("does not discard the custom QCAlgorithm that produced the run just completed", async () => {
  const saved = run();
  const { fixture, http } = await createLab({ backtestRun: saved });
  http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
  await fixture.whenStable();

  const lab = fixture.componentInstance;
  lab.config.changeEngine("lean");
  lab.config.customLeanSource.set("class Edited(QCAlgorithm): pass");
  lab.runs.justProducedRunId.set(saved.id);
  lab.loadRun(saved.id);
  await fixture.whenStable();

  expect(lab.config.customLeanSource()).toBe("class Edited(QCAlgorithm): pass");
});
```

Extend `createLab` to accept `activeRun?: number` and put it in the query param map as `run`, keeping `restoreRun` support:

```ts
  const query: Record<string, string> = {};
  if (options.activeRun) query["run"] = String(options.activeRun);
  if (options.restoreRun) query["restoreRun"] = String(options.restoreRun);
  const params = convertToParamMap(query);
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab.component.spec.ts'
```

Expected: FAIL — `app-strategy-lab-run-stats` is never rendered; `loadRun` and `justProducedRunId` do not exist.

- [ ] **Step 3: Implement**

**3a — `strategy-lab-config.store.ts`**, add near `launchParams` (do *not* fold `run` into `EngineLaunchParams`: `applyLaunchParams` is guarded by `appliedLaunchParamsKey`, so a changing `run` would re-fire the whole launch-param path and reset `activeTab` and the strategy selection):

```ts
  /**
   * The persisted run the page is displaying. Deliberately separate from
   * `launchParams`: run loading and launch-param application are two
   * mechanisms over one query string, and folding them together would make
   * every run load re-apply the launch params.
   */
  readonly activeRunParam = toSignal(
    inject(ActivatedRoute).queryParamMap.pipe(
      map((params) => parseRunId(params.get("run") ?? params.get("restoreRun"))),
    ),
    { initialValue: null },
  );
```

and at file scope:

```ts
function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
```

(The single `inject(ActivatedRoute)` already at the top of the class can be hoisted to a field and reused.)

**3b — `strategy-lab-runner.service.ts`**, add the field:

```ts
  /** The run this runner just persisted, so the workbench can skip a restore
   *  that would clobber the configuration which produced it. */
  readonly justProducedRunId = signal<number | null>(null);
```

and replace both navigations (lines 351 and 378) with:

```ts
        this.justProducedRunId.set(studyId);
        await this.router.navigate(["/strategy-lab"], {
          queryParams: { run: studyId },
          queryParamsHandling: "merge",
        });
```

(where `studyId` is `response.study_id` / `response.strategy_execution_id` respectively).

**3c — `strategy-lab.component.ts`**: add `StrategyLabRunReport` to `providers`; add `StrategyLabRunStatsComponent` and `StrategyLabStageComponent` to `imports`; remove `ValidationStagePlaceholderComponent` from `imports` (the stage owns it now). Replace the `restoreRunId` field and constructor body with:

```ts
  readonly report = inject(StrategyLabRunReport);

  constructor() {
    const strategiesReady = this.config.loadStrategies();
    effect(() => {
      const runId = this.config.activeRunParam();
      if (runId === null || runId === this.loadedRunId) return;
      this.loadedRunId = runId;
      this.runs.clearRunError();
      void this.loadRun(runId, strategiesReady);
    });
    effect(() => {
      const runId = this.report.run()?.id ?? null;
      if (runId === null || runId === this.collapsedForRunId) return;
      this.collapsedForRunId = runId;
      // Transient only: `configNavOverride` is the operator's saved preference
      // and a completed run is an event, not a setting.
      this.config.configNavCollapsed.set(true);
    });
  }

  private loadedRunId: number | null = null;
  private collapsedForRunId: number | null = null;

  async loadRun(runId: number, strategiesReady?: Promise<void>): Promise<void> {
    this.report.activeRunId.set(runId);
    // The run the runner just produced already matches the configuration on
    // screen. Restoring it anyway would call applyStrategy, which nulls
    // customLeanSource — silently discarding the QCAlgorithm that produced it.
    if (this.runs.justProducedRunId() === runId) {
      this.runs.justProducedRunId.set(null);
      return;
    }
    this.config.activeTab.set("configuration");
    await (strategiesReady ?? Promise.resolve());
    try {
      const response = await firstValueFrom(
        this.apollo.query<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id: runId },
          fetchPolicy: "network-only",
        }),
      );
      const run = response.data?.backtestRun;
      if (run === null || run === undefined) {
        this.runs.runError.set(`Saved run #${runId} was not found.`);
        return;
      }
      this.restoreConfiguration(run);
    } catch (error) {
      const message = error instanceof Error ? error.message : "The saved configuration could not be restored.";
      this.config.configurationWarning.set(message);
      this.runs.runError.set(message);
    }
  }
```

Keep `restoreConfiguration` unchanged. Replace `selectHistoryRun`'s navigation:

```ts
    void this.router.navigate(["/strategy-lab"], {
      queryParams: { run: numericId },
      queryParamsHandling: "merge",
    });
```

**3d — `strategy-lab.component.html`**: in the `configuration` tab panel, the rail keeps its existing `@if` chain around `<app-strategy-lab-config-rail>` and gains, after it:

```html
              @if (report.run(); as currentRun) {
                @if (report.engineResult(); as result) {
                  <app-strategy-lab-run-stats
                    [run]="currentRun"
                    [result]="result"
                    [verdict]="report.verdict()"
                    [parity]="report.parity()"
                    [tradesTruncated]="currentRun.tradesTruncated"
                  />
                }
              } @else if (report.loadError()) {
                <div class="lab-notice lab-notice--error" role="alert">
                  Run report could not be loaded. Try again from History.
                </div>
              }
```

and the stage becomes:

```html
            <section class="workbench__stage" aria-label="Strategy evidence">
              @if (runs.runError() ?? config.configurationWarning(); as error) {
                <div class="lab-notice lab-notice--error" role="alert">{{ error }}</div>
              }

              <app-strategy-lab-stage
                [run]="report.run()"
                [markers]="report.markers()"
                [equityPoints]="report.equityPoints()"
                [notices]="report.reportNotices()"
                [running]="runs.running()"
                [runStatus]="runs.runStatusBanner()"
                [runPhaseDetail]="runs.runPhaseDetail()"
                [symbol]="config.effectiveSymbol()"
                [resolution]="config.resolution()"
                [fillMode]="config.fillMode()"
                [engine]="config.engine()"
                [dataPolicyNote]="config.dataPolicyNote()"
              />
            </section>
```

Delete the old `run-status` block (the stage owns progress now).

**3e — `strategy-lab.component.scss`**: replace `.workbench`, `.workbench__rail`, `.workbench__stage`, delete `.run-status*`, and add the chrome variable:

```scss
.strategy-lab {
  // Tab list + the run dock's 36px collapsed strip. The dock is
  // position:fixed; expanded (320px) it deliberately overlays.
  --strategy-lab-chrome: calc(var(--page-pad-y) * 2 + 48px + 36px);
  display: grid;
  gap: var(--space-4);
  min-width: 0;
}

.workbench {
  display: grid;
  grid-template-columns: minmax(320px, 380px) minmax(0, 1fr);
  gap: var(--space-4);
  min-width: 0;
  min-height: 0;
  height: calc(100dvh - var(--strategy-lab-chrome));
  align-items: stretch;
}

.workbench__rail {
  display: grid;
  gap: var(--space-3);
  min-width: 0;
  min-height: 0;
  align-content: start;
  overflow-y: auto;
}

.workbench__stage {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: var(--space-3);
  min-width: 0;
  min-height: 0;
}

@media (max-width: 900px) {
  .workbench {
    grid-template-columns: minmax(0, 1fr);
    height: auto;
  }
  .workbench__rail { overflow-y: visible; }
}
```

**3f — Delete the retired surfaces:**

```bash
git rm -r Frontend/src/app/components/engine-lab Frontend/src/app/components/strategy-lab/results-page
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab.component.spec.ts'
cd Frontend && npx ng test --include='src/app/app.routes.spec.ts'
```

Expected: PASS. Then confirm nothing still imports the deleted surfaces:

```bash
grep -rn "engine-lab/run-report\|results-page\|app-engine-run-report\|StrategyLabResultsComponent" Frontend/src | grep -v node_modules
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add -A Frontend/src/app/components/strategy-lab Frontend/src/app/components/engine-lab
git commit -m "$(cat <<'EOF'
Make Strategy Lab one page

Run statistics render under the configuration; the chart replaces the
placeholder on the stage. The run id rides ?run=N on the workbench's own
route, so a completing run no longer unmounts the config store and runner
that produced it. Completion collapses the configuration transiently,
leaving the operator's saved preference untouched, and a restore is
skipped for the run the runner just produced so an edited QCAlgorithm
survives its own run.

Deletes RunReportComponent and the results page, whose layouts this
replaces.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: `results-summary` stacks in a narrow column

Its current wide layout and both media queries are dead after Task 7: media queries are **viewport**-width, so they cannot fire for a 360px column on a 1600px screen, and the component now has exactly one consumer, which is always narrow.

**Files:**
- Modify: `.../strategy-lab/results-summary/results-summary.component.scss`
- Test: `.../strategy-lab/results-summary/results-summary.component.spec.ts` (amend)

- [ ] **Step 1: Write the failing test**

```ts
it("stacks its metric groups in one column for the workbench rail", async () => {
  const { container } = await render(ResultsSummaryComponent, {
    inputs: { result: makeResult(), verdict: null, metricDocumentation: [], runId: 1 },
    providers: [provideZonelessChangeDetection()],
  });

  const summary = container.querySelector<HTMLElement>(".results-summary");
  if (!summary) throw new Error("results-summary root is missing");
  expect(getComputedStyle(summary).gridTemplateColumns.split(" ").length).toBe(1);
});
```

(Match this file's existing `makeResult`/render helper.)

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/results-summary/results-summary.component.spec.ts'
```

Expected: FAIL — two columns.

- [ ] **Step 3: Implement**

In `results-summary.component.scss`: change `.results-summary`'s `grid-template-columns` to `minmax(0, 1fr)`; change `.results-summary__metrics`'s to `minmax(0, 1fr)` and replace `border-left` with `border-top: 1px solid var(--border)`; change `.results-summary__metrics dl` to `grid-template-columns: 1fr`; **delete both `@media` blocks** at the bottom of the file.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-lab/results-summary/results-summary.component.spec.ts'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-lab/results-summary/
git commit -m "$(cat <<'EOF'
Stack the results summary for the workbench rail

Its viewport-width media queries could never fire for a 360px column on
a wide screen, and after the results page was deleted this component has
one always-narrow consumer. Deletes the dead wide layout rather than
leaving it behind a mode flag.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: The chart fits the viewport

`chartHeight` is today `sum(PANE_HEIGHTS)` — 470 + 205 + 185×n, so 860–1100px by construction, which is why the page had to scroll. It becomes a distribution of *measured* available height, keeping today's constants as weights so pane proportions are unchanged.

**Files:**
- Modify: `Frontend/src/app/shared/trading-chart/trading-chart.component.{ts,scss}`
- Modify: `.../strategy-lab/strategy-lab-chart/strategy-lab-chart.component.scss`
- Test: `Frontend/src/app/shared/trading-chart/trading-chart.component.spec.ts` (amend)

**Critical detail.** `observeResize` currently observes the **canvas** element, whose height is driven by `--chart-height` from `chartHeight()`. Measuring that same element would be a feedback loop. Measure `.trading-chart__canvas-wrap` instead: it takes its height from `.trading-chart__body { height: calc(100% - 80px) }`, which is layout-driven and independent of canvas content.

**Interfaces:**
- Produces: `readonly availableHeight = signal(0);` on `TradingChartComponent`, set by the resize observer; `chartHeight` becomes a `computed` over `panes()` and `availableHeight()`.

- [ ] **Step 1: Write the failing test**

```ts
it("distributes measured height across panes by their weights", () => {
  const fixture = renderChart({ candles: CANDLES, equity: EQUITY });
  fixture.componentInstance.availableHeight.set(675);
  fixture.detectChanges();

  // Weights 470 (price) : 205 (equity) = 675 total, so a 675px viewport is exact.
  expect(fixture.componentInstance.chartHeight()).toBe(675);
});

it("falls back to fixed pane heights when the viewport is below the floor", () => {
  const fixture = renderChart({ candles: CANDLES, equity: EQUITY });
  fixture.componentInstance.availableHeight.set(180);
  fixture.detectChanges();

  // Two panes at a 120px floor cannot fit 180px, so the wrap scrolls instead
  // of squashing the price pane into unreadability.
  expect(fixture.componentInstance.chartHeight()).toBe(675);
});

it("uses the fixed heights until a measurement arrives", () => {
  const fixture = renderChart({ candles: CANDLES, equity: EQUITY });

  expect(fixture.componentInstance.chartHeight()).toBe(675);
});
```

(Match this file's existing `renderChart` helper and fixtures; make `availableHeight` and `chartHeight` public on the component so the spec can drive them.)

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/shared/trading-chart/trading-chart.component.spec.ts'
```

Expected: FAIL — `availableHeight` does not exist.

- [ ] **Step 3: Implement**

In `trading-chart.component.ts`, add `const MIN_PANE_HEIGHT = 120;`, a `viewChild` for the wrap, and:

```ts
  /** Measured height of the scroll wrap. 0 until the observer first fires. */
  readonly availableHeight = signal(0);

  /**
   * Pane heights are proportional to the available height, using the fixed
   * PANE_HEIGHTS as weights so relative proportions are unchanged. Below the
   * floor the fixed heights return and `.trading-chart__canvas-wrap` scrolls —
   * squashing a price pane to nothing is worse than a scrollbar.
   */
  readonly paneHeights = computed<number[]>(() => {
    const weights = this.panes().map((pane) => pane.height);
    const weightTotal = weights.reduce((total, weight) => total + weight, 0);
    const available = this.availableHeight();
    if (available <= 0 || weightTotal === 0) return weights;
    const scaled = weights.map((weight) => Math.round((weight / weightTotal) * available));
    return scaled.some((height) => height < MIN_PANE_HEIGHT) ? weights : scaled;
  });

  readonly chartHeight = computed(() =>
    this.paneHeights().reduce((total, height) => total + height, 0),
  );
```

Replace the pane-sizing line in `rebuildChart` (currently `pane.setHeight(panes[index]?.height ?? PANE_HEIGHTS.indicator)`) with `pane.setHeight(this.paneHeights()[index] ?? PANE_HEIGHTS.indicator)`.

Replace `observeResize` to observe the wrap and report both dimensions:

```ts
  private observeResize(element: HTMLDivElement): void {
    const wrap = this.canvasWrap()?.nativeElement;
    if (typeof ResizeObserver === "undefined" || this.chart === null || !wrap) return;
    this.resizeObserver = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      // The wrap's height is layout-driven; the canvas's is driven by
      // chartHeight, so observing the canvas would be a feedback loop.
      const height = rect?.height ?? wrap.clientHeight;
      if (height > 0) this.availableHeight.set(Math.round(height));
      const width = element.clientWidth;
      if (width > 0) this.chart?.resize(width, this.chartHeight());
    });
    this.resizeObserver.observe(wrap);
  }
```

In `trading-chart.component.scss`, add `height: 100%;` to `.trading-chart` and `min-height: 0;` to `:host`.

In `strategy-lab-chart.component.scss`, make the host fill its grid cell:

```scss
:host {
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  min-width: 0;
  min-height: 0;
  height: 100%;
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd Frontend && npx ng test --include='src/app/shared/trading-chart/trading-chart.component.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-lab/strategy-lab-chart/strategy-lab-chart.component.spec.ts'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/shared/trading-chart/ Frontend/src/app/components/strategy-lab/strategy-lab-chart/
git commit -m "$(cat <<'EOF'
Fit the trading chart to its available height

Pane heights become proportional to measured available height, using the
former fixed pixel heights as weights so proportions are unchanged. Below
a per-pane floor the fixed heights return and the wrap scrolls. The
observer watches the layout-driven scroll wrap, not the canvas whose own
height is derived from chartHeight, which would be a feedback loop.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: The registered LEAN twin on Strategy Validation

Strategy Validation already shows the vendored QuantConnect **audit copy** (what a port was validated *against*, present for 1 of 7 strategies). The registered **validation twin** — what Strategy Lab actually executes, present for 3 of 7 — belongs beside it. `strategy_key` is built directly from `_STRATEGY_REGISTRY.items()`, the same dict the lean-source endpoint looks up, so it is already the right key.

**Files:**
- Create: `.../strategy-validation/lean-twin-source/lean-twin-source.component.{ts,html,scss}`
- Test: `.../strategy-validation/lean-twin-source/lean-twin-source.component.spec.ts`
- Modify: `.../strategy-validation/strategy-validation.component.ts` (import), `...html:231-233` (render beside `app-quantconnect-reference-code`)

**Interfaces:**
- Consumes: `LeanSourceService.getStrategySource` returning `LeanSourceResult` (Task 4).
- Produces: `class LeanTwinSourceComponent { readonly strategyKey = input.required<string>(); }`, selector `app-lean-twin-source`.

- [ ] **Step 1: Write the failing test**

```ts
import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import { LeanSourceService } from "../../../services/lean-source.service";
import { LeanTwinSourceComponent } from "./lean-twin-source.component";

async function renderViewer(getStrategySource: ReturnType<typeof vi.fn>, strategyKey: string) {
  return render(LeanTwinSourceComponent, {
    inputs: { strategyKey },
    providers: [provideZonelessChangeDetection(), { provide: LeanSourceService, useValue: { getStrategySource } }],
  });
}

describe("LeanTwinSourceComponent", () => {
  it("shows the registered twin and its source hash", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "available" as const,
      source: {
        strategy_name: "rsi_mean_reversion", template: "rsi_mean_reversion", language: "python",
        source: "class RsiAlgorithm(QCAlgorithm): pass", source_sha256: "c".repeat(64),
      },
    })), "rsi_mean_reversion");

    expect(await screen.findByText(/class RsiAlgorithm/)).toBeTruthy();
    expect(screen.getByTitle("Registered source SHA-256").textContent).toBe("c".repeat(64));
  });

  it("states that a strategy has no registered twin rather than reporting a failure", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "unregistered" as const,
      detail: "Strategy 'sma_crossover' has no registered LEAN validation source",
    })), "sma_crossover");

    expect(await screen.findByText(/has no registered LEAN validation source/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("raises a real lookup failure as an alert", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "unavailable" as const,
      detail: "The registered QCAlgorithm source could not be loaded.",
    })), "rsi_mean_reversion");

    expect((await screen.findByRole("alert")).textContent)
      .toContain("The registered QCAlgorithm source could not be loaded.");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-validation/lean-twin-source/lean-twin-source.component.spec.ts'
```

Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`lean-twin-source.component.ts`:

```ts
import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from "@angular/core";

import { CopyButtonComponent } from "../../../shared/copy-button/copy-button.component";
import { LeanSourceService } from "../../../services/lean-source.service";

/**
 * The registered LEAN validation twin — what Strategy Lab executes — beside
 * the vendored QuantConnect audit copy, which is what a port was validated
 * against. Most registered strategies have no twin, so "none registered" is a
 * first-class state here, not an error.
 */
@Component({
  selector: "app-lean-twin-source",
  imports: [CopyButtonComponent],
  templateUrl: "./lean-twin-source.component.html",
  styleUrl: "./lean-twin-source.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class LeanTwinSourceComponent {
  private readonly leanSource = inject(LeanSourceService);

  readonly strategyKey = input.required<string>();

  protected readonly sourceResource = resource({
    params: () => this.strategyKey(),
    loader: ({ params }) => this.leanSource.getStrategySource(params),
  });
  protected readonly result = computed(() =>
    this.sourceResource.hasValue() ? this.sourceResource.value() : null,
  );
}
```

`lean-twin-source.component.html`:

```html
<section class="lean-twin" aria-labelledby="lean-twin-title">
  <header>
    <div>
      <p class="lean-twin__eyebrow">Registered validation twin</p>
      <h3 id="lean-twin-title">QCAlgorithm source</h3>
    </div>
    @if (result(); as state) {
      @if (state.kind === "available") {
        <app-copy-button
          variant="button"
          [text]="state.source.source"
          label="Copy source"
          ariaLabel="Copy QCAlgorithm source"
        />
      }
    }
  </header>

  @if (sourceResource.isLoading()) {
    <p class="lean-twin__state" role="status">Loading registered QCAlgorithm…</p>
  } @else if (result(); as state) {
    @switch (state.kind) {
      @case ("available") {
        <pre class="lean-twin__source"><code>{{ state.source.source }}</code></pre>
        <footer>
          <span>{{ state.source.template }}</span>
          <code title="Registered source SHA-256">{{ state.source.source_sha256 }}</code>
        </footer>
      }
      @case ("unregistered") {
        <p class="lean-twin__state" role="status">{{ state.detail }}</p>
      }
      @case ("unavailable") {
        <p class="lean-twin__state lean-twin__state--error" role="alert">{{ state.detail }}</p>
      }
    }
  }
</section>
```

`lean-twin-source.component.scss`:

```scss
:host { display: block; min-width: 0; }

.lean-twin {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
  padding: var(--space-3);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.lean-twin > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.lean-twin h3 { margin: 0; color: var(--text-primary); font-size: var(--fs-sm); }

.lean-twin__eyebrow {
  margin: 0 0 2px;
  color: var(--accent);
  font: 500 var(--fs-xxs)/1 var(--font-mono);
  letter-spacing: var(--ls-caps);
  text-transform: uppercase;
}

.lean-twin__source {
  max-height: 420px;
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  border-radius: var(--radius);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font: 500 var(--fs-xs)/1.5 var(--font-mono);
}

.lean-twin__state { margin: 0; color: var(--text-subtle); font-size: var(--fs-xs); }
.lean-twin__state--error { color: var(--danger); }

.lean-twin > footer {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-3);
  color: var(--text-muted);
  font-size: var(--fs-xxs);
}

.lean-twin > footer code { overflow-wrap: anywhere; }
```

In `strategy-validation.component.ts`, add `LeanTwinSourceComponent` to `imports`. In `strategy-validation.component.html`, render it beside the existing reference-code block (around line 231), outside that block's `@if` so it shows for every selected strategy:

```html
            <app-lean-twin-source [strategyKey]="selected.strategy_key" />
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd Frontend && npx ng test --include='src/app/components/strategy-validation/lean-twin-source/lean-twin-source.component.spec.ts'
cd Frontend && npx ng test --include='src/app/components/strategy-validation/strategy-validation.component.spec.ts'
```

Expected: PASS. If the 508-line `strategy-validation.component.spec.ts` fails because its `LeanSourceService` is now called, add a stub provider returning `{ kind: "unregistered", detail: "…" }` to its TestBed.

- [ ] **Step 5: Commit**

```bash
git add Frontend/src/app/components/strategy-validation/
git commit -m "$(cat <<'EOF'
Show the registered LEAN twin on Strategy Validation

Beside the vendored QuantConnect audit copy: one is what a port was
validated against, the other is what Strategy Lab executes. Only three of
seven registered strategies have a twin, so "none registered" is a
first-class state rather than an error.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Verification sweep

No new behaviour. This task proves the branch is shippable and is where the repo's pre-push gates run.

**Files:** none created or modified unless a gate fails.

- [ ] **Step 1: Project-scope lint**

```bash
npx eslint Frontend/src/ --max-warnings 0
```

Expected: clean. Cross-file drift (imports orphaned by the Task 7 deletions) surfaces here and nowhere else — the pre-commit hook only lints staged paths.

- [ ] **Step 2: Full frontend suite**

```bash
cd Frontend && npx ng test
```

Expected: green. A scoped run cannot substitute: parent specs in this repo pin child component copy, and exit code 137 means the container OOMed rather than that a test failed — rerun rather than treating it as a result.

- [ ] **Step 3: Baseline any failure before claiming it is pre-existing**

If a suite fails, confirm whether it also fails on the base commit before treating it as inherited:

```bash
git stash && git checkout origin/master -- Frontend/src && cd Frontend && npx ng test --include='<the failing spec>'
git checkout HEAD -- Frontend/src && git stash pop
```

Anything not on that pre-existing list is this branch's to fix. Genuinely pre-existing failures go in the PR description, never silently.

- [ ] **Step 4: Verify the page in the running app**

Start the preview, then check each acceptance behaviour and capture a screenshot:

1. `/strategy-lab` — configuration left, placeholder right, no page scrollbar.
2. Run a validation — statistics appear under the configuration, chart replaces the placeholder, configuration collapses to its summary strip, URL becomes `/strategy-lab?run=<id>`.
3. Reload — configuration stays collapsed only if it was collapsed *before* the run (`localStorage['engineLab.configNavOverride']` must be unchanged by the automatic collapse).
4. Open `/strategy-lab/runs/<id>` directly — redirects to `/strategy-lab?run=<id>` with the configuration restored.
5. Re-run — previous chart stays visible but dimmed under the progress overlay.
6. Engine → `lean` → "Edit QCAlgorithm source" — drawer opens with the source.
7. Strategy Validation → `rsi_mean_reversion` shows a twin; `sma_crossover` says none is registered.

- [ ] **Step 5: Thermonuclear review, then push**

`CLAUDE.md` requires this before the first push that opens the PR. Address every **major** finding in-branch; minor findings are optional.

```bash
# Invoke the thermo-nuclear-code-quality-review skill over the branch diff,
# fix majors, then:
git push -u origin feat/strategy-lab-one-page
```

---

## Self-Review

**Spec coverage** — every section maps to a task:

| Spec section | Task |
|---|---|
| §3.1 / §4.4 routing, `?run=N`, redirect function, `?restoreRun=` alias | 6, 7 |
| §4.1 service + two components, deletions | 1, 2, 3, 7 |
| §4.2 layout, widened rail, `--strategy-lab-chrome`, `results-summary` restyle | 7, 8 |
| §4.3 transient auto-collapse, preference untouched | 7 |
| §4.4 restore guard for `customLeanSource` | 7 |
| §4.5 viewport-fitting chart, weights, floor, wrap-not-canvas observer | 9 |
| §4.6 drawer in the Lab, viewer on Strategy Validation, semantic-vs-transport 404 | 4, 5, 10 |
| §5 non-scope (persisted twins, Data Lab, run comparison, backend) | none — correctly absent |
| §6 testing: rewritten, new, amended, deleted specs | 1, 3, 5, 6, 7, 8, 9, 10, 11 |

**Ordering check.** Task 5 moves the LEAN editor into the drawer *and* removes it from the stage in one commit, so there is never a commit where custom LEAN source is unreachable — and Task 7 therefore does not touch the editor. Task 4 precedes both consumers of `LeanSourceResult` (Tasks 5 and 10). Task 6 precedes Task 7, which relies on `?run=N` resolving.

**Type consistency.** `activeRunId` (Task 1) is the same name used in Task 7's `loadRun`. `justProducedRunId` is spelled identically in the runner (Task 7, step 3b), the workbench guard (step 3c), and its test. `LeanSourceResult`'s three `kind` values — `available` / `unregistered` / `unavailable` — are used identically in Tasks 4, 5, and 10. `availableHeight` / `paneHeights` / `chartHeight` (Task 9) are consistent between implementation and spec. Component selectors `app-strategy-lab-run-stats`, `app-strategy-lab-stage`, `app-lean-twin-source` match their usages in Tasks 7 and 10.

**One known follow-up for the executor**, not a gap: Task 1's spec reuses `makeRun`/`makeTrade`/`curve` builders that Tasks 2 and 3 also need. They are copied per file rather than extracted to a shared fixture — three copies is the point at which extraction becomes right, so if a fourth consumer appears, extract to `strategy-lab/testing/run-fixtures.ts` instead of copying again.
