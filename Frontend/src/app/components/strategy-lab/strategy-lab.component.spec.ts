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
import { BehaviorSubject, of, throwError } from "rxjs";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail } from "../../services/backtest-runs.types";
import { BacktestRunsService } from "../../services/backtest-runs.service";
import { JobsService, type JobState } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from "../../shared/ticker-catalog/testing/fake-ticker-catalog";
import { StrategyLabComponent } from "./strategy-lab.component";
import { inputsFromBacktestJob, inputsFromSavedRun } from "./strategy-lab.models";

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
    startDate: 1772427600000, // 2026-03-02 ET midnight
    endDate: 1775102400000, // 2026-04-02 ET midnight
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
    metricDocumentation: [],
    notes: null,
    parityVerdicts: [],
    ...overrides,
  };
}

/** The `engine_backtest` payload `StrategyLabRunner.runPython` submits for the run above
 *  (the runner spec pins that the real payload reads back the same way). */
function backtestPayload(): Record<string, unknown> {
  return {
    strategy_name: "ema_crossover_signal",
    requested_engine: "both",
    fill_mode: "next_bar_open",
    initial_cash: 75_000,
    commission_per_order: 0.35,
    params: { symbol: "QQQ", lookback: 8 },
    auto_fetch: true,
    resolution: "minute",
    compatibility_profile: "us-equity-raw-ibkr-v1",
    data_policy: run().dataPolicy,
    start_date: "2026-03-02",
    end_date: "2026-04-02",
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
    /** Makes the run-detail read surface a transport error, which is a
     *  different failure from the fetched run being unrestorable. */
    backtestRunQueryError?: Error;
  } = {},
) {
  const jobs = signal<never[]>([]);
  const activeJobs = signal<JobState[]>([]);
  const navigate = vi.fn(async () => true);
  const diagnose = vi.fn();
  const query: Record<string, string> = {};
  if (options.activeRun) query["run"] = String(options.activeRun);
  if (options.restoreRun) query["restoreRun"] = String(options.restoreRun);
  // A subject, not `of(...)`: the query string is the workbench's only run
  // input now, so tests drive run selection and back-navigation by pushing
  // params the way the router does.
  const queryParamMap = new BehaviorSubject(convertToParamMap(query));
  // StrategyLabRunReport reads the single run; the run-history rail reads the
  // list — one service mock, both reads.
  const backtestRuns = {
    get: vi.fn(() =>
      options.backtestRunQueryError
        ? throwError(() => options.backtestRunQueryError)
        : of(options.backtestRun ?? null),
    ),
    list: vi.fn(() => of([])),
    updateNotes: vi.fn(),
  };
  const runDetailWatchCount = (): number => backtestRuns.get.mock.calls.length;
  // The instrument picker reads the lake catalog on init. This spec drives
  // the workbench through `HttpTestingController` and verifies no request
  // is left open, so the catalog is stubbed rather than served.
  const catalog = fakeTickerCatalog([{ symbol: "SPY", name: "SPDR S&P 500 ETF Trust", exchange: "ARCA" }]);
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
      {
        provide: JobsService,
        useValue: {
          jobs,
          activeJobs,
          job: (id: string) => activeJobs().find((job) => job.id === id) ?? null,
          resumed: signal(true),
          startJob: vi.fn(),
          fetchResult: vi.fn(),
          refreshActive: vi.fn(async () => undefined),
          registryError: signal<string | null>(null),
          cancelJob: vi.fn(),
        },
      },
      {
        provide: LeanSidecarService,
        useValue: {
          diagnose,
          nextTradingDayOpen: vi.fn(async () => ({ session_open_ms_utc: 1_700_000_000_000 })),
        },
      },
      { provide: BacktestRunsService, useValue: backtestRuns },
      provideFakeTickerCatalog(catalog),
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
    activeJobs,
    catalog,
    http: TestBed.inject(HttpTestingController),
    navigate,
    diagnose,
    navigateToQuery,
    runDetailWatchCount,
  };
}

describe("Strategy Lab Workbench", () => {
  // Each lab is a fresh tab: the runner's own-job marker must not leak from
  // one test's adoption into the next one's.
  beforeEach(() => sessionStorage.clear());

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

  it("keeps Run disabled and shows the run as in flight when a backtest is resumed after a reload", async () => {
    const { fixture, http, activeJobs } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const runButton = (): HTMLButtonElement | undefined =>
      Array.from(root.querySelectorAll("button")).find((button) => /Run validation|Running…/.test(button.textContent ?? ""));
    expect(runButton()?.textContent).toContain("Run validation");

    // JobsService.resumeActive() finds the job the reload interrupted.
    activeJobs.set([{ id: "resumed-1", type: "engine_backtest", status: "running", phase: "running_indicators", recentLogs: [], logSeq: 0 }]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(runButton()?.textContent).toContain("Running…");
    expect(runButton()?.disabled).toBe(true);
    // The resumed job's phase is rendered on the stage, not just the button.
    expect(root.textContent).toContain("Running indicators and strategy logic…");

    // Several active jobs from other tabs: nothing is adopted, but Run stays
    // disabled — the container is busy either way.
    activeJobs.set([
      { id: "tab-a", type: "engine_backtest", status: "running", recentLogs: [], logSeq: 0 },
      { id: "tab-b", type: "engine_backtest", status: "running", recentLogs: [], logSeq: 0 },
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(runButton()?.disabled).toBe(true);
    http.verify();
  });

  it("describes a resumed backtest's own inputs on the rail while it runs", async () => {
    // #1953: after a reload the store is rebuilt with defaults, so until the
    // persisted report arrived the rail labelled "Exact run inputs" described
    // SPY over the default window, not the QQQ run actually in flight.
    const { fixture, http, activeJobs } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const field = (label: string | RegExp): HTMLInputElement => within(root).getAllByLabelText(label)[0] as HTMLInputElement;
    expect(field("Start date").value).not.toBe("2026-03-02");

    activeJobs.set([{
      id: "resumed-1", type: "engine_backtest", status: "running", recentLogs: [], logSeq: 0,
      parameters: { backtest: backtestPayload() },
    }]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(field("Start date").value).toBe("2026-03-02");
    expect(field("End date").value).toBe("2026-04-02");
    expect(field(/^lookback/).value).toBe("8");
    expect(field(/^Initial cash/).value).toBe("75000");
    expect(field(/^Commission/).value).toBe("0.35");
    expect((root.querySelector("#strategy-picker") as HTMLSelectElement).value).toBe("ema_crossover_signal");
    expect(root.querySelector(".ticker-box__symbol")?.textContent).toContain("QQQ");
    http.verify();
  });

  it("keeps a loaded report's inputs on the rail when a job from elsewhere is adopted", async () => {
    const { fixture, http, activeJobs } = await createLab({ activeRun: 91, backtestRun: run() });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    // A loaded report collapses the rail to its facts strip.
    const facts = (): string => root.querySelector(".config-strip__facts")?.textContent ?? "";
    expect(facts()).toContain("2026-03-02 → 2026-04-02");

    activeJobs.set([{
      id: "other-tab", type: "engine_backtest", status: "running", recentLogs: [], logSeq: 0,
      parameters: { backtest: { ...backtestPayload(), start_date: "2025-01-06", end_date: "2025-02-06" } },
    }]);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(facts()).toContain("2026-03-02 → 2026-04-02");
    expect(facts()).not.toContain("2025-01-06");
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

  it("restores the finished run over controls the operator changed while it ran", async () => {
    const saved = run();
    const { fixture, http, navigateToQuery } = await createLab({ backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();

    const lab = fixture.componentInstance;
    lab.config.changeEngine("lean");
    lab.config.customLeanSource.set("class Edited(QCAlgorithm): pass");
    lab.runs.justProducedRunId.set(saved.id);
    // Nothing locks the still-enabled controls while a job runs, so the
    // configuration on screen is not automatically the one that produced the
    // finished run.
    lab.config.initialCash.set(1_000);
    lab.config.fillMode.set("signal_bar_close");
    navigateToQuery({ run: String(saved.id) });

    // The report must not render beside inputs that no longer describe it —
    // "Run validation" has to reproduce what is displayed.
    await vi.waitFor(() => {
      expect(lab.config.initialCash()).toBe(75_000);
    });
    expect(lab.config.fillMode()).toBe("next_bar_open");
    // The one thing the restore would otherwise destroy: `applyStrategy` nulls
    // the custom source, and custom source is the only route to a
    // parameterized LEAN run.
    expect(lab.config.customLeanSource()).toBe("class Edited(QCAlgorithm): pass");
    http.verify();
  });

  it("abandons a restore for a run the operator has already navigated away from", async () => {
    localStorage.removeItem("engineLab.configNavOverride");
    const saved = run();
    const { fixture, http, navigateToQuery } = await createLab({
      activeRun: saved.id,
      backtestRun: saved,
    });
    const lab = fixture.componentInstance;
    // The catalog request is deliberately left in flight: `adoptRun` captures
    // its run, collapses the rail, and then awaits it — that await is the
    // window `activeRunId` can move under.
    const strategies = http.expectOne((request) => request.url.endsWith("/api/engine/strategies"));
    await vi.waitFor(() => {
      expect(lab.config.configNavCollapsed()).toBe(true);
    });

    navigateToQuery({});
    strategies.flush(strategyCatalog());
    await fixture.whenStable();
    // A macrotask turn, so every continuation parked on the catalog promise —
    // the one inside `adoptRun` included — has run before the assertions.
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Run 91 is no longer on the page, so its configuration must not be
    // written over the defaults the operator is now looking at.
    expect(lab.report.displayRun()).toBeNull();
    expect(lab.config.initialCash()).toBe(100_000);
    expect(lab.config.fillMode()).toBe("signal_bar_close");
    http.verify();
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

  it("switches off History so a run that fails to load is not a silent dead end", async () => {
    const { fixture, http, navigateToQuery } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();

    const lab = fixture.componentInstance;
    // The operator is browsing History, not the workbench, when they click a
    // saved run — `selectHistoryRun` only navigates, so drive the resulting
    // URL change the way the router would.
    lab.config.activeTab.set("history");
    fixture.detectChanges();
    expect(lab.config.activeTab()).toBe("history");

    // The clicked run has since been deleted: the detail query resolves with
    // no run, which is `notFound`, not a transport failure.
    navigateToQuery({ run: "404" });

    await vi.waitFor(() => {
      expect(lab.config.activeTab()).toBe("configuration");
    });
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

  it("blocks a rerun once the tree the engine choice reads has answered without the symbol", async () => {
    const { fixture, http, catalog } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    const config = fixture.componentInstance.config;
    expect(config.rerunBlocked()).toBe(false);

    // `both` reads the raw tree, which the lake reports holds nothing at all.
    // With auto-fetch on (the default) that is the engine's job: it
    // materializes the missing raw days before reading, so the run stays open.
    catalog.viewFor("raw").pool.set([]);
    config.changeEngine("both");
    expect(config.rerunBlocked()).toBe(false);

    // With auto-fetch off the run reads the tree as it stands. An
    // answered-empty tree is exactly the case the engine would fail deep
    // inside, so it must block rather than pass as "not yet known".
    config.changeRange({ ...config.range(), autoFetch: false });
    expect(config.rerunBlocked()).toBe(true);

    // Opening the dropdown re-reads the tree. The verdict already in hand
    // holds through that window rather than briefly opening the run.
    catalog.viewFor("raw").loading.set(true);
    expect(config.rerunBlocked()).toBe(true);
    catalog.viewFor("raw").loading.set(false);

    // Until the raw tree has answered, nothing can be said about the symbol.
    catalog.viewFor("raw").resolved.set(false);
    expect(config.rerunBlocked()).toBe(false);

    // The split-adjusted tree still holds SPY: switching back clears the block.
    catalog.viewFor("raw").resolved.set(true);
    config.changeEngine("python");
    expect(config.rerunBlocked()).toBe(false);
    http.verify();
  });

  // A frozen recording restores with auto-fetch off, and its symbol (QQQ) is
  // not in the lake (the catalog holds SPY only). Whether the membership
  // gate applies depends on who reads the bars.
  function fixtureBackedRun(requestedEngine: "lean" | "both"): BacktestRunDetail {
    const basePolicy = run().dataPolicy;
    if (basePolicy === null) throw new Error("the run factory always carries a data policy");
    return run({
      requestedEngine,
      dataPolicy: {
        ...basePolicy,
        source: "synthetic",
        provider_kind: "fixture",
        fixture_id: "fixture-1",
        fixture_sha256: "abc123",
      },
    });
  }

  it("never gates a restored direct LEAN fixture run on lake membership", async () => {
    // The LEAN sidecar replays the recording itself: the run never reads
    // the lake, so the gate must not make it impossible to rerun.
    const saved = fixtureBackedRun("lean");
    const { fixture, http } = await createLab({ activeRun: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const config = fixture.componentInstance.config;
    await vi.waitFor(() => {
      expect(config.autoFetch()).toBe(false);
    });

    expect(config.effectiveSymbol()).toBe("QQQ");
    expect(config.engine()).toBe("lean");
    expect(config.readsLake()).toBe(false);
    expect(config.rerunBlocked()).toBe(false);
    http.verify();
  });

  it("still gates a restored fixture run that the Python engine would read from the lake", async () => {
    // `both` goes through the Python engine, which resolves the lake tree
    // regardless of the restored policy's provenance: the absent symbol
    // would fail deep in the engine, so the gate stays.
    const saved = fixtureBackedRun("both");
    const { fixture, http } = await createLab({ activeRun: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    const config = fixture.componentInstance.config;
    await vi.waitFor(() => {
      expect(config.autoFetch()).toBe(false);
    });

    expect(config.engine()).toBe("both");
    expect(config.readsLake()).toBe(true);
    expect(config.rerunBlocked()).toBe(true);
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

  it("reads a backtest job's payload back into the inputs the rail shows", () => {
    const inputs = inputsFromBacktestJob({ backtest: backtestPayload() }, {
      symbol: "SPY", from: "2020-01-01", to: "2020-02-01", resolution: "minute", autoFetch: true,
    });

    expect(inputs).toMatchObject({
      strategyName: "ema_crossover_signal",
      engine: "both",
      range: { symbol: "QQQ", from: "2026-03-02", to: "2026-04-02", resolution: "minute", multiplier: 1, session: "rth", autoFetch: true },
      parameters: { symbol: "QQQ", lookback: 8 },
      fillMode: "next_bar_open",
      initialCash: 75_000,
      commissionPerOrder: 0.35,
    });
    expect(inputs?.dataPolicy?.symbol).toBe("QQQ");
  });

  it("leaves the rail alone for a payload it did not compose, including a LEAN-only run's", () => {
    const range = { symbol: "SPY", from: "2020-01-01", to: "2020-02-01", resolution: "minute" as const, autoFetch: true };

    expect(inputsFromBacktestJob(undefined, range)).toBeNull();
    expect(inputsFromBacktestJob({ request: { run_id: "strategy_lab_spy_x1", start_ms_utc: 1, end_ms_utc: 2 } }, range)).toBeNull();
    expect(inputsFromBacktestJob({ backtest: { ...backtestPayload(), strategy_name: undefined } }, range)).toBeNull();
    expect(inputsFromBacktestJob({ backtest: { ...backtestPayload(), requested_engine: "rust" } }, range)).toBeNull();
    expect(inputsFromBacktestJob({ backtest: { ...backtestPayload(), data_policy: null } }, range)).toBeNull();
    expect(inputsFromBacktestJob({ backtest: { ...backtestPayload(), start_date: "March 2" } }, range)).toBeNull();
  });

  it("restores every persisted control without inferring away the Both selection", () => {
    const configuration = inputsFromSavedRun(run(), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "daily",
      autoFetch: false,
    });

    expect(configuration).toEqual({
      strategyName: "ema_crossover_signal",
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
    expect(() => inputsFromSavedRun(run({ parameters: "{" }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    })).toThrow(/Saved run parameters are malformed/);
  });
});
