import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { Component, input, output, provideZonelessChangeDetection, signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, Router, convertToParamMap } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail } from "../../graphql/backtest-runs.query";
import { JobsService } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { RunReportComponent } from "../engine-lab/run-report/run-report.component";
import { StrategyLabComponent } from "./strategy-lab.component";
import { toStrategyLabConfiguration } from "./strategy-lab.models";

@Component({ selector: "app-engine-run-report", template: "Persisted run report" })
class RunReportStubComponent {
  readonly runId = input.required<number>();
  readonly runDetail = input<BacktestRunDetail | null>(null);
  readonly runRefreshed = output<BacktestRunDetail>();
}

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

async function createLab(options: { restoreRun?: number; routeRunId?: number; backtestRun?: BacktestRunDetail | null } = {}) {
  const jobs = signal<never[]>([]);
  const navigate = vi.fn(async () => true);
  const params = convertToParamMap(options.restoreRun ? { restoreRun: String(options.restoreRun) } : {});
  await TestBed.configureTestingModule({
    imports: [StrategyLabComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ActivatedRoute, useValue: { queryParamMap: of(params), snapshot: { queryParamMap: params } } },
      { provide: Router, useValue: { navigate } },
      { provide: JobsService, useValue: { jobs, job: vi.fn(() => null), startJob: vi.fn(), fetchResult: vi.fn(), cancelJob: vi.fn() } },
      { provide: LeanSidecarService, useValue: { diagnose: vi.fn(), nextTradingDayOpen: vi.fn() } },
      {
        provide: Apollo,
        useValue: {
          query: vi.fn(() => of({ data: { backtestRun: options.backtestRun ?? null } })),
          watchQuery: vi.fn(() => ({
            valueChanges: of({ data: { backtestRuns: { pageInfo: { hasNextPage: false, endCursor: null }, nodes: [] } } }),
            refetch: vi.fn(),
          })),
        },
      },
    ],
  }).overrideComponent(StrategyLabComponent, {
    remove: { imports: [RunReportComponent] },
    add: { imports: [RunReportStubComponent] },
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabComponent);
  if (options.routeRunId) fixture.componentRef.setInput("id", String(options.routeRunId));
  fixture.detectChanges();
  return { fixture, http: TestBed.inject(HttpTestingController), navigate };
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
    expect(root.querySelector("app-engine-run-report")).toBeNull();
    http.verify();
  });

  it("opens a selected history run on its dedicated Results route", async () => {
    const { fixture, http, navigate } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();

    fixture.componentInstance.selectHistoryRun("91");

    expect(navigate).toHaveBeenCalledWith(["/strategy-lab/runs", 91]);
    http.verify();
  });

  it("keeps a deep-linked run inside the full-width Workbench with statistics below configuration", async () => {
    const saved = run({
      verdictJson: JSON.stringify({
        verdict_version: 1,
        engine: "python",
        generated_at_ms: 1,
        composite: 41,
        grade: "C",
        signal: "Rework",
        headline: "Needs work.",
        red_flags: [],
        dimensions: [{
          key: "return_quality",
          label: "Return quality",
          weight: 0.2,
          score: 8,
          summary: "Mixed return quality.",
          sub_scores: [
            { key: "sharpe", label: "Sharpe", score: 2, raw_value: -2.03, display: "−2.03", note: "Below target." },
            { key: "cagr", label: "CAGR", score: 17, raw_value: 0.12, display: "12.00%", note: "Above target." },
          ],
        }],
        missing_metrics: [],
        normalized_weights: false,
        cleanliness: null,
      }),
    });
    const { fixture, http } = await createLab({ routeRunId: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await vi.waitFor(() => expect(fixture.componentInstance.selectedRun()?.id).toBe(saved.id));
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("Workbench");
    expect(root.textContent).toContain("History");
    expect(root.querySelector(".workbench__rail app-strategy-lab-config-rail .config-strip")).not.toBeNull();
    expect(root.querySelector(".workbench__rail app-strategy-lab-results-sidebar")).not.toBeNull();
    expect(root.querySelector(".workbench__stage app-engine-run-report")).not.toBeNull();
    expect(root.textContent).toContain("CAGR");
    expect(root.textContent).not.toContain("Back to workbench");

    root.querySelector<HTMLButtonElement>("[aria-label='Expand configuration']")?.click();
    fixture.detectChanges();
    const strategyPicker = root.querySelector<HTMLSelectElement>("#strategy-picker");
    expect(strategyPicker?.value).toBe("ema_crossover_signal");
    expect(strategyPicker?.selectedOptions[0]?.textContent).toContain("EMA crossover");
    http.verify();
  });

});

describe("Strategy Lab saved configuration", () => {
  it("rehydrates the persisted configuration after returning from Results", async () => {
    const saved = run();
    const { fixture, http } = await createLab({ restoreRun: saved.id, backtestRun: saved });
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await vi.waitFor(() => {
      expect(fixture.componentInstance.config.engine()).toBe("both");
    });
    fixture.detectChanges();

    expect(fixture.componentInstance.config.range()).toEqual(expect.objectContaining({
      symbol: "QQQ",
      from: "2026-03-02",
      to: "2026-04-02",
      resolution: "minute",
    }));
    expect(fixture.componentInstance.config.fillMode()).toBe("next_bar_open");
    expect(fixture.componentInstance.config.initialCash()).toBe(75_000);
    expect(fixture.componentInstance.config.commissionPerOrder()).toBe(0.35);
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
