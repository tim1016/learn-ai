import {
  HttpErrorResponse,
  HttpResponse,
  provideHttpClient,
  withInterceptors,
  type HttpInterceptorFn,
} from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { provideZonelessChangeDetection, signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, Router, convertToParamMap } from "@angular/router";
import { within } from "@testing-library/angular";
import { Apollo } from "apollo-angular";
import { BehaviorSubject, of, throwError } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail } from "../../graphql/backtest-runs.query";
import { JobsService } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { StrategyLabComponent } from "./strategy-lab.component";
import { toStrategyLabConfiguration } from "./strategy-lab.models";

function run(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: 91,
    engine: "PYTHON",
    source: "engine",
    requestedEngine: "both",
    strategyName: "ema_crossover_signal",
    symbol: "QQQ",
    leanRunId: null,
    parameters: JSON.stringify({ fast: 8, slow: 21, symbol: "QQQ" }),
    startDate: "2026-03-02",
    endDate: "2026-04-02",
    fillMode: "next_bar_open",
    executedAt: 1,
    durationMs: 2,
    totalTrades: 3,
    winningTrades: 2,
    losingTrades: 1,
    winRate: 2 / 3,
    totalPnL: 120,
    initialCash: 75_000,
    commissionPerOrder: 0.35,
    finalEquity: 75_120,
    totalFees: 2.1,
    maxDrawdown: 0.02,
    sharpeRatio: 1.1,
    sortinoRatio: 1.4,
    profitFactor: 2,
    leanStatisticsJson: null,
    leanAnalysisJson: null,
    verdictJson: null,
    verdictVersion: null,
    verdictGrade: null,
    verdictSignal: null,
    equityCurve: null,
    validationAnalytics: null,
    dataPolicy: {
      source: "polygon",
      symbol: "QQQ",
      adjusted: true,
      session: "regular",
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
      timestamp_policy: "bar_close_ms_utc",
      timezone: "America/New_York",
      provider_kind: "live",
      fixture_id: null,
      fixture_sha256: null,
    },
    insightSummaryJson: null,
    parityGroupId: null,
    trades: [],
    tradesTruncated: false,
    parityVerdicts: [],
    ...overrides,
  };
}

function strategyCatalog() {
  return [{
    name: "ema_crossover_signal",
    display_name: "EMA crossover",
    description: "EMA validation",
    params_schema: {
      properties: {
        symbol: { type: "string", default: "SPY" },
        lookback: { type: "integer", default: 50 },
      },
    },
    supported_resolutions: ["minute"],
    strategy_bars: { timespan: "minute", multiplier: 15, parameter: null },
    lean_twin: "ema_crossover_signal",
  }];
}

interface WatchQueryRequest {
  variables?: Record<string, unknown>;
}

/**
 * Once a run loads, the workbench mounts the real chart, which pulls in
 * auxiliary data this suite never asserts on: the indicator catalog, a
 * stock snapshot, and the run's chart bars. Short-circuiting those requests
 * keeps `HttpTestingController` scoped to what each test manages explicitly
 * (`/api/engine/strategies`, the LEAN source fetch) and keeps zoneless
 * `whenStable()` from hanging on a request nothing in the test ever flushes.
 * The chart bars fetch is answered with an error, not a fabricated payload,
 * so the chart's own `catchError` path handles it the way a real outage
 * would — a fabricated 200 with no `coverage`/`bars` would crash the chart's
 * own computed signals instead.
 */
const bypassAuxiliaryChartRequests: HttpInterceptorFn = (request, next) => {
  if (request.url.endsWith("/api/dataset/available") || request.url.endsWith("/graphql")) {
    return of(new HttpResponse({ status: 200, body: null }));
  }
  if (request.url.endsWith("/api/engine/chart")) {
    return throwError(() => new HttpErrorResponse({ status: 503, url: request.url }));
  }
  return next(request);
};

async function createLab(
  options: {
    restoreRun?: number;
    activeRun?: number;
    backtestRun?: BacktestRunDetail | null;
    /** Makes the run-detail watch query surface a GraphQL/transport error,
     *  which is a different failure from the fetched run being unrestorable. */
    backtestRunQueryError?: Error;
  } = {},
) {
  const jobs = signal<never[]>([]);
  const navigate = vi.fn(async () => true);
  const diagnose = vi.fn();
  const query: Record<string, string> = {};
  if (options.activeRun) query["run"] = String(options.activeRun);
  if (options.restoreRun) query["restoreRun"] = String(options.restoreRun);
  // A subject, not `of(...)`: the query string is the workbench's only run
  // input now, so tests drive run selection and back-navigation by pushing
  // params the way the router does.
  const queryParamMap = new BehaviorSubject(convertToParamMap(query));
  const runDetail = options.backtestRunQueryError
    ? { data: undefined, loading: false, error: options.backtestRunQueryError }
    : { data: { backtestRun: options.backtestRun ?? null }, loading: false };
  const watchQuery = vi.fn((request: WatchQueryRequest) =>
    // StrategyLabRunReport watches the single-run detail query (variables.id);
    // the run-history rail watches the paged list query — same mock, two shapes.
    request.variables && "id" in request.variables
      ? { valueChanges: of(runDetail), stopPolling: vi.fn() }
      : {
          valueChanges: of({ data: { backtestRuns: { pageInfo: { hasNextPage: false, endCursor: null }, nodes: [] } } }),
          refetch: vi.fn(),
        },
  );
  const runDetailWatchCount = (): number =>
    watchQuery.mock.calls.filter(([request]) => request.variables && "id" in request.variables).length;
  await TestBed.configureTestingModule({
    imports: [StrategyLabComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(withInterceptors([bypassAuxiliaryChartRequests])),
      provideHttpClientTesting(),
      {
        provide: ActivatedRoute,
        useValue: { queryParamMap, snapshot: { queryParamMap: queryParamMap.value } },
      },
      { provide: Router, useValue: { navigate } },
      { provide: JobsService, useValue: { jobs, job: vi.fn(() => null), startJob: vi.fn(), fetchResult: vi.fn(), cancelJob: vi.fn() } },
      {
        provide: LeanSidecarService,
        useValue: {
          diagnose,
          nextTradingDayOpen: vi.fn(async () => ({ session_open_ms_utc: 1_700_000_000_000 })),
        },
      },
      { provide: Apollo, useValue: { watchQuery } },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabComponent);
  fixture.detectChanges();
  const navigateToQuery = (next: Record<string, string>): void => {
    queryParamMap.next(convertToParamMap(next));
    fixture.detectChanges();
  };
  return {
    fixture,
    http: TestBed.inject(HttpTestingController),
    navigate,
    diagnose,
    navigateToQuery,
    runDetailWatchCount,
  };
}

describe("Strategy Lab Workbench", () => {
  it("starts with Workbench and History tabs instead of repeated page framing", async () => {
    const { fixture, http } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("Workbench");
    expect(root.textContent).toContain("History");
    expect(root.textContent).not.toContain("Run a strategy, inspect its evidence");
    expect(root.querySelector("app-strategy-lab-config-rail")).not.toBeNull();
    http.verify();
  });

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
    // The stage is the workbench grid's own second column — no wrapper section,
    // which would duplicate its "Strategy evidence" landmark and reintroduce
    // the auto-placement drift the stage's two-child grid now prevents.
    expect(root.querySelector(".workbench > app-strategy-lab-stage")).not.toBeNull();
    expect(root.querySelectorAll("[aria-label='Strategy evidence']")).toHaveLength(1);
    expect(root.textContent).not.toContain("Back to workbench");
    http.verify();
  });

  it("clears the run off the page when back-navigation drops the run parameter", async () => {
    const saved = run();
    const { fixture, http, navigateToQuery } = await createLab({ activeRun: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const root = fixture.nativeElement as HTMLElement;
    await vi.waitFor(() => {
      expect(root.querySelector("app-strategy-lab-run-stats")).not.toBeNull();
    });

    // Back off `?run=N` lands on bare /strategy-lab: the statistics and chart
    // must go with the run, not linger as evidence of a run no longer selected.
    navigateToQuery({});
    await vi.waitFor(() => {
      expect(root.querySelector("app-strategy-lab-run-stats")).toBeNull();
    });

    expect(root.querySelector("app-strategy-lab-chart")).toBeNull();
    expect(root.textContent).toContain("Run a validation to populate the equity curve");
    expect(root.textContent).not.toContain("was not found");
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
    const { fixture, http, navigateToQuery } = await createLab({ backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();

    const lab = fixture.componentInstance;
    lab.config.changeEngine("lean");
    lab.config.customLeanSource.set("class Edited(QCAlgorithm): pass");
    // What the runner does on completion: claim the run, then push `?run=N`.
    lab.runs.justProducedRunId.set(saved.id);
    navigateToQuery({ run: String(saved.id) });
    await vi.waitFor(() => {
      expect(lab.runs.justProducedRunId()).toBeNull();
    });

    expect(lab.config.customLeanSource()).toBe("class Edited(QCAlgorithm): pass");
  });

  it("loads the run detail once per selected run rather than once per surface", async () => {
    const saved = run();
    const { fixture, http, runDetailWatchCount } = await createLab({
      activeRun: saved.id,
      backtestRun: saved,
    });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await vi.waitFor(() => {
      expect(fixture.componentInstance.report.run()).not.toBeNull();
    });
    await fixture.whenStable();

    // The report service is the single source of truth for the loaded run;
    // configuration restore reads it rather than fetching the same run again.
    expect(runDetailWatchCount()).toBe(1);
    http.verify();
  });

  it("keeps the run button enabled when the saved-run fetch fails transiently", async () => {
    const saved = run();
    const { fixture, http } = await createLab({
      activeRun: saved.id,
      backtestRunQueryError: new Error("Network error"),
    });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const root = fixture.nativeElement as HTMLElement;
    await vi.waitFor(() => {
      expect(root.textContent).toContain("Run report could not be loaded");
    });

    // A transport/query failure says nothing about the configuration on
    // screen, which is still valid — it must not get silently disabled by a
    // message that describes a restore problem rather than the fetch that
    // actually failed.
    expect(fixture.componentInstance.config.configurationWarning()).toBeNull();
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(false);
    http.verify();
  });

  it("names a saved run that no longer exists instead of blaming the report", async () => {
    const { fixture, http } = await createLab({ activeRun: 404, backtestRun: null });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const root = fixture.nativeElement as HTMLElement;
    await vi.waitFor(() => {
      expect(root.textContent).toContain("Saved run #404 was not found.");
    });

    expect(root.textContent).not.toContain("Run report could not be loaded");
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(false);
    http.verify();
  });

  it("blocks a rerun only when restoring the fetched configuration itself fails", async () => {
    const malformed = run({ parameters: "{" });
    const { fixture, http } = await createLab({ activeRun: malformed.id, backtestRun: malformed });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await vi.waitFor(() => {
      expect(fixture.componentInstance.config.configurationWarning()).not.toBeNull();
    });

    expect(fixture.componentInstance.config.configurationWarning()).toMatch(/Saved run parameters are malformed/);
    expect(fixture.componentInstance.runs.runError()).toMatch(/Saved run parameters are malformed/);
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(true);
    http.verify();
  });

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

});

describe("Strategy Lab saved configuration", () => {
  it("rehydrates the persisted configuration after returning from Results", async () => {
    const saved = run();
    const { fixture, http } = await createLab({ restoreRun: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const root = fixture.nativeElement as HTMLElement;
    // Restoring a persisted run loads its report, which auto-collapses the
    // configuration to the compact strip (see the "collapses the
    // configuration on completion" spec) — expand it back to inspect the
    // restored controls, the same way an operator would.
    await vi.waitFor(() => {
      expect(root.querySelector(".config-strip__expand")).not.toBeNull();
    });
    root.querySelector<HTMLButtonElement>(".config-strip__expand")?.click();
    fixture.detectChanges();
    await vi.waitFor(() => {
      expect(root.querySelector(".config-rail")).not.toBeNull();
    });
    fixture.detectChanges();

    expect(root.querySelector<HTMLElement>("[role='radio'][aria-checked='true']")?.textContent?.trim()).toBe("both");
    expect(root.querySelector("app-instrument-card .ticker-box__symbol")?.textContent?.trim()).toBe("QQQ");
    const dates = root.querySelectorAll<HTMLInputElement>("app-time-window-card input[type='date']");
    expect(dates[0]?.value).toBe("2026-03-02");
    expect(dates[1]?.value).toBe("2026-04-02");

    const advanced = root.querySelector<HTMLDetailsElement>("details.advanced");
    if (!advanced) throw new Error("Advanced configuration controls are missing");
    advanced.open = true;
    fixture.detectChanges();
    expect(root.querySelector<HTMLSelectElement>("details.advanced select")?.value).toBe("next_bar_open");
    const executionInputs = root.querySelectorAll<HTMLInputElement>("details.advanced fieldset:last-of-type input");
    expect(executionInputs[0]?.value).toBe("75000");
    expect(executionInputs[1]?.value).toBe("0.35");
    http.verify();
  });

  it("restores every persisted control without inferring away the Both selection", () => {
    const configuration = toStrategyLabConfiguration(run(), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "daily",
      autoFetch: false,
    });

    expect(configuration).toEqual({
      engine: "both",
      range: {
        symbol: "QQQ",
        from: "2026-03-02",
        to: "2026-04-02",
        resolution: "minute",
        multiplier: 1,
        session: "rth",
        autoFetch: true,
      },
      parameters: { fast: 8, slow: 21, symbol: "QQQ" },
      fillMode: "next_bar_open",
      initialCash: 75_000,
      commissionPerOrder: 0.35,
      dataPolicy: run().dataPolicy,
    });
  });

  it("rejects malformed persisted parameters instead of enabling a changed rerun", () => {
    expect(() => toStrategyLabConfiguration(run({ parameters: "{" }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    })).toThrow(/Saved run parameters are malformed/);
  });
});
