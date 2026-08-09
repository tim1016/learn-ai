import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { Component, input, output, provideZonelessChangeDetection, signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, convertToParamMap } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type { BacktestRunDetail } from "../../graphql/backtest-runs.query";
import { JobsService } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { RunReportComponent } from "../engine-lab/run-report/run-report.component";
import { StrategyLabComponent, toStrategyLabConfiguration } from "./strategy-lab.component";

@Component({ selector: "app-engine-run-report", template: "Saved report {{ runId() }}" })
class RunReportStubComponent {
  readonly runId = input.required<number>();
  readonly runDetail = input<BacktestRunDetail | null>(null);
  readonly rerun = output();
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

async function createLab(backtestRun: BacktestRunDetail | null = null) {
  const jobs = signal<never[]>([]);
  await TestBed.configureTestingModule({
    imports: [StrategyLabComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({})) } },
      {
        provide: JobsService,
        useValue: {
          jobs,
          job: vi.fn(() => null),
          startJob: vi.fn(),
          fetchResult: vi.fn(),
          cancelJob: vi.fn(),
        },
      },
      {
        provide: LeanSidecarService,
        useValue: { diagnose: vi.fn(), nextTradingDayOpen: vi.fn() },
      },
      {
        provide: Apollo,
        useValue: {
          query: vi.fn(() => of({ data: { backtestRun } })),
          watchQuery: vi.fn(() => ({
            valueChanges: of({ data: { backtestRuns: [] } }),
            refetch: vi.fn(async () => ({ data: { backtestRuns: [] } })),
          })),
        },
      },
    ],
  })
    .overrideComponent(StrategyLabComponent, {
      remove: { imports: [RunReportComponent] },
      add: { imports: [RunReportStubComponent] },
    })
    .compileComponents();
  const fixture = TestBed.createComponent(StrategyLabComponent);
  fixture.detectChanges();
  return { fixture, http: TestBed.inject(HttpTestingController) };
}

describe("Strategy Lab History rehydration", () => {
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

  it("preserves the complete persisted data policy for an exact rerun", () => {
    const basePolicy = run().dataPolicy;
    if (basePolicy === null) throw new Error("test run must include a data policy");
    const dataPolicy = {
      ...basePolicy,
      adjusted: false,
      session: "extended" as const,
      input_bars: { timespan: "minute" as const, multiplier: 5 },
      strategy_bars: { timespan: "minute" as const, multiplier: 30 },
      provider_kind: "fixture" as const,
      fixture_id: "bars-v1",
      fixture_sha256: "a".repeat(64),
    };

    const configuration = toStrategyLabConfiguration(run({ dataPolicy }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "daily",
    });

    expect(configuration.dataPolicy).toEqual(dataPolicy);
    expect(configuration.range).toEqual(expect.objectContaining({
      multiplier: 5,
      session: "extended",
      autoFetch: false,
    }));
  });

  it("rejects malformed persisted parameters instead of enabling a changed rerun", () => {
    expect(() => toStrategyLabConfiguration(run({ parameters: "{" }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    })).toThrow(/Saved run parameters are malformed/);
  });

  it("uses producing engine only as a legacy-row fallback", () => {
    expect(toStrategyLabConfiguration(run({ requestedEngine: null, engine: "LEAN", source: "lean-sidecar" }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    }).engine).toBe("lean");
  });

  it("keeps legacy LEAN cash out of schema-validated strategy parameters", () => {
    const configuration = toStrategyLabConfiguration(run({
      engine: "LEAN",
      source: "lean-sidecar",
      parameters: JSON.stringify({ symbol: "SPY", starting_cash: 100000 }),
    }), {
      symbol: "SPY",
      from: "2025-01-01",
      to: "2025-01-02",
      resolution: "minute",
    });

    expect(configuration.parameters).toEqual({ symbol: "QQQ" });
    expect(configuration.initialCash).toBe(75_000);
  });
});

describe("StrategyLabComponent", () => {
  it("instantiates the focused workbench without legacy availability side effects", async () => {
    const { fixture, http } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("Strategy Lab");
    expect(root.querySelector("app-strategy-lab-config-rail")).not.toBeNull();
    http.expectNone((request) => request.url.includes("/data/availability"));
    http.verify();
  });

  it("keeps History available when the strategy catalog is empty", async () => {
    const { fixture, http } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([]);
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("History");
    expect(root.textContent).toContain("No runnable strategies");
    http.verify();
  });

  it("keeps History available when the strategy catalog fails", async () => {
    const { fixture, http } = await createLab();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(
      { detail: "offline" },
      { status: 503, statusText: "Unavailable" },
    );
    await fixture.whenStable();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    expect(root.textContent).toContain("History");
    expect(root.textContent).toContain("Failed to load strategies");
    http.verify();
  });

  it("keeps malformed History evidence visible while blocking reconstruction and rerun", async () => {
    const { fixture, http } = await createLab(run({ parameters: "{" }));
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.componentInstance.selectHistoryRun("91");
    fixture.detectChanges();

    expect(fixture.componentInstance.runs.runError()).toMatch(/Saved run parameters are malformed/);
    expect(fixture.componentInstance.runs.completedRunId()).toBe(91);
    expect(fixture.componentInstance.selectedRun()?.id).toBe(91);
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(true);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain("Saved report 91");
    http.verify();
  });

  it("keeps a retired strategy's persisted report visible while blocking its rerun", async () => {
    const retired = run({ strategyName: "retired_strategy" });
    const { fixture, http } = await createLab(retired);
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    await fixture.componentInstance.selectHistoryRun("91");
    fixture.detectChanges();

    expect(fixture.componentInstance.runs.completedRunId()).toBe(91);
    expect(fixture.componentInstance.config.selectedStrategyName()).toBe("retired_strategy");
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(true);
    expect((fixture.nativeElement as HTMLElement).textContent).toContain("Saved report 91");
    http.verify();
  });

  it("reconciles a saved strategy selected before the strategy catalog resolves", async () => {
    const { fixture, http } = await createLab(run());

    await fixture.componentInstance.selectHistoryRun("91");
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(true);

    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.config.selectedStrategyName()).toBe("ema_crossover_signal");
    expect(fixture.componentInstance.config.paramValues()).toEqual(expect.objectContaining({
      fast: 8,
      slow: 21,
      lookback: 50,
      symbol: "QQQ",
    }));
    expect(fixture.componentInstance.config.configurationWarning()).toBeNull();
    expect(fixture.componentInstance.config.rerunBlocked()).toBe(false);
    http.verify();
  });

  it("preserves untouched restored policy fields when only the end date changes", async () => {
    const basePolicy = run().dataPolicy;
    if (basePolicy === null) throw new Error("test run must include a data policy");
    const restoredPolicy: NonNullable<BacktestRunDetail["dataPolicy"]> = {
      ...basePolicy,
      adjusted: false,
      session: "extended",
      input_bars: { timespan: "minute", multiplier: 5 },
      provider_kind: "fixture",
      fixture_id: "spy-bars-v1",
      fixture_sha256: "b".repeat(64),
    };
    const { fixture, http } = await createLab(run({ dataPolicy: restoredPolicy }));
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush(strategyCatalog());
    await fixture.whenStable();
    await fixture.componentInstance.selectHistoryRun("91");

    fixture.componentInstance.config.changeRange({
      ...fixture.componentInstance.config.range(),
      to: "2026-04-03",
    });

    expect(fixture.componentInstance.config.dataPolicy()).toEqual(restoredPolicy);
    http.verify();
  });
});
